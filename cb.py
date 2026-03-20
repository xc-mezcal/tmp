"""
Oracle PL/SQL EXECUTE IMMEDIATE Analyzer
=========================================
Reads Oracle PL/SQL scripts, extracts SQL from EXECUTE IMMEDIATE strings,
parses them via sqlglot, and reports base tables + columns used.

Architecture:
  Phase 1: Extract + reassemble SQL from EXECUTE IMMEDIATE (our code)
  Phase 2: Minimal cleanup for sqlglot (hints, comments only — keep INSERT INTO)
  Phase 3: Walk sqlglot parse tree sequentially, building a TableRegistry
           - CTEs processed top-to-bottom within a block
           - INSERT INTO targets registered so later blocks can resolve through them
           - Alias resolution via sqlglot's Table.name / Table.alias
  Phase 4: Flatten to (base_table, column) pairs via registry.resolve()

Usage (CLI):
    python oracle_sql_analyzer.py <input.sql> [--output results.json]

Usage (notebook):
    from oracle_sql_analyzer import analyze_file
    report, df = analyze_file("my_script.sql")
    df
"""

import re
import json
import argparse
import logging
from pathlib import Path
from typing import Optional

try:
    import sqlglot
    from sqlglot import exp
    SQLGLOT_AVAILABLE = True
except ImportError:
    SQLGLOT_AVAILABLE = False

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ===========================================================================
# Phase 1: Extract EXECUTE IMMEDIATE strings from PL/SQL
# ===========================================================================

def extract_execute_immediate_blocks(plsql_text: str) -> list[str]:
    """
    Find all EXECUTE IMMEDIATE '...' blocks and return the reconstructed SQL.

    At the PL/SQL OUTER level:
    - String literals are delimited by single quotes
    - Inside those literals, '' is an escaped single quote
    - || between string literals is PL/SQL concatenation
    - Anything between || that is NOT a string literal is a PL/SQL expression

    At the SQL INNER level (inside the string literals):
    - || is Oracle SQL string concatenation (PRESERVED as-is)
    - '' has already been unescaped to ' during extraction
    - -- comments, /*+ hints */, regexp patterns are all SQL content
    """
    text = plsql_text.replace('\r\n', '\n')
    blocks = []

    pattern = re.compile(r"EXECUTE\s+IMMEDIATE\s*", re.IGNORECASE)

    for match in pattern.finditer(text):
        line_start = text.rfind('\n', 0, match.start()) + 1
        line_prefix = text[line_start:match.start()]
        if '--' in line_prefix:
            continue

        last_open = text.rfind('/*', 0, match.start())
        last_close = text.rfind('*/', 0, match.start())
        if last_open > last_close:
            continue

        start_pos = match.end()
        raw_block = _extract_exec_imm_statement(text, start_pos)
        if raw_block is not None and len(raw_block.strip()) > 10:
            blocks.append(raw_block)

    logger.info(f"Found {len(blocks)} EXECUTE IMMEDIATE block(s)")
    return blocks


def _extract_exec_imm_statement(text: str, pos: int) -> Optional[str]:
    tokens = _tokenize_plsql_expr(text, pos)
    if not tokens:
        return None

    sql_parts = []
    for tok_type, tok_value in tokens:
        if tok_type == 'STRING':
            sql_parts.append(tok_value)
        elif tok_type == 'EXPR':
            placeholder = _expression_to_placeholder(tok_value)
            sql_parts.append(placeholder)

    result = ''.join(sql_parts)
    return result if result.strip() else None


def _tokenize_plsql_expr(text: str, pos: int) -> list[tuple[str, str]]:
    tokens = []
    i = _skip_ws(text, pos)

    while i < len(text):
        i = _skip_ws(text, i)
        if i >= len(text):
            break

        if text[i] == "'":
            content, end_pos = _parse_plsql_string(text, i)
            if content is not None:
                tokens.append(('STRING', content))
                i = end_pos
            else:
                break
        elif text[i] == '|' and i + 1 < len(text) and text[i + 1] == '|':
            tokens.append(('CONCAT', '||'))
            i += 2
        elif text[i] == ';':
            tokens.append(('END', ';'))
            break
        else:
            expr, end_pos = _parse_plsql_interp_expr(text, i)
            if expr:
                tokens.append(('EXPR', expr))
            i = end_pos

    return tokens


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in ' \t\n\r':
        pos += 1
    return pos


def _parse_plsql_string(text: str, pos: int) -> tuple[Optional[str], int]:
    if pos >= len(text) or text[pos] != "'":
        return None, pos

    result = []
    i = pos + 1
    while i < len(text):
        if text[i] == "'":
            if i + 1 < len(text) and text[i + 1] == "'":
                result.append("'")
                i += 2
            else:
                return ''.join(result), i + 1
        else:
            result.append(text[i])
            i += 1

    logger.warning(f"Unterminated PL/SQL string literal at position {pos}")
    return ''.join(result), i


def _parse_plsql_interp_expr(text: str, pos: int) -> tuple[str, int]:
    i = pos
    paren_depth = 0

    while i < len(text):
        ch = text[i]
        if ch == '(':
            paren_depth += 1
            i += 1
        elif ch == ')':
            paren_depth -= 1
            if paren_depth < 0:
                paren_depth = 0
            i += 1
        elif ch == "'" and paren_depth > 0:
            _, i = _parse_plsql_string(text, i)
        elif paren_depth == 0:
            if ch == '|' and i + 1 < len(text) and text[i + 1] == '|':
                break
            elif ch == ';':
                break
            elif ch == "'":
                break
            elif ch == '\n':
                lookahead = _skip_ws(text, i + 1)
                if lookahead < len(text) and text[lookahead] in "|';":
                    i = lookahead
                    break
                else:
                    i += 1
            else:
                i += 1
        else:
            i += 1

    expr = text[pos:i].strip()
    return expr, i


def _expression_to_placeholder(expr: str) -> str:
    expr_upper = expr.upper().strip()

    if 'TO_CHAR' in expr_upper:
        return '01-JAN-25'
    if 'TO_DATE' in expr_upper:
        return '01-JAN-25'

    if any(fn in expr_upper for fn in [
        'LENGTH', 'REGEXP', 'NVL', 'COALESCE', 'DECODE',
        'GREATEST', 'LEAST', 'ABS', 'ROUND', 'TRUNC',
        'MOD', 'CEIL', 'FLOOR', 'SUBSTR', 'INSTR',
    ]):
        return '0'

    numeric_hints = [
        'AMT', 'NUM', 'COUNT', 'ID', 'LIMIT', 'MIN', 'MAX',
        'THRESHOLD', 'DAYS', 'FREQ', 'PERIOD', 'IND', 'AMOUNT',
        'SCORE', 'PCT', 'RATE', 'QTY', 'SIZE', 'LEN', 'SCENARIO',
    ]
    if any(hint in expr_upper for hint in numeric_hints):
        return '0'

    if '(' not in expr and re.match(r'^[A-Za-z_][A-Za-z0-9_.]*$', expr.strip()):
        return '0'

    return 'PLACEHOLDER'


# ===========================================================================
# Phase 2: Minimal cleanup for sqlglot
# ===========================================================================
# We keep INSERT INTO (sqlglot parses it fine and we need the target table).
# We only remove things sqlglot can't handle: hints, comments.

def clean_sql_for_parsing(raw_sql: str) -> str:
    sql = raw_sql.strip().rstrip(';').strip()

    # Remove Oracle optimizer hints /*+ ... */
    sql = re.sub(r'/\*\+.*?\*/', '', sql, flags=re.DOTALL)

    # Remove single-line SQL comments
    sql = re.sub(r'--[^\n]*', '', sql)

    # Remove block comments (non-greedy)
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)

    # Normalize whitespace
    sql = re.sub(r'\s+', ' ', sql).strip()

    return sql


# ===========================================================================
# Phase 3: Table Registry — sequential, incremental resolution
# ===========================================================================
#
# Core idea: process the query top-to-bottom. Every time we encounter a
# table reference, we check the registry:
#   - Known derived table (CTE / INSERT target) → trace columns through it
#   - Unknown → it's a base table, register it
#
# Each derived table entry stores a column map:
#   { COLUMN_NAME: [(source_table, source_column), ...] }
#
# The list allows for ambiguity (unqualified column in multi-table join).

class TableRegistry:
    """
    Incrementally built catalog of all tables and their column lineage.
    Shared across all EXECUTE IMMEDIATE blocks in a file.
    """

    def __init__(self):
        # table_name_upper -> {type: "base"|"derived", columns: {col -> [(src_tbl, src_col)]}}
        self._tables: dict[str, dict] = {}

    def register_base(self, name: str):
        key = name.upper()
        if key not in self._tables:
            self._tables[key] = {"type": "base", "columns": {}}
            logger.debug(f"    Registry: base table [{key}]")

    def register_derived(self, name: str, column_map: dict[str, list[tuple[str, str]]]):
        key = name.upper()
        # If previously registered as base (appeared in an earlier block before
        # we saw the INSERT INTO that creates it), upgrade to derived
        self._tables[key] = {"type": "derived", "columns": column_map}
        logger.debug(f"    Registry: derived table [{key}] — {len(column_map)} columns")

    def is_known(self, name: str) -> bool:
        return name.upper() in self._tables

    def is_base(self, name: str) -> bool:
        entry = self._tables.get(name.upper())
        return entry is not None and entry["type"] == "base"

    def is_derived(self, name: str) -> bool:
        entry = self._tables.get(name.upper())
        return entry is not None and entry["type"] == "derived"

    def get_derived_columns(self, name: str) -> dict:
        entry = self._tables.get(name.upper(), {})
        return entry.get("columns", {})

    def get_base_tables(self) -> list[str]:
        return sorted(k for k, v in self._tables.items() if v["type"] == "base")

    def resolve(self, table: str, column: str,
                _visited: Optional[set] = None) -> list[tuple[str, str]]:
        """
        Resolve (table, column) to base table source(s).
        Returns list of (base_table, base_column) — usually length 1,
        >1 for ambiguous cases.
        """
        if _visited is None:
            _visited = set()

        key = (table.upper(), column.upper())
        if key in _visited:
            return [key]
        _visited.add(key)

        entry = self._tables.get(table.upper())

        if entry is None:
            # Unknown table — assume base
            self.register_base(table)
            return [(table.upper(), column.upper())]

        if entry["type"] == "base":
            return [(table.upper(), column.upper())]

        # Derived: look up this column in the column map
        sources = entry["columns"].get(column.upper())
        if not sources:
            # Column not tracked (might be SELECT *, computed, or ROWNUM etc.)
            # Return as-is — we can't trace further
            return [(table.upper(), column.upper())]

        # Recursively resolve each source
        results = []
        for src_tbl, src_col in sources:
            resolved = self.resolve(src_tbl, src_col, _visited.copy())
            results.extend(resolved)

        return results if results else [key]


# ===========================================================================
# Phase 3b: Walk the sqlglot parse tree using the registry
# ===========================================================================

def process_block(cleaned_sql: str, registry: TableRegistry,
                  block_index: int) -> dict:
    """
    Parse one cleaned SQL block with sqlglot. Walk the tree sequentially:
    1. Process CTEs in order (each builds on previous)
    2. Process the main SELECT
    3. If INSERT INTO, register the target as a derived table

    Returns block-level result dict.
    """
    result = {
        "block_index": block_index,
        "cleaned_sql": cleaned_sql,
        "insert_target": None,
        "ctes_found": [],
        "base_tables_found": [],
        "columns": [],
        "parse_errors": [],
    }

    if not SQLGLOT_AVAILABLE:
        result["parse_errors"].append("sqlglot not installed — pip install sqlglot")
        return result

    # --- Parse ---
    try:
        parsed = sqlglot.parse_one(cleaned_sql, read="oracle")
    except Exception as e:
        result["parse_errors"].append(f"Parse error: {e}")
        logger.warning(f"Block {block_index}: parse error — {e}")
        try:
            parsed = sqlglot.parse_one(
                cleaned_sql, read="oracle",
                error_level=sqlglot.ErrorLevel.WARN,
            )
        except Exception as e2:
            result["parse_errors"].append(f"Lenient parse also failed: {e2}")
            return result

    # --- Detect INSERT INTO target ---
    insert_target = None
    insert_node = parsed.find(exp.Insert)
    if insert_node:
        target_table = insert_node.find(exp.Table)
        if target_table:
            insert_target = target_table.name.upper()
            result["insert_target"] = insert_target
            logger.debug(f"  INSERT INTO {insert_target}")

    # --- Process CTEs sequentially ---
    with_node = parsed.find(exp.With)
    if with_node:
        for cte_node in with_node.find_all(exp.CTE):
            cte_name = cte_node.alias
            if not cte_name:
                continue
            cte_name_upper = cte_name.upper()
            result["ctes_found"].append(cte_name_upper)

            cte_body = cte_node.this  # The SELECT inside the CTE
            col_map = _resolve_select_body(cte_body, registry)
            registry.register_derived(cte_name_upper, col_map)
            logger.debug(f"  CTE [{cte_name_upper}]: {list(col_map.keys())}")

    # --- Process the main/final SELECT ---
    main_select = _find_main_select(parsed)
    if main_select is None:
        result["parse_errors"].append("No main SELECT found")
        return result

    main_col_map = _resolve_select_body(main_select, registry)

    # --- If INSERT INTO, register target as derived ---
    if insert_target:
        registry.register_derived(insert_target, main_col_map)

    # --- Flatten all columns to base tables ---
    seen = set()
    for col_name, sources in main_col_map.items():
        for src_tbl, src_col in sources:
            for base_tbl, base_col in registry.resolve(src_tbl, src_col):
                pair = (base_tbl, base_col)
                if pair not in seen:
                    seen.add(pair)
                    result["columns"].append({
                        "base_table": base_tbl,
                        "column": base_col,
                    })

    # Also collect columns from WHERE, JOIN ON, GROUP BY, etc.
    _collect_non_select_columns(parsed, registry, result, seen)

    result["base_tables_found"] = sorted({
        c["base_table"] for c in result["columns"]
        if registry.is_base(c["base_table"])
    })

    return result


def _find_main_select(parsed) -> Optional[object]:
    """
    Find the 'main' SELECT — the outermost one that isn't inside a CTE.
    For INSERT INTO ... WITH ... SELECT, this is the SELECT after all CTEs.
    """
    if not SQLGLOT_AVAILABLE:
        return None

    # For a plain SELECT or WITH...SELECT, the parsed root may be the Select
    if isinstance(parsed, exp.Select):
        return parsed

    # For INSERT INTO ... SELECT, find the Select under Insert
    insert = parsed.find(exp.Insert)
    if insert:
        # The SELECT is the expression part of the insert
        select = insert.find(exp.Select)
        if select:
            return select

    # Fallback: find any Select
    return parsed.find(exp.Select)


def _resolve_select_body(select_node, registry: TableRegistry
                         ) -> dict[str, list[tuple[str, str]]]:
    """
    Given a SELECT node, resolve each column to its source table.

    Steps:
    1. Build scope: FROM + JOINs → alias_map {ALIAS: ACTUAL_TABLE}
    2. For each referenced column:
       - Qualified (t.col) → look up alias → (actual_table, col)
       - Unqualified (col) → search all tables in scope
         → if derived table has this column → that table
         → if only base tables → ambiguous, list all
    3. Track output names (aliases) for the column map

    Returns: {OUTPUT_COL_NAME: [(source_table, source_col), ...]}
    """
    # Step 1: Build scope
    scope = _build_scope(select_node, registry)
    logger.debug(f"    Scope: {scope}")

    # Step 2: Walk columns
    column_map: dict[str, list[tuple[str, str]]] = {}

    # Get the SELECT expressions (the projection list)
    select_expressions = select_node.expressions if hasattr(select_node, 'expressions') else []

    for sel_expr in select_expressions:
        # Determine output name
        output_name = _get_output_name(sel_expr)
        if not output_name:
            continue

        # Find column references in this expression
        sources = _extract_column_sources(sel_expr, scope, registry)

        if sources:
            column_map[output_name] = sources
        # If no sources found (e.g., literal "0 AS SCENARIO_ID"), skip

    return column_map


def _build_scope(select_node, registry: TableRegistry) -> dict[str, str]:
    """
    Build alias → actual_table_name map for a SELECT's FROM + JOINs.
    Also registers unknown tables as base tables in the registry.
    """
    scope = {}  # ALIAS_UPPER -> TABLE_NAME_UPPER

    # Walk all Table nodes that are direct children of this SELECT's FROM/JOIN
    for table_node in select_node.find_all(exp.Table):
        tname = table_node.name.upper()
        talias = table_node.alias.upper() if table_node.alias else tname

        scope[talias] = tname

        # Register in registry if unknown
        if not registry.is_known(tname):
            registry.register_base(tname)

    # Handle subqueries in FROM (inline views)
    for subq in select_node.find_all(exp.Subquery):
        if subq.alias:
            sub_alias = subq.alias.upper()
            sub_select = subq.find(exp.Select)
            if sub_select:
                sub_map = _resolve_select_body(sub_select, registry)
                registry.register_derived(sub_alias, sub_map)
                scope[sub_alias] = sub_alias

    return scope


def _get_output_name(sel_expr) -> Optional[str]:
    """
    Get the output column name for a SELECT expression.
    Could be: explicit alias (AS name), or the column name itself.
    """
    if not SQLGLOT_AVAILABLE:
        return None

    # If it's an Alias node: SELECT col AS alias_name
    if isinstance(sel_expr, exp.Alias):
        return sel_expr.alias.upper()

    # If it's a Column node: SELECT t.col
    if isinstance(sel_expr, exp.Column):
        return sel_expr.name.upper()

    # If it's a Star: SELECT * or SELECT t.*
    if isinstance(sel_expr, exp.Star):
        return None  # Can't track individual columns from *

    # For expressions (functions, etc.), try to find a Column inside
    col = sel_expr.find(exp.Column)
    if col:
        return col.name.upper()

    return None


def _extract_column_sources(expr_node, scope: dict,
                             registry: TableRegistry
                             ) -> list[tuple[str, str]]:
    """
    Find all Column references in an expression and resolve each
    to (actual_table, column_name) using the scope.

    For a simple "t.col", returns [(actual_table, col)].
    For a complex expression like "t.col1 + s.col2", returns both.
    For unqualified "col", searches all tables in scope.
    """
    if not SQLGLOT_AVAILABLE:
        return []

    sources = []

    for col_node in expr_node.find_all(exp.Column):
        col_name = col_node.name.upper()
        col_table_ref = col_node.table.upper() if col_node.table else ""

        resolved = _resolve_one_column(col_name, col_table_ref, scope, registry)
        sources.extend(resolved)

    return sources


def _resolve_one_column(col_name: str, col_table_ref: str,
                         scope: dict, registry: TableRegistry
                         ) -> list[tuple[str, str]]:
    """
    Resolve a single column reference.

    Case 1: Qualified (col_table_ref given) → alias lookup → done
    Case 2: Unqualified → search all tables in scope:
      - Derived table has this column in its map → match
      - Base table → could have it (we don't know the schema)
      - If exactly one derived table has it → use that
      - Otherwise → list all base tables as ambiguous
    """
    if col_table_ref:
        actual_table = scope.get(col_table_ref, col_table_ref)
        return [(actual_table, col_name)]

    # Unqualified: search scope
    # First, check derived tables — they have known column sets
    derived_matches = []
    base_tables_in_scope = []

    for alias, actual_table in scope.items():
        if registry.is_derived(actual_table):
            derived_cols = registry.get_derived_columns(actual_table)
            if col_name in derived_cols:
                derived_matches.append((actual_table, col_name))
        elif registry.is_base(actual_table):
            base_tables_in_scope.append((actual_table, col_name))

    if len(derived_matches) == 1:
        return derived_matches
    elif derived_matches:
        # Multiple derived tables have this column — ambiguous
        logger.debug(f"    Ambiguous (derived): {col_name} in {derived_matches}")
        return derived_matches

    # No derived table match — must come from a base table
    if len(base_tables_in_scope) == 1:
        return base_tables_in_scope
    elif base_tables_in_scope:
        logger.debug(f"    Ambiguous (base): {col_name} in {base_tables_in_scope}")
        return base_tables_in_scope

    return [("UNRESOLVED", col_name)]


def _collect_non_select_columns(parsed, registry: TableRegistry,
                                 block_result: dict, seen: set):
    """
    Collect column references from WHERE, JOIN ON, GROUP BY, HAVING, ORDER BY.
    These columns indicate base table usage even if not in the SELECT list.
    """
    # Build global alias map from ALL Table nodes
    global_scope = {}
    for table_node in parsed.find_all(exp.Table):
        tname = table_node.name.upper()
        talias = table_node.alias.upper() if table_node.alias else tname
        global_scope[talias] = tname
        if not registry.is_known(tname):
            registry.register_base(tname)

    # Walk ALL Column nodes
    for col_node in parsed.find_all(exp.Column):
        col_name = col_node.name.upper()
        col_table_ref = col_node.table.upper() if col_node.table else ""

        if not col_table_ref:
            continue  # Skip unqualified — already handled in SELECT resolution

        actual_table = global_scope.get(col_table_ref, col_table_ref)

        for base_tbl, base_col in registry.resolve(actual_table, col_name):
            pair = (base_tbl, base_col)
            if pair not in seen:
                seen.add(pair)
                block_result["columns"].append({
                    "base_table": base_tbl,
                    "column": base_col,
                })


# ===========================================================================
# Phase 4: Output — JSON + DataFrame
# ===========================================================================

def generate_report(block_results: list[dict], registry: TableRegistry,
                    output_path: str) -> dict:
    all_base_tables = registry.get_base_tables()

    all_columns = set()
    for br in block_results:
        for c in br["columns"]:
            if registry.is_base(c["base_table"]):
                all_columns.add((c["base_table"], c["column"]))

    report = {
        "summary": {
            "total_blocks": len(block_results),
            "total_base_tables": len(all_base_tables),
            "total_base_columns": len(all_columns),
            "base_tables": all_base_tables,
        },
        "blocks": [],
    }

    for br in block_results:
        report["blocks"].append({
            "block_index": br["block_index"],
            "insert_target": br["insert_target"],
            "ctes": br["ctes_found"],
            "base_tables_referenced": br["base_tables_found"],
            "columns": br["columns"],
            "parse_errors": br["parse_errors"],
        })

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    logger.info(f"JSON report written to {output_path}")
    return report


def build_dataframe(block_results: list[dict], registry: TableRegistry):
    """
    Build a DataFrame: one row per unique (base_table, column).

    Columns:
    - base_table     : the ultimate source base table
    - column         : column name
    - first_seen_in  : block_index where first encountered
    - referenced_in  : list of block indices that reference this column
    """
    col_info: dict[tuple[str, str], dict] = {}

    for br in block_results:
        for c in br["columns"]:
            bt = c["base_table"]
            col = c["column"]
            if not registry.is_base(bt):
                continue
            key = (bt, col)
            if key not in col_info:
                col_info[key] = {
                    "base_table": bt,
                    "column": col,
                    "first_seen_in": br["block_index"],
                    "referenced_in": [],
                }
            col_info[key]["referenced_in"].append(br["block_index"])

    records = list(col_info.values())
    for r in records:
        r["referenced_in"] = sorted(set(r["referenced_in"]))

    if not PANDAS_AVAILABLE:
        logger.warning("pandas not installed — returning list of dicts")
        return records

    df = pd.DataFrame(records)
    if df.empty:
        return df

    return df.sort_values(["base_table", "column"]).reset_index(drop=True)


# ===========================================================================
# Main
# ===========================================================================

def analyze_file(input_path: str, output_path: str = "analysis_results.json"):
    """
    Full pipeline. Returns (report_dict, DataFrame).
    """
    logger.info(f"Reading {input_path}")
    text = Path(input_path).read_text(encoding='utf-8', errors='replace')

    # Phase 1: Extract
    raw_blocks = extract_execute_immediate_blocks(text)
    if not raw_blocks:
        logger.warning("No EXECUTE IMMEDIATE blocks found!")
        logger.info("Attempting to parse entire file as plain SQL...")
        raw_blocks = [text]

    # Shared registry across all blocks
    registry = TableRegistry()
    block_results = []

    for i, raw_sql in enumerate(raw_blocks):
        logger.info(f"Processing block {i + 1}/{len(raw_blocks)}")

        # Phase 2: Clean
        cleaned = clean_sql_for_parsing(raw_sql)
        logger.debug(f"  Cleaned (first 300): {cleaned[:300]}")

        # Phase 3: Walk parse tree, build registry
        res = process_block(cleaned, registry, block_index=i + 1)
        block_results.append(res)

        if res["parse_errors"]:
            for err in res["parse_errors"]:
                logger.warning(f"  Block {i + 1}: {err}")
        else:
            logger.info(
                f"  Block {i + 1}: "
                f"{len(res['base_tables_found'])} base tables, "
                f"{len(res['ctes_found'])} CTEs, "
                f"{len(res['columns'])} resolved columns"
                + (f", INSERT INTO {res['insert_target']}"
                   if res['insert_target'] else "")
            )

    # Phase 4: Output
    report = generate_report(block_results, registry, output_path)
    df = build_dataframe(block_results, registry)

    print(f"\n{'=' * 60}")
    print(f"  Analysis Complete")
    print(f"{'=' * 60}")
    print(f"  Blocks analyzed:    {len(block_results)}")
    print(f"  Base tables:        {registry.get_base_tables()}")
    total_cols = sum(len(br['columns']) for br in block_results)
    print(f"  Resolved columns:   {total_cols}")
    print(f"  JSON report:        {output_path}")
    print(f"{'=' * 60}\n")

    return report, df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Analyze Oracle PL/SQL scripts for base tables and columns"
    )
    parser.add_argument("input", help="Path to the .sql file")
    parser.add_argument("--output", "-o", default="analysis_results.json",
                        help="Output JSON file (default: analysis_results.json)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    analyze_file(args.input, args.output)
