"""
Oracle PL/SQL EXECUTE IMMEDIATE Analyzer
=========================================
Reads Oracle PL/SQL scripts, extracts SQL from EXECUTE IMMEDIATE strings,
parses them via sqlglot, and reports base tables + columns used.

Handles:
- Multiple EXECUTE IMMEDIATE blocks per file
- || concatenation at PL/SQL level (variable interpolation)
- || concatenation inside SQL (Oracle string concat) — preserved correctly
- Escaped quotes ('') inside string literals
- SQL comments (-- and /* */) inside the query
- Oracle hints (/*+ ... */)
- REGEXP patterns with special characters
- CTE (WITH ... AS) resolution back to base tables
- INSERT INTO ... WITH ... SELECT patterns

Output: JSON file + DataFrame for notebook use.

Usage (CLI):
    python oracle_sql_analyzer.py <input.sql> [--output results.json]

Usage (notebook):
    from oracle_sql_analyzer import analyze_file
    report, df = analyze_file("my_script.sql")
    df  # displays the DataFrame
"""

import re
import json
import argparse
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
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


# ---------------------------------------------------------------------------
# Phase 1: Extract EXECUTE IMMEDIATE strings from PL/SQL
# ---------------------------------------------------------------------------

def extract_execute_immediate_blocks(plsql_text: str) -> list[str]:
    """
    Find all EXECUTE IMMEDIATE '...' blocks and return the reconstructed SQL.

    Strategy:
    ---------
    The EXECUTE IMMEDIATE statement in PL/SQL builds a SQL string via:
        EXECUTE IMMEDIATE '<sql_frag>' || plsql_expr || '<sql_frag>' ...

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
        # Skip if inside a single-line comment
        line_start = text.rfind('\n', 0, match.start()) + 1
        line_prefix = text[line_start:match.start()]
        if '--' in line_prefix:
            continue

        # Skip if inside a block comment
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
    """
    Parse the full EXECUTE IMMEDIATE statement starting at pos.

    Tokenizes at the PL/SQL level into STRING / CONCAT / EXPR / END,
    then reassembles: string content kept as SQL, expressions replaced
    with placeholders.
    """
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
    """
    Tokenize the RHS of EXECUTE IMMEDIATE into typed tokens.
    """
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
    """
    Parse a PL/SQL string literal starting at pos (must be ').
    '' inside becomes ' (PL/SQL escape rule).
    Everything else is raw SQL content — preserved verbatim.
    """
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
    """
    Parse a PL/SQL interpolated expression between || operators.
    Respects parentheses and string literals inside function calls.
    """
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
    """
    Convert a PL/SQL interpolated expression into a SQL-safe placeholder.
    Returns a bare value (no wrapping quotes) — the surrounding SQL string
    fragments provide whatever quoting context is needed.
    """
    expr_upper = expr.upper().strip()

    if 'TO_CHAR' in expr_upper:
        return '01-JAN-25'
    if 'TO_DATE' in expr_upper:
        return '01-JAN-25'

    # Functions that return numbers
    if any(fn in expr_upper for fn in [
        'LENGTH', 'REGEXP', 'NVL', 'COALESCE', 'DECODE',
        'GREATEST', 'LEAST', 'ABS', 'ROUND', 'TRUNC',
        'MOD', 'CEIL', 'FLOOR', 'SUBSTR', 'INSTR',
    ]):
        return '0'

    # Numeric variable heuristic
    numeric_hints = [
        'AMT', 'NUM', 'COUNT', 'ID', 'LIMIT', 'MIN', 'MAX',
        'THRESHOLD', 'DAYS', 'FREQ', 'PERIOD', 'IND', 'AMOUNT',
        'SCORE', 'PCT', 'RATE', 'QTY', 'SIZE', 'LEN', 'SCENARIO',
    ]
    if any(hint in expr_upper for hint in numeric_hints):
        return '0'

    # Simple identifier — default numeric
    if '(' not in expr and re.match(r'^[A-Za-z_][A-Za-z0-9_.]*$', expr.strip()):
        return '0'

    return 'PLACEHOLDER'


# ---------------------------------------------------------------------------
# Phase 2: Clean up the extracted SQL for parsing
# ---------------------------------------------------------------------------

def _detect_insert_target(raw_sql: str) -> Optional[str]:
    """Extract the INSERT INTO target table name (if present)."""
    m = re.match(r'(?i)^\s*INSERT\s+INTO\s+(\S+)', raw_sql.strip())
    return m.group(1) if m else None


def clean_sql_for_parsing(raw_sql: str) -> str:
    """
    Clean extracted SQL for sqlglot parsing.
    - Strip INSERT INTO <table> before WITH/SELECT (for parseability)
    - Remove Oracle hints /*+ ... */
    - Remove SQL comments
    - Normalize whitespace
    """
    sql = raw_sql.strip().rstrip(';').strip()

    # Strip INSERT INTO <table> (with optional column list)
    sql = re.sub(
        r'(?i)^\s*INSERT\s+INTO\s+\S+\s*(\([^)]*\))?\s*(?=WITH\b|SELECT\b)',
        '', sql
    ).strip()

    # Remove Oracle optimizer hints /*+ ... */
    sql = re.sub(r'/\*\+[^*]*?\*/', '', sql)

    # Remove single-line SQL comments
    sql = re.sub(r'--[^\n]*', '', sql)

    # Remove block comments (non-greedy)
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)

    # Normalize whitespace
    sql = re.sub(r'\s+', ' ', sql).strip()

    return sql


# ---------------------------------------------------------------------------
# Phase 3: Parse with sqlglot — let it do the heavy lifting
# ---------------------------------------------------------------------------
#
# Architecture:
#   sqlglot parses the SQL and gives us the AST.
#   We use sqlglot's scope analysis where possible for alias resolution.
#   We build our own lineage graph on top for:
#     - CTE chain resolution (CTE_B -> CTE_A -> base_table)
#     - Cross-block INSERT INTO tracking
#     - Final flattening to (base_table, column) leaf nodes
#
# The lineage graph:
#   Nodes are (table_name_upper, column_name_upper)
#   Edges point from derived -> source
#   Base tables are leaf nodes (no outgoing edges)
#   CTEs and INSERT INTO targets are intermediate nodes
# ---------------------------------------------------------------------------

@dataclass
class BlockInfo:
    """Raw parsed info from a single EXECUTE IMMEDIATE block."""
    block_index: int
    raw_sql: str
    cleaned_sql: str
    insert_target: Optional[str]          # e.g., 'temp_alert_staging'
    cte_definitions: dict                  # CTE_NAME -> {select_columns, source_tables}
    base_tables: dict                      # TABLE_NAME -> {schema, aliases}
    alias_to_table: dict                   # alias -> TABLE_NAME (base tables + CTEs)
    select_columns: list[dict]             # [{column, table_ref, in_cte}]
    parse_errors: list[str] = field(default_factory=list)


def parse_block(cleaned_sql: str, raw_sql: str, block_index: int,
                insert_target: Optional[str]) -> BlockInfo:
    """
    Use sqlglot to parse a single SQL block and extract structured info.
    """
    info = BlockInfo(
        block_index=block_index,
        raw_sql=raw_sql,
        cleaned_sql=cleaned_sql,
        insert_target=insert_target.upper() if insert_target else None,
        cte_definitions={},
        base_tables={},
        alias_to_table={},
        select_columns=[],
    )

    if not SQLGLOT_AVAILABLE:
        info.parse_errors.append("sqlglot not installed — pip install sqlglot")
        return info

    # --- Parse ---
    try:
        parsed = sqlglot.parse_one(cleaned_sql, read="oracle")
    except Exception as e:
        info.parse_errors.append(f"sqlglot parse error: {e}")
        logger.warning(f"Block {block_index}: parse error — {e}")
        try:
            parsed = sqlglot.parse_one(
                cleaned_sql, read="oracle",
                error_level=sqlglot.ErrorLevel.WARN
            )
        except Exception as e2:
            info.parse_errors.append(f"Lenient parse also failed: {e2}")
            return info

    # --- Collect CTE names first (needed to distinguish from base tables) ---
    cte_names = set()
    for cte_node in parsed.find_all(exp.CTE):
        cname = cte_node.alias
        if cname:
            cte_names.add(cname.upper())

    # --- Walk all Table nodes: build alias map and base table set ---
    for table_node in parsed.find_all(exp.Table):
        tname = table_node.name
        tname_upper = tname.upper()
        schema = table_node.db if hasattr(table_node, 'db') and table_node.db else ""
        alias = table_node.alias if table_node.alias else tname

        # Register in alias map (alias -> what it refers to)
        info.alias_to_table[alias.upper()] = tname_upper

        if tname_upper in cte_names:
            # CTE reference, not a base table — alias still maps to CTE name
            continue

        # It's a base table (or temp table from a previous block — resolved later)
        if tname_upper not in info.base_tables:
            info.base_tables[tname_upper] = {
                "name": tname,
                "schema": schema,
                "aliases": set(),
            }
        info.base_tables[tname_upper]["aliases"].add(alias.upper())

    # --- Parse CTE definitions: what columns each CTE exposes ---
    for cte_node in parsed.find_all(exp.CTE):
        cte_alias = cte_node.alias
        if not cte_alias:
            continue
        cte_name = cte_alias.upper()

        cte_body = cte_node.this
        cte_info = {"columns": {}, "source_tables": set()}

        # Find all tables referenced in this CTE's body
        for t in cte_body.find_all(exp.Table):
            ref_name = t.name.upper()
            ref_alias = t.alias.upper() if t.alias else ref_name
            cte_info["source_tables"].add(ref_name)
            # Also register local alias within CTE scope
            info.alias_to_table[ref_alias] = ref_name

        # Find all columns in the CTE's body and record their source table ref
        for col in cte_body.find_all(exp.Column):
            col_name = col.name.upper()
            col_table_ref = col.table.upper() if col.table else ""

            # Resolve the table ref through alias map
            resolved_ref = info.alias_to_table.get(col_table_ref, col_table_ref)

            # Store: this CTE column came from this source
            # (could be base table or another CTE)
            if col_name not in cte_info["columns"]:
                cte_info["columns"][col_name] = resolved_ref

        info.cte_definitions[cte_name] = cte_info

    # --- Collect all Column references in the full query ---
    for col in parsed.find_all(exp.Column):
        col_name = col.name.upper()
        col_table_ref = col.table.upper() if col.table else ""

        # Resolve alias -> actual table/CTE name
        resolved_ref = info.alias_to_table.get(col_table_ref, col_table_ref)

        # Determine if this column is inside a CTE definition or in the main query
        # (by checking parent chain for CTE nodes)
        in_cte = None
        parent = col.parent
        while parent:
            if isinstance(parent, exp.CTE):
                in_cte = parent.alias.upper() if parent.alias else None
                break
            parent = parent.parent if hasattr(parent, 'parent') else None

        info.select_columns.append({
            "column": col_name,
            "table_ref_raw": col.table.upper() if col.table else "",
            "table_ref_resolved": resolved_ref,
            "in_cte": in_cte,
        })

    return info


# ---------------------------------------------------------------------------
# Phase 3b: Lineage graph — cross-CTE and cross-block resolution
# ---------------------------------------------------------------------------

class LineageGraph:
    """
    Tracks column lineage across CTEs and EXECUTE IMMEDIATE blocks.

    Nodes: (TABLE_NAME, COLUMN_NAME) tuples
    Edges: derived_node -> source_node

    After building, we can walk any node to its leaf (base table) source.

    Also tracks INSERT INTO: if block 1 does INSERT INTO temp SELECT col FROM base,
    then temp.col -> base.col, so block 2 reading temp.col resolves to base.col.
    """

    def __init__(self):
        # edges[derived] = source  (single parent lineage, last-write-wins)
        self.edges: dict[tuple[str, str], tuple[str, str]] = {}
        # Known base tables (leaf nodes — not CTEs, not INSERT targets)
        self.base_tables: set[str] = set()
        # INSERT INTO targets: target_table -> {col -> (source_table, col)}
        self.insert_targets: dict[str, dict[str, tuple[str, str]]] = {}
        # All table metadata
        self.table_meta: dict[str, dict] = {}  # TABLE -> {schema, aliases}

    def add_block(self, info: BlockInfo):
        """Integrate one parsed block into the lineage graph."""

        # Register base tables
        for tname, meta in info.base_tables.items():
            self.base_tables.add(tname)
            if tname not in self.table_meta:
                self.table_meta[tname] = {
                    "schema": meta["schema"],
                    "aliases": meta["aliases"].copy(),
                }
            else:
                self.table_meta[tname]["aliases"].update(meta["aliases"])

        # Build CTE lineage edges
        for cte_name, cte_info in info.cte_definitions.items():
            for col_name, source_table in cte_info["columns"].items():
                derived = (cte_name, col_name)
                source = (source_table, col_name)
                self.edges[derived] = source

        # Handle INSERT INTO: record that target_table.col -> source
        if info.insert_target:
            target = info.insert_target
            logger.debug(f"  Block {info.block_index}: INSERT INTO {target}")

            # Remove from base_tables if it was previously added
            # (it's a derived table, not a true base)
            self.base_tables.discard(target)

            # Build column mapping: for columns in the main SELECT (not in CTEs),
            # trace each back to its source
            if target not in self.insert_targets:
                self.insert_targets[target] = {}

            for col_info in info.select_columns:
                if col_info["in_cte"] is not None:
                    continue  # Skip columns inside CTE definitions

                col_name = col_info["column"]
                source_ref = col_info["table_ref_resolved"]

                if source_ref:
                    self.insert_targets[target][col_name] = (source_ref, col_name)
                    # Also add an edge so resolve_to_base can follow it
                    self.edges[(target, col_name)] = (source_ref, col_name)

    def resolve_to_base(self, table: str, column: str,
                        _visited: set = None) -> tuple[str, str]:
        """
        Follow lineage edges until we reach a base table (leaf node).
        Returns (base_table, column).
        """
        if _visited is None:
            _visited = set()

        key = (table.upper(), column.upper())
        if key in _visited:
            logger.debug(f"  Cycle detected at {key}")
            return key  # Cycle — return as-is
        _visited.add(key)

        # If it's already a base table, we're done
        if table.upper() in self.base_tables:
            return (table.upper(), column.upper())

        # Check INSERT INTO targets
        if table.upper() in self.insert_targets:
            mapping = self.insert_targets[table.upper()]
            if column.upper() in mapping:
                src_table, src_col = mapping[column.upper()]
                return self.resolve_to_base(src_table, src_col, _visited)

        # Check lineage edges (CTE chains)
        if key in self.edges:
            src_table, src_col = self.edges[key]
            return self.resolve_to_base(src_table, src_col, _visited)

        # Couldn't resolve — return as-is
        return key

    def is_base_table(self, table: str) -> bool:
        return table.upper() in self.base_tables


# ---------------------------------------------------------------------------
# Phase 4: Flatten to final (base_table, column) pairs + output
# ---------------------------------------------------------------------------

def build_final_results(blocks: list[BlockInfo], lineage: LineageGraph):
    """
    Walk all column references, resolve each to a base table via lineage,
    and produce the final deduplicated (base_table, column) records.
    """
    records = []
    seen = set()

    for info in blocks:
        for col_info in info.select_columns:
            col_name = col_info["column"]
            raw_ref = col_info["table_ref_raw"]
            resolved_ref = col_info["table_ref_resolved"]

            # Skip if no table context at all
            source = resolved_ref if resolved_ref else raw_ref
            if not source:
                # Column with no table qualifier — can't resolve
                key = ("?", col_name.upper())
                if key not in seen:
                    seen.add(key)
                    records.append({
                        "block_index": info.block_index,
                        "base_table": "UNRESOLVED",
                        "schema": "",
                        "column": col_name,
                        "original_ref": raw_ref,
                        "resolved_via": "unresolved",
                    })
                continue

            # Resolve through lineage graph to base table
            base_table, base_col = lineage.resolve_to_base(source, col_name)

            # Determine resolution path
            if lineage.is_base_table(base_table):
                resolved_via = "direct"
                if source != base_table:
                    # It went through a CTE or INSERT INTO
                    resolved_via = "cte" if source in {
                        cte for b in blocks for cte in b.cte_definitions
                    } else "insert_into"
            else:
                resolved_via = "unresolved"

            meta = lineage.table_meta.get(base_table, {})
            schema = meta.get("schema", "")

            key = (base_table, base_col)
            if key not in seen:
                seen.add(key)
                records.append({
                    "block_index": info.block_index,
                    "base_table": base_table,
                    "schema": schema,
                    "column": base_col,
                    "original_ref": raw_ref,
                    "resolved_via": resolved_via,
                })

    return records


def generate_report(records: list[dict], blocks: list[BlockInfo],
                    lineage: LineageGraph, output_path: str) -> dict:
    """Write full analysis to JSON."""
    base_tables_set = sorted(lineage.base_tables)
    base_cols = {r["column"] for r in records if r["base_table"] != "UNRESOLVED"}

    report = {
        "summary": {
            "total_blocks": len(blocks),
            "total_base_tables": len(base_tables_set),
            "total_columns": len(base_cols),
            "base_tables": base_tables_set,
        },
        "lineage": {
            "insert_into_targets": {
                k: {col: list(src) for col, src in v.items()}
                for k, v in lineage.insert_targets.items()
            },
            "cte_chains": {
                bname: {
                    "source_tables": sorted(binfo.cte_definitions.get(cte, {}).get("source_tables", set()))
                    for cte in binfo.cte_definitions
                }
                for binfo in blocks
                for bname in [f"block_{binfo.block_index}"]
            },
        },
        "columns": records,
    }

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, default=_json_default)

    logger.info(f"JSON report written to {output_path}")
    return report


def _json_default(obj):
    """Handle sets and other non-serializable types."""
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def build_dataframe(records: list[dict]):
    """
    Build a DataFrame with one row per unique (base_table, column) pair.

    Columns:
    - base_table   : the ultimate source base table
    - schema       : schema if known
    - column       : column name on that base table
    - block_index  : which block first referenced it
    - original_ref : the alias/table ref as written in the SQL
    - resolved_via : 'direct' | 'cte' | 'insert_into' | 'unresolved'
    """
    if not PANDAS_AVAILABLE:
        logger.warning("pandas not installed — returning list of dicts")
        return records

    df = pd.DataFrame(records)
    if df.empty:
        return df

    df = df.sort_values(['base_table', 'column', 'block_index']).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze_file(input_path: str, output_path: str = "analysis_results.json"):
    """
    Full pipeline: read -> extract -> clean -> parse -> resolve lineage -> report.

    Returns:
        (report_dict, DataFrame)
    """
    logger.info(f"Reading {input_path}")
    text = Path(input_path).read_text(encoding='utf-8', errors='replace')

    # Phase 1: Extract
    raw_blocks = extract_execute_immediate_blocks(text)

    if not raw_blocks:
        logger.warning("No EXECUTE IMMEDIATE blocks found!")
        logger.info("Attempting to parse entire file as plain SQL...")
        raw_blocks = [text]

    # Phase 2+3: Clean, parse each block, detect INSERT INTO targets
    parsed_blocks = []
    for i, raw_sql in enumerate(raw_blocks):
        logger.info(f"Processing block {i + 1}/{len(raw_blocks)}")

        insert_target = _detect_insert_target(raw_sql)
        if insert_target:
            logger.info(f"  INSERT INTO target: {insert_target}")

        cleaned = clean_sql_for_parsing(raw_sql)
        logger.debug(f"  Cleaned SQL (first 300 chars): {cleaned[:300]}")

        info = parse_block(cleaned, raw_sql, block_index=i + 1,
                           insert_target=insert_target)
        parsed_blocks.append(info)

        if info.parse_errors:
            for err in info.parse_errors:
                logger.warning(f"  Block {i + 1}: {err}")
        else:
            logger.info(
                f"  Block {i + 1}: {len(info.base_tables)} base tables, "
                f"{len(info.cte_definitions)} CTEs, "
                f"{len(info.select_columns)} column refs"
            )

    # Phase 3b: Build lineage graph across all blocks
    lineage = LineageGraph()
    for info in parsed_blocks:
        lineage.add_block(info)

    logger.info(f"Lineage graph: {len(lineage.base_tables)} base tables, "
                f"{len(lineage.insert_targets)} INSERT targets, "
                f"{len(lineage.edges)} edges")

    # Phase 4: Flatten to base table columns
    records = build_final_results(parsed_blocks, lineage)
    report = generate_report(records, parsed_blocks, lineage, output_path)
    df = build_dataframe(records)

    # Console summary
    base_only = [r for r in records if r["base_table"] != "UNRESOLVED"]
    unresolved = [r for r in records if r["base_table"] == "UNRESOLVED"]

    print(f"\n{'=' * 60}")
    print(f"  Analysis Complete")
    print(f"{'=' * 60}")
    print(f"  Blocks analyzed:    {len(parsed_blocks)}")
    print(f"  Base tables:        {sorted(lineage.base_tables)}")
    print(f"  Resolved columns:   {len(base_only)}")
    if unresolved:
        print(f"  Unresolved columns: {len(unresolved)}")
    if lineage.insert_targets:
        print(f"  INSERT INTO targets tracked: {sorted(lineage.insert_targets.keys())}")
    print(f"  JSON report: {output_path}")
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
