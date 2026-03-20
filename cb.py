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
# Phase 3: Scope-based resolution using sqlglot.optimizer.scope
# ===========================================================================
#
# Key tools from sqlglot:
#   traverse_scope(parsed) — yields Scope objects in DFS post-order
#       (children first: CTEs before main SELECT, subqueries before outer)
#   scope.sources — {alias: Table | Scope} — what's in FROM/JOIN
#   scope.columns — Column nodes in THIS scope only (not nested subqueries)
#   scope.expression.selects — the SELECT projection list
#   scope.expression.named_selects — output column names
#   scope.scope_type — ROOT, CTE, SUBQUERY, DERIVED_TABLE, UNION
#
# Our TableRegistry handles cross-block concerns only:
#   INSERT INTO targets from block N become available in block N+1
#
# Within a single block, traverse_scope does all the heavy lifting.

try:
    from sqlglot.optimizer.scope import traverse_scope, Scope, ScopeType
    SCOPE_AVAILABLE = True
except ImportError:
    SCOPE_AVAILABLE = False


class TableRegistry:
    """
    Cross-block table registry.
    Tracks INSERT INTO targets so later blocks can resolve through them.
    Also accumulates the final set of base tables seen across all blocks.
    """

    def __init__(self):
        # base_table_upper -> set of column names seen
        self._base_tables: dict = {}
        # derived_table_upper -> {col_upper: [(base_table, base_col), ...]}
        self._derived_tables: dict = {}

    def register_base_column(self, table: str, column: str):
        t = table.upper()
        c = column.upper()
        if t not in self._base_tables:
            self._base_tables[t] = set()
        self._base_tables[t].add(c)

    def register_derived(self, name: str, column_map: dict):
        """Register a CTE or INSERT INTO target with its column lineage."""
        self._derived_tables[name.upper()] = column_map

    def is_derived(self, name: str) -> bool:
        return name.upper() in self._derived_tables

    def get_derived_columns(self, name: str) -> dict:
        return self._derived_tables.get(name.upper(), {})

    def is_base(self, name: str) -> bool:
        return name.upper() in self._base_tables and not self.is_derived(name)

    def get_base_tables(self) -> list:
        # Base tables are those in _base_tables but NOT overridden as derived
        return sorted(t for t in self._base_tables if t not in self._derived_tables)

    def resolve(self, table: str, column: str,
                _visited: Optional[set] = None) -> list:
        """Resolve (table, column) to base table source(s) via derived table chain."""
        if _visited is None:
            _visited = set()
        key = (table.upper(), column.upper())
        if key in _visited:
            return [key]
        _visited.add(key)

        t_upper = table.upper()
        if t_upper not in self._derived_tables:
            # It's a base table (or unknown — treat as base)
            self.register_base_column(t_upper, column)
            return [(t_upper, column.upper())]

        col_map = self._derived_tables[t_upper]
        sources = col_map.get(column.upper())
        if not sources:
            return [(t_upper, column.upper())]

        results = []
        for src_tbl, src_col in sources:
            results.extend(self.resolve(src_tbl, src_col, _visited.copy()))
        return results if results else [key]


def process_block(cleaned_sql: str, registry: TableRegistry,
                  block_index: int) -> dict:
    """
    Parse one SQL block using sqlglot + traverse_scope.
    Returns a result dict with all column lineage info.
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
    if not SCOPE_AVAILABLE:
        result["parse_errors"].append("sqlglot.optimizer.scope not available")
        return result

    # --- Parse ---
    try:
        parsed = sqlglot.parse_one(cleaned_sql, read="oracle")
    except Exception as e:
        result["parse_errors"].append(f"Parse error: {e}")
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

    # --- Traverse scopes (post-order: CTEs first, then main SELECT) ---
    try:
        scopes = traverse_scope(parsed)
    except Exception as e:
        result["parse_errors"].append(f"traverse_scope error: {e}")
        return result

    # For each scope, build a column map: {output_col: [(source_table, source_col)]}
    # We register CTE scopes in the registry so downstream scopes can resolve through them.
    scope_col_maps = {}  # id(scope) -> column_map
    main_scope = None

    for scope in scopes:
        scope_type = scope.scope_type
        col_map = _process_scope(scope, registry, scope_col_maps)
        scope_col_maps[id(scope)] = col_map

        # Register CTEs in the registry
        if scope_type == ScopeType.CTE:
            cte_name = scope.expression.parent.alias
            if cte_name:
                cte_name_upper = cte_name.upper()
                result["ctes_found"].append(cte_name_upper)
                registry.register_derived(cte_name_upper, col_map)
                logger.debug(f"  CTE [{cte_name_upper}]: {list(col_map.keys())}")

        # Track the root scope
        if scope_type == ScopeType.ROOT:
            main_scope = scope

    # --- Build final column list from the root scope ---
    if main_scope is not None:
        main_col_map = scope_col_maps.get(id(main_scope), {})

        # If INSERT INTO, register the target as derived
        if insert_target:
            registry.register_derived(insert_target, main_col_map)

        # Resolve all columns to base tables
        seen = set()
        for output_col, sources in main_col_map.items():
            for src_tbl, src_col in sources:
                for base_tbl, base_col in registry.resolve(src_tbl, src_col):
                    _add_column_record(result, base_tbl, base_col,
                                       output_col, src_tbl, "SELECT",
                                       registry, seen)

        # Collect columns from WHERE, JOIN ON, GROUP BY, etc. in the root scope
        _collect_filter_columns(main_scope, registry, result, seen,
                                scope_col_maps)

    result["base_tables_found"] = sorted({
        c["base_table"] for c in result["columns"]
        if registry.is_base(c["base_table"])
    })

    return result


def _process_scope(scope, registry: TableRegistry,
                   scope_col_maps: dict) -> dict:
    """
    Process one Scope: resolve each SELECT column to its source table.
    Returns {OUTPUT_COL_UPPER: [(source_table, source_col), ...]}
    """
    col_map = {}
    select_node = scope.expression

    if not isinstance(select_node, exp.Select):
        return col_map

    # Build source lookup: alias -> (table_name, is_physical)
    # scope.sources gives us {alias: Table | Scope}
    source_info = {}  # alias_upper -> (actual_table_upper, is_scope)
    for alias_name, source in scope.sources.items():
        alias_upper = alias_name.upper()
        if isinstance(source, exp.Table):
            tname = source.name.upper()
            source_info[alias_upper] = (tname, False)
            # Register as base table if not already derived
            if not registry.is_derived(tname):
                registry.register_base_column(tname, "")  # just register the table
        elif isinstance(source, Scope):
            # CTE or subquery — get its column map
            sub_col_map = scope_col_maps.get(id(source), {})
            # The "table name" for resolution is the alias
            source_info[alias_upper] = (alias_upper, True)

    # Process each SELECT expression
    for sel_expr in select_node.selects:
        _process_select_expr(sel_expr, source_info, scope, registry,
                             scope_col_maps, col_map)

    return col_map


def _process_select_expr(sel_expr, source_info, scope, registry,
                         scope_col_maps, col_map):
    """Process a single SELECT expression and add to col_map."""

    # Handle sa.* (Star with table qualifier)
    if isinstance(sel_expr, exp.Star):
        _expand_star(sel_expr, None, source_info, scope, scope_col_maps,
                     registry, col_map)
        return

    # Handle t.* inside a Column-like context
    if isinstance(sel_expr, exp.Column) and isinstance(sel_expr.this, exp.Star):
        table_ref = sel_expr.table.upper() if sel_expr.table else None
        _expand_star(sel_expr, table_ref, source_info, scope, scope_col_maps,
                     registry, col_map)
        return

    # Determine output name
    if isinstance(sel_expr, exp.Alias):
        output_name = sel_expr.alias.upper()
    elif isinstance(sel_expr, exp.Column):
        output_name = sel_expr.name.upper()
    else:
        # Expression like count(c) without alias
        col = sel_expr.find(exp.Column)
        if col:
            output_name = col.name.upper()
        else:
            return  # Pure literal, skip

    # Find all Column references in this expression
    sources = []
    for col_node in sel_expr.find_all(exp.Column):
        if isinstance(col_node.this, exp.Star):
            continue  # handled separately
        col_name = col_node.name.upper()
        col_table_ref = col_node.table.upper() if col_node.table else ""

        resolved = _resolve_column_in_scope(
            col_name, col_table_ref, source_info, scope, registry,
            scope_col_maps
        )
        sources.extend(resolved)

    if sources:
        col_map[output_name] = sources


def _expand_star(node, table_ref, source_info, scope, scope_col_maps,
                 registry, col_map):
    """
    Expand SELECT * or SELECT t.* by looking up the source's known columns.
    """
    if table_ref:
        # t.* — expand columns from one specific source
        targets = {table_ref: source_info.get(table_ref)}
    else:
        # * — expand from all sources
        targets = source_info

    for alias_upper, info in targets.items():
        if info is None:
            continue
        actual_table, is_scope = info

        if is_scope:
            # Look up the scope's column map to get its output columns
            for src_alias, source in scope.sources.items():
                if src_alias.upper() == alias_upper and isinstance(source, Scope):
                    sub_map = scope_col_maps.get(id(source), {})
                    for sub_col, sub_sources in sub_map.items():
                        col_map[sub_col] = [(alias_upper, sub_col)]
                    break
        else:
            # Physical table — we don't know its columns without schema
            # Register what we can: if we've seen columns for this table before,
            # include them. Otherwise, just note it.
            known_cols = registry._base_tables.get(actual_table, set())
            if known_cols:
                for c in known_cols:
                    if c:  # skip empty string placeholder
                        col_map[c] = [(actual_table, c)]
            else:
                # Mark that we have a star from this table
                logger.debug(f"    SELECT * from base table {actual_table} "
                             f"— columns unknown without schema")


def _resolve_column_in_scope(col_name, col_table_ref, source_info,
                              scope, registry, scope_col_maps):
    """
    Resolve a single column reference within a scope.

    Qualified (t.col): look up alias → source table → done.
    Unqualified (col): check scope sources.
      - Single source → attribute to it.
      - Scope sources with known col → attribute there.
      - Multiple base tables → ambiguous, list all.
    """
    if col_table_ref:
        info = source_info.get(col_table_ref)
        if info:
            actual_table, is_scope = info
            if is_scope:
                # Column from a CTE/subquery — look up in its column map
                for src_alias, source in scope.sources.items():
                    if src_alias.upper() == col_table_ref and isinstance(source, Scope):
                        sub_map = scope_col_maps.get(id(source), {})
                        if col_name in sub_map:
                            return sub_map[col_name]
                        else:
                            # Column not in the scope's explicit map
                            # (might be from SELECT * expansion)
                            return [(col_table_ref, col_name)]
                return [(col_table_ref, col_name)]
            else:
                return [(actual_table, col_name)]
        else:
            # Unknown alias — might be table name directly
            return [(col_table_ref, col_name)]

    # Unqualified column
    if len(source_info) == 1:
        # Only one source — must be from there
        alias_upper, (actual_table, is_scope) = next(iter(source_info.items()))
        if is_scope:
            return [(alias_upper, col_name)]
        else:
            return [(actual_table, col_name)]

    # Multiple sources — try to disambiguate
    # Check scope-type sources (CTEs/subqueries) which have known columns
    scope_matches = []
    base_matches = []
    for alias_upper, (actual_table, is_scope) in source_info.items():
        if is_scope:
            for src_alias, source in scope.sources.items():
                if src_alias.upper() == alias_upper and isinstance(source, Scope):
                    sub_map = scope_col_maps.get(id(source), {})
                    if col_name in sub_map:
                        scope_matches.append((alias_upper, col_name))
        else:
            base_matches.append((actual_table, col_name))

    if len(scope_matches) == 1:
        return scope_matches
    elif scope_matches:
        return scope_matches  # ambiguous among derived

    if len(base_matches) == 1:
        return base_matches

    # Truly ambiguous
    if base_matches:
        return base_matches

    return [("UNRESOLVED", col_name)]


def _collect_filter_columns(scope, registry, result, seen, scope_col_maps):
    """
    Collect column references from WHERE, JOIN ON, GROUP BY, HAVING, ORDER BY
    of the given scope. These are columns used but not necessarily in SELECT.
    """
    source_info = {}
    for alias_name, source in scope.sources.items():
        alias_upper = alias_name.upper()
        if isinstance(source, exp.Table):
            source_info[alias_upper] = (source.name.upper(), False)
        elif isinstance(source, Scope):
            source_info[alias_upper] = (alias_upper, True)

    # scope.columns gives us Column nodes within THIS scope (not nested)
    for col_node in scope.columns:
        if isinstance(col_node.this, exp.Star):
            continue
        col_name = col_node.name.upper()
        col_table_ref = col_node.table.upper() if col_node.table else ""

        # Detect clause
        clause = _detect_clause(col_node)
        if clause == "SELECT":
            continue  # already handled

        resolved = _resolve_column_in_scope(
            col_name, col_table_ref, source_info, scope, registry,
            scope_col_maps
        )

        for src_tbl, src_col in resolved:
            for base_tbl, base_col in registry.resolve(src_tbl, src_col):
                _add_column_record(result, base_tbl, base_col,
                                   col_name, src_tbl, clause,
                                   registry, seen)


def _detect_clause(col_node) -> str:
    """Walk up parent chain to determine which SQL clause a Column is in."""
    node = col_node.parent if hasattr(col_node, 'parent') else None
    while node:
        if isinstance(node, exp.Where):
            return "WHERE"
        if isinstance(node, exp.Group):
            return "GROUP_BY"
        if isinstance(node, exp.Having):
            return "HAVING"
        if isinstance(node, exp.Order):
            return "ORDER_BY"
        if isinstance(node, exp.Join):
            return "JOIN_ON"
        if isinstance(node, exp.Select):
            return "SELECT"
        node = node.parent if hasattr(node, 'parent') else None
    return "OTHER"


def _add_column_record(result, base_tbl, base_col, output_name, source_name,
                        clause, registry, seen):
    """Add a column record to the result, deduplicating."""
    pair_key = (base_tbl, base_col, clause)
    if pair_key in seen:
        return
    seen.add(pair_key)

    # Determine source_type
    if registry.is_base(base_tbl):
        if source_name and source_name != base_tbl:
            source_type = "cte" if registry.is_derived(source_name) else "direct"
        else:
            source_type = "direct"
    elif registry.is_derived(base_tbl):
        source_type = "derived"
    else:
        source_type = "direct"

    is_ambiguous = False  # TODO: could track from _resolve_column_in_scope

    result["columns"].append({
        "base_table": base_tbl,
        "column": base_col,
        "source_type": source_type,
        "source_name": source_name if source_name else base_tbl,
        "output_name": output_name,
        "clause": clause,
        "ambiguous": is_ambiguous,
        "is_base": registry.is_base(base_tbl),
    })


# ===========================================================================
# Phase 4: Output — JSON + DataFrame
# ===========================================================================

def generate_report(block_results: list, registry: TableRegistry,
                    output_path: str) -> dict:
    all_base_tables = registry.get_base_tables()

    all_columns = set()
    for br in block_results:
        for c in br["columns"]:
            if c.get("is_base"):
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


def build_dataframe(block_results: list, registry: TableRegistry):
    """
    Build a rich DataFrame — one row per column reference.

    Columns:
    - block_index   : which EXECUTE IMMEDIATE block
    - base_table    : ultimate source base table
    - column        : column name on the base table
    - source_type   : 'direct' | 'cte' | 'derived'
    - source_name   : immediate table/CTE the column was read from
    - output_name   : column name as it appears in the SELECT output
    - clause        : 'SELECT' | 'WHERE' | 'JOIN_ON' | 'GROUP_BY' | 'HAVING' | 'ORDER_BY'
    - ambiguous     : True if source couldn't be uniquely determined
    - is_base       : True if base_table is a real base table

    Filtering:
        df[df.is_base]                              # only base table columns
        df[df.is_base & (df.clause == 'SELECT')]    # base columns in SELECT
        df[df.ambiguous]                            # needs review
        df[df.source_type == 'cte']                 # went through CTE
        df.groupby('base_table')['column'].apply(set)
    """
    records = []
    for br in block_results:
        for c in br["columns"]:
            records.append({
                "block_index": br["block_index"],
                **c,
            })

    if not PANDAS_AVAILABLE:
        logger.warning("pandas not installed — returning list of dicts")
        return records

    df = pd.DataFrame(records)
    if df.empty:
        return df

    return df.sort_values(
        ["block_index", "base_table", "column", "clause"]
    ).reset_index(drop=True)


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

        cleaned = clean_sql_for_parsing(raw_sql)
        logger.debug(f"  Cleaned (first 300): {cleaned[:300]}")

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

    # Output
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
