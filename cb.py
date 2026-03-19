"""
Oracle PL/SQL EXECUTE IMMEDIATE Analyzer
=========================================
Reads Oracle PL/SQL scripts, extracts SQL from EXECUTE IMMEDIATE strings,
parses them via sqlglot, and reports base tables + columns used.

Handles:
- Multiple EXECUTE IMMEDIATE blocks per file
- || concatenation with PL/SQL variable interpolation
- Escaped quotes ('') inside string literals
- CTE (WITH ... AS) resolution back to base tables
- Standard Oracle functions (TO_CHAR, TO_DATE, NVL, etc.)

Output: JSON file with base tables and column mappings.

Usage:
    python oracle_sql_analyzer.py <input.sql> [--output results.json]
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

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1: Extract EXECUTE IMMEDIATE strings from PL/SQL
# ---------------------------------------------------------------------------

def extract_execute_immediate_blocks(plsql_text: str) -> list[str]:
    """
    Find all EXECUTE IMMEDIATE '...' blocks and return the raw string content.
    Handles:
      - Multi-line strings
      - '' escaped quotes inside the string
      - || concatenation that continues the string
      - Skips matches inside comments
    """
    # Normalize line endings
    text = plsql_text.replace('\r\n', '\n')

    blocks = []

    # Pattern: EXECUTE IMMEDIATE followed by a string built with || concatenation
    pattern = re.compile(r"EXECUTE\s+IMMEDIATE\s*", re.IGNORECASE)

    for match in pattern.finditer(text):
        # Skip if this match is inside a single-line comment (-- ...)
        line_start = text.rfind('\n', 0, match.start()) + 1
        line_prefix = text[line_start:match.start()]
        if '--' in line_prefix:
            continue

        # Skip if inside a block comment (/* ... */)
        last_open = text.rfind('/*', 0, match.start())
        last_close = text.rfind('*/', 0, match.start())
        if last_open > last_close:
            continue

        start_pos = match.end()
        raw_block = _extract_concatenated_string(text, start_pos)
        if raw_block is not None and len(raw_block.strip()) > 10:
            blocks.append(raw_block)

    logger.info(f"Found {len(blocks)} EXECUTE IMMEDIATE block(s)")
    return blocks


def _extract_concatenated_string(text: str, pos: int) -> Optional[str]:
    """
    Starting at `pos`, parse a PL/SQL concatenated string expression like:
        'SELECT ...' || expr || ' WHERE ...' || expr || ' ...'
    Returns the assembled string with interpolated expressions replaced by placeholders.
    """
    fragments = []
    i = _skip_whitespace(text, pos)

    if i >= len(text):
        return None

    while i < len(text):
        i = _skip_whitespace(text, i)
        if i >= len(text):
            break

        if text[i] == "'":
            # Parse a string literal
            literal, end_pos = _parse_string_literal(text, i)
            if literal is None:
                logger.warning(f"Failed to parse string literal at position {i}")
                break
            fragments.append(literal)
            i = end_pos
        elif text[i] == '|' and i + 1 < len(text) and text[i + 1] == '|':
            # Concatenation operator — skip it
            i += 2
            continue
        elif text[i] == ';':
            # End of the EXECUTE IMMEDIATE statement
            break
        else:
            # This is an interpolated PL/SQL expression (variable, function call, etc.)
            expr, end_pos = _parse_plsql_expression(text, i)
            # Replace the expression with a placeholder
            placeholder = _expression_to_placeholder(expr)
            fragments.append(placeholder)
            i = end_pos

    if not fragments:
        return None

    return ''.join(fragments)


def _skip_whitespace(text: str, pos: int) -> int:
    """Skip whitespace and newlines."""
    while pos < len(text) and text[pos] in ' \t\n\r':
        pos += 1
    return pos


def _parse_string_literal(text: str, pos: int) -> tuple[Optional[str], int]:
    """
    Parse a PL/SQL string literal starting at pos (which should be a quote).
    Handles '' escape sequences (PL/SQL doubled single quotes).
    Returns (unescaped_string, position_after_closing_quote).
    """
    if text[pos] != "'":
        return None, pos

    result = []
    i = pos + 1
    while i < len(text):
        if text[i] == "'":
            # Check for escaped quote ''
            if i + 1 < len(text) and text[i + 1] == "'":
                result.append("'")
                i += 2
            else:
                # End of literal
                return ''.join(result), i + 1
        else:
            result.append(text[i])
            i += 1

    # Unterminated string
    logger.warning(f"Unterminated string literal starting at position {pos}")
    return ''.join(result), i


def _parse_plsql_expression(text: str, pos: int) -> tuple[str, int]:
    """
    Parse a PL/SQL expression that appears between || operators.
    This could be: a variable name, a function call like TO_CHAR(...), etc.
    We read until we hit || or ; while respecting parentheses.
    """
    i = pos
    paren_depth = 0
    start = pos

    while i < len(text):
        ch = text[i]
        if ch == '(':
            paren_depth += 1
            i += 1
        elif ch == ')':
            paren_depth -= 1
            i += 1
        elif ch == "'" and paren_depth > 0:
            # String literal inside a function call — skip it
            _, i = _parse_string_literal(text, i)
        elif paren_depth == 0 and ch == '|' and i + 1 < len(text) and text[i + 1] == '|':
            break
        elif paren_depth == 0 and ch == ';':
            break
        elif paren_depth == 0 and ch == '\n':
            # Check if this is really the end or just a linebreak in the expression
            # Look ahead for || on the next line
            lookahead = _skip_whitespace(text, i + 1)
            if lookahead < len(text) and text[lookahead] == '|' and lookahead + 1 < len(text) and text[lookahead + 1] == '|':
                i = lookahead
                break
            elif lookahead < len(text) and text[lookahead] == "'":
                # Next part is a string literal connected by implicit continuation
                i = lookahead
                break
            else:
                i += 1
        else:
            i += 1

    expr = text[start:i].strip()
    return expr, i


def _expression_to_placeholder(expr: str) -> str:
    """
    Convert a PL/SQL interpolated expression into a SQL-safe placeholder.

    IMPORTANT: The placeholder is injected between string literal fragments,
    so the surrounding context often already provides quotes.
    e.g., PL/SQL: ''' || TO_CHAR(alert_date, 'dd-mon-yy') || '''
          becomes: ' + placeholder + '  (the quotes come from the string fragments)

    So we return bare values — no wrapping quotes — for expressions that are
    typically interpolated inside quoted contexts.
    For numeric variables, we return a bare number.
    """
    expr_upper = expr.upper().strip()

    # TO_CHAR / TO_DATE patterns — return bare date string (context provides quotes)
    if 'TO_CHAR' in expr_upper or 'TO_DATE' in expr_upper:
        return '01-JAN-25'

    # Numeric variable names (heuristic: if name contains amt, num, count, id, etc.)
    numeric_hints = ['AMT', 'NUM', 'COUNT', 'ID', 'LIMIT', 'MIN', 'MAX', 'THRESHOLD',
                     'DAYS', 'FREQ', 'PERIOD', 'IND', 'AMOUNT', 'SCORE']
    if any(hint in expr_upper for hint in numeric_hints):
        return '0'

    # If it looks like a simple variable (no parens), assume numeric in most PL/SQL contexts
    if '(' not in expr:
        return '0'

    # Function call we don't recognize — return bare placeholder
    return 'PLACEHOLDER'


# ---------------------------------------------------------------------------
# Phase 2: Clean up the extracted SQL for parsing
# ---------------------------------------------------------------------------

def clean_sql_for_parsing(raw_sql: str) -> str:
    """
    Additional cleanup to make the extracted SQL parseable by sqlglot.
    """
    sql = raw_sql.strip()

    # Remove trailing semicolons (sqlglot doesn't always like them)
    sql = sql.rstrip(';').strip()

    # Fix any double spaces or weird whitespace
    sql = re.sub(r'\s+', ' ', sql)

    # Fix cases where placeholder substitution may have created bad syntax
    # e.g., "to_date('01-JAN-25', '01-JAN-25')" — not ideal but parseable
    # Also handle cases like WHERE col > 0 0 (double placeholder)
    sql = re.sub(r"(\d)\s+(\d)", r"\1", sql)

    return sql


# ---------------------------------------------------------------------------
# Phase 3: Parse SQL with sqlglot and extract tables/columns
# ---------------------------------------------------------------------------

@dataclass
class QueryAnalysis:
    """Analysis results for a single EXECUTE IMMEDIATE block."""
    block_index: int
    raw_sql: str
    cleaned_sql: str
    base_tables: list[dict] = field(default_factory=list)  # {name, schema, alias}
    columns: list[dict] = field(default_factory=list)       # {column, table, resolved_base_table}
    cte_names: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


def analyze_sql(cleaned_sql: str, block_index: int) -> QueryAnalysis:
    """Parse a cleaned SQL string and extract base tables and columns."""
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
        # Try a more lenient parse with error_level
        try:
            parsed = sqlglot.parse_one(
                cleaned_sql, read="oracle",
                error_level=sqlglot.ErrorLevel.WARN
            )
        except Exception as e2:
            result.parse_errors.append(f"sqlglot lenient parse also failed: {e2}")
            return result

    # --- Extract CTEs ---
    cte_names = set()
    for cte in parsed.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            cte_names.add(alias.upper())
    result.cte_names = sorted(cte_names)

    # --- Extract all table references ---
    alias_to_table = {}  # alias -> (schema, table_name)
    all_tables = []

    for table in parsed.find_all(exp.Table):
        table_name = table.name
        schema_name = table.db if hasattr(table, 'db') and table.db else None
        alias = table.alias if table.alias else table_name

        if table_name.upper() in cte_names:
            # This is a CTE reference, not a base table
            continue

        entry = {
            "name": table_name,
            "schema": schema_name or "",
            "alias": alias,
        }
        all_tables.append(entry)
        alias_to_table[alias.upper()] = (schema_name, table_name)
        alias_to_table[table_name.upper()] = (schema_name, table_name)

    # Deduplicate tables
    seen = set()
    for t in all_tables:
        key = (t["schema"].upper() if t["schema"] else "", t["name"].upper())
        if key not in seen:
            seen.add(key)
            result.base_tables.append(t)

    # --- Extract columns and resolve to base tables ---
    # First, build CTE -> base table resolution map
    cte_column_map = _build_cte_column_map(parsed, cte_names, alias_to_table)

    for col in parsed.find_all(exp.Column):
        col_name = col.name
        table_ref = col.table if col.table else ""

        resolved_table = ""
        if table_ref:
            table_ref_upper = table_ref.upper()
            if table_ref_upper in alias_to_table:
                schema, tname = alias_to_table[table_ref_upper]
                resolved_table = f"{schema + '.' if schema else ''}{tname}"
            elif table_ref_upper in cte_names:
                # Try to resolve through CTE
                base = cte_column_map.get((table_ref_upper, col_name.upper()))
                resolved_table = base if base else f"CTE:{table_ref}"
            else:
                resolved_table = table_ref

        result.columns.append({
            "column": col_name,
            "table_ref": table_ref,
            "resolved_base_table": resolved_table,
        })

    # Deduplicate columns
    result.columns = _deduplicate_columns(result.columns)

    return result


def _build_cte_column_map(
    parsed, cte_names: set, alias_to_table: dict
) -> dict[tuple[str, str], str]:
    """
    Build a map of (CTE_NAME, COLUMN_NAME) -> base_table_name
    by inspecting the SELECT list of each CTE definition.
    This is a best-effort resolution — complex expressions may not resolve.
    """
    if not SQLGLOT_AVAILABLE:
        return {}

    cte_map = {}

    for cte in parsed.find_all(exp.CTE):
        cte_alias = cte.alias.upper() if cte.alias else None
        if not cte_alias:
            continue

        # Get the CTE's SELECT body
        select = cte.this
        if not isinstance(select, exp.Select):
            # Could be a UNION or subquery
            select = select.find(exp.Select)
            if not select:
                continue

        # Walk columns in the CTE's body
        for col in select.find_all(exp.Column):
            col_name = col.name.upper()
            table_ref = col.table.upper() if col.table else ""

            if table_ref and table_ref in alias_to_table:
                schema, tname = alias_to_table[table_ref]
                full_name = f"{schema + '.' if schema else ''}{tname}"
                cte_map[(cte_alias, col_name)] = full_name
            elif table_ref and table_ref in cte_names:
                # CTE referencing another CTE — could recurse, but keep simple
                cte_map[(cte_alias, col_name)] = f"CTE:{table_ref}"

    return cte_map


def _deduplicate_columns(columns: list[dict]) -> list[dict]:
    """Remove duplicate column entries."""
    seen = set()
    unique = []
    for c in columns:
        key = (c["column"].upper(), c["table_ref"].upper(), c["resolved_base_table"].upper())
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# Phase 4: Output
# ---------------------------------------------------------------------------

def generate_report(analyses: list[QueryAnalysis], output_path: str):
    """Write analysis results to a JSON file."""
    report = {
        "summary": {
            "total_blocks": len(analyses),
            "total_base_tables": len({
                t["name"].upper()
                for a in analyses
                for t in a.base_tables
            }),
            "total_columns": len({
                (c["column"].upper(), c["resolved_base_table"].upper())
                for a in analyses
                for c in a.columns
            }),
        },
        "all_base_tables": sorted({
            (t.get("schema", "") + "." if t.get("schema") else "") + t["name"]
            for a in analyses
            for t in a.base_tables
        }),
        "blocks": [asdict(a) for a in analyses],
    }

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    logger.info(f"Report written to {output_path}")
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze_file(input_path: str, output_path: str = "analysis_results.json") -> dict:
    """Full pipeline: read file -> extract -> clean -> parse -> report."""
    logger.info(f"Reading {input_path}")
    text = Path(input_path).read_text(encoding='utf-8', errors='replace')

    # Phase 1: Extract
    raw_blocks = extract_execute_immediate_blocks(text)

    if not raw_blocks:
        logger.warning("No EXECUTE IMMEDIATE blocks found!")
        # Fall back: maybe the file is just plain SQL?
        logger.info("Attempting to parse the entire file as plain SQL...")
        raw_blocks = [text]

    analyses = []
    for i, raw_sql in enumerate(raw_blocks):
        logger.info(f"Processing block {i + 1}/{len(raw_blocks)}")
        logger.debug(f"Raw extracted SQL:\n{raw_sql[:200]}...")

        # Phase 2: Clean
        cleaned = clean_sql_for_parsing(raw_sql)
        logger.debug(f"Cleaned SQL:\n{cleaned[:200]}...")

        # Phase 3 & 4: Parse and extract
        analysis = analyze_sql(cleaned, block_index=i + 1)
        analyses.append(analysis)

        if analysis.parse_errors:
            for err in analysis.parse_errors:
                logger.warning(f"  Block {i + 1}: {err}")
        else:
            logger.info(f"  Block {i + 1}: {len(analysis.base_tables)} tables, "
                        f"{len(analysis.columns)} columns, "
                        f"{len(analysis.cte_names)} CTEs")

    # Phase 5: Report
    report = generate_report(analyses, output_path)

    # Print summary to console
    print(f"\n{'=' * 60}")
    print(f"Analysis Summary")
    print(f"{'=' * 60}")
    print(f"EXECUTE IMMEDIATE blocks found: {report['summary']['total_blocks']}")
    print(f"Unique base tables: {report['summary']['total_base_tables']}")
    print(f"Unique columns: {report['summary']['total_columns']}")
    print(f"\nBase tables: {', '.join(report['all_base_tables'])}")
    print(f"\nFull report: {output_path}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Analyze Oracle PL/SQL scripts for base tables and columns"
    )
    parser.add_argument("input", help="Path to the .sql file")
    parser.add_argument("--output", "-o", default="analysis_results.json",
                        help="Output JSON file path (default: analysis_results.json)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    analyze_file(args.input, args.output)
