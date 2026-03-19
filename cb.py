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

def clean_sql_for_parsing(raw_sql: str) -> str:
    """
    Clean extracted SQL for sqlglot parsing.
    - Strip INSERT INTO ... before WITH/SELECT
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
# Phase 3: Parse with sqlglot
# ---------------------------------------------------------------------------

@dataclass
class QueryAnalysis:
    block_index: int
    raw_sql: str
    cleaned_sql: str
    base_tables: list[dict] = field(default_factory=list)
    columns: list[dict] = field(default_factory=list)
    cte_names: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


def analyze_sql(cleaned_sql: str, block_index: int) -> QueryAnalysis:
    """Parse cleaned SQL and extract base tables and columns."""
    result = QueryAnalysis(
        block_index=block_index,
        raw_sql=cleaned_sql,
        cleaned_sql=cleaned_sql,
    )

    if not SQLGLOT_AVAILABLE:
        result.parse_errors.append("sqlglot not installed — pip install sqlglot")
        return result

    try:
        parsed = sqlglot.parse_one(cleaned_sql, read="oracle")
    except Exception as e:
        result.parse_errors.append(f"sqlglot parse error: {e}")
        logger.warning(f"Block {block_index}: parse error — {e}")
        try:
            parsed = sqlglot.parse_one(
                cleaned_sql, read="oracle",
                error_level=sqlglot.ErrorLevel.WARN
            )
        except Exception as e2:
            result.parse_errors.append(f"Lenient parse also failed: {e2}")
            return result

    # --- CTEs ---
    cte_names = set()
    for cte in parsed.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            cte_names.add(alias.upper())
    result.cte_names = sorted(cte_names)

    # INSERT INTO targets (temp tables, not base tables)
    insert_targets = set()
    for insert in parsed.find_all(exp.Insert):
        tbl = insert.find(exp.Table)
        if tbl:
            insert_targets.add(tbl.name.upper())

    # --- Tables ---
    alias_to_table = {}
    all_tables = []

    for table in parsed.find_all(exp.Table):
        table_name = table.name
        schema_name = table.db if hasattr(table, 'db') and table.db else None
        alias = table.alias if table.alias else table_name

        if table_name.upper() in cte_names:
            continue
        if table_name.upper() in insert_targets:
            continue

        entry = {
            "name": table_name,
            "schema": schema_name or "",
            "alias": alias,
        }
        all_tables.append(entry)
        alias_to_table[alias.upper()] = (schema_name, table_name)
        alias_to_table[table_name.upper()] = (schema_name, table_name)

    seen = set()
    for t in all_tables:
        key = (t["schema"].upper() if t["schema"] else "", t["name"].upper())
        if key not in seen:
            seen.add(key)
            result.base_tables.append(t)

    # --- Columns ---
    cte_column_map = _build_cte_column_map(parsed, cte_names, alias_to_table)

    for col in parsed.find_all(exp.Column):
        col_name = col.name
        table_ref = col.table if col.table else ""

        resolved_table = ""
        if table_ref:
            tr_upper = table_ref.upper()
            if tr_upper in alias_to_table:
                schema, tname = alias_to_table[tr_upper]
                resolved_table = f"{schema + '.' if schema else ''}{tname}"
            elif tr_upper in cte_names:
                base = cte_column_map.get((tr_upper, col_name.upper()))
                resolved_table = base if base else f"CTE:{table_ref}"
            else:
                resolved_table = table_ref

        result.columns.append({
            "column": col_name,
            "table_ref": table_ref,
            "resolved_base_table": resolved_table,
        })

    result.columns = _deduplicate_columns(result.columns)
    return result


def _build_cte_column_map(parsed, cte_names, alias_to_table):
    """Build (CTE_NAME, COLUMN) -> base_table map."""
    if not SQLGLOT_AVAILABLE:
        return {}

    cte_map = {}
    for cte in parsed.find_all(exp.CTE):
        cte_alias = cte.alias.upper() if cte.alias else None
        if not cte_alias:
            continue

        select = cte.this
        if not isinstance(select, exp.Select):
            select = select.find(exp.Select)
            if not select:
                continue

        for col in select.find_all(exp.Column):
            col_name = col.name.upper()
            table_ref = col.table.upper() if col.table else ""

            if table_ref and table_ref in alias_to_table:
                schema, tname = alias_to_table[table_ref]
                full_name = f"{schema + '.' if schema else ''}{tname}"
                cte_map[(cte_alias, col_name)] = full_name
            elif table_ref and table_ref in cte_names:
                cte_map[(cte_alias, col_name)] = f"CTE:{table_ref}"

    return cte_map


def _deduplicate_columns(columns):
    seen = set()
    unique = []
    for c in columns:
        key = (c["column"].upper(), c["table_ref"].upper(),
               c["resolved_base_table"].upper())
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# Phase 4: Output
# ---------------------------------------------------------------------------

def generate_report(analyses, output_path):
    report = {
        "summary": {
            "total_blocks": len(analyses),
            "total_base_tables": len({
                t["name"].upper()
                for a in analyses for t in a.base_tables
            }),
            "total_columns": len({
                (c["column"].upper(), c["resolved_base_table"].upper())
                for a in analyses for c in a.columns
            }),
        },
        "all_base_tables": sorted({
            (t.get("schema", "") + "." if t.get("schema") else "") + t["name"]
            for a in analyses for t in a.base_tables
        }),
        "blocks": [asdict(a) for a in analyses],
    }

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    logger.info(f"JSON report written to {output_path}")
    return report


def build_dataframe(analyses):
    """
    Build a DataFrame with one row per (base_table, column) pair.

    Columns returned:
    - block_index    : which EXECUTE IMMEDIATE block (1-based)
    - base_table     : resolved base table name
    - schema         : schema if known
    - alias          : table alias used in query
    - column         : column name
    - table_ref      : original alias/table ref in SQL
    - resolved_via   : 'direct' | 'cte' | 'unresolved'
    """
    records = _build_records(analyses)

    if not PANDAS_AVAILABLE:
        logger.warning("pandas not installed — returning list of dicts")
        return records

    df = pd.DataFrame(records)
    if df.empty:
        return df

    df = df.sort_values(
        ['block_index', 'base_table', 'column']
    ).reset_index(drop=True)
    return df


def _build_records(analyses):
    records = []

    for analysis in analyses:
        alias_map = {}
        for t in analysis.base_tables:
            alias_map[t['alias'].upper()] = t
            alias_map[t['name'].upper()] = t

        for col in analysis.columns:
            resolved = col['resolved_base_table']
            table_ref = col['table_ref']

            if resolved.startswith('CTE:'):
                resolved_via = 'cte'
                base_table = resolved
            elif resolved:
                resolved_via = 'direct'
                base_table = resolved
            elif table_ref:
                resolved_via = 'unresolved'
                base_table = f'?:{table_ref}'
            else:
                resolved_via = 'unresolved'
                base_table = '?'

            schema = ''
            alias = table_ref
            for t in analysis.base_tables:
                if t['name'].upper() in base_table.upper():
                    schema = t.get('schema', '')
                    alias = t.get('alias', table_ref)
                    break

            records.append({
                'block_index': analysis.block_index,
                'base_table': base_table,
                'schema': schema,
                'alias': alias,
                'column': col['column'],
                'table_ref': table_ref,
                'resolved_via': resolved_via,
            })

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze_file(input_path: str, output_path: str = "analysis_results.json"):
    """
    Full pipeline: read -> extract -> clean -> parse -> report.

    Returns:
        (report_dict, DataFrame)
    """
    logger.info(f"Reading {input_path}")
    text = Path(input_path).read_text(encoding='utf-8', errors='replace')

    raw_blocks = extract_execute_immediate_blocks(text)

    if not raw_blocks:
        logger.warning("No EXECUTE IMMEDIATE blocks found!")
        logger.info("Attempting to parse entire file as plain SQL...")
        raw_blocks = [text]

    analyses = []
    for i, raw_sql in enumerate(raw_blocks):
        logger.info(f"Processing block {i + 1}/{len(raw_blocks)}")
        logger.debug(f"Extracted SQL (first 300 chars):\n{raw_sql[:300]}")

        cleaned = clean_sql_for_parsing(raw_sql)
        logger.debug(f"Cleaned SQL (first 300 chars):\n{cleaned[:300]}")

        analysis = analyze_sql(cleaned, block_index=i + 1)
        analyses.append(analysis)

        if analysis.parse_errors:
            for err in analysis.parse_errors:
                logger.warning(f"  Block {i + 1}: {err}")
        else:
            logger.info(f"  Block {i + 1}: {len(analysis.base_tables)} base tables, "
                        f"{len(analysis.columns)} columns, "
                        f"{len(analysis.cte_names)} CTEs")

    report = generate_report(analyses, output_path)
    df = build_dataframe(analyses)

    print(f"\n{'=' * 60}")
    print(f"  Analysis Complete")
    print(f"{'=' * 60}")
    print(f"  Blocks analyzed:    {report['summary']['total_blocks']}")
    print(f"  Unique base tables: {report['summary']['total_base_tables']}")
    print(f"  Unique columns:     {report['summary']['total_columns']}")
    print(f"  Base tables: {', '.join(report['all_base_tables'])}")
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


    '''
    import logging
    logging.getLogger().setLevel(logging.DEBUG)

    report, df = oracle_sql_analyzer.analyze_file('test_aml_script.sql')

    logging.getLogger().setLevel(logging.INFO)
    '''
