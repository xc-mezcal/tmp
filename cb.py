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
    sql = _strip_comments_and_hints(sql)
    sql = re.sub(r'\s+', ' ', sql).strip()
    return sql


def _strip_comments_and_hints(sql: str) -> str:
    """Remove -- comments and /* */ hints while respecting string literals."""
    result = []
    i = 0
    while i < len(sql):
        if sql[i] == "'":
            result.append("'")
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    if i + 1 < len(sql) and sql[i + 1] == "'":
                        result.append("''")
                        i += 2
                    else:
                        result.append("'")
                        i += 1
                        break
                else:
                    result.append(sql[i])
                    i += 1
        elif sql[i] == '/' and i + 1 < len(sql) and sql[i + 1] == '*':
            end = sql.find('*/', i + 2)
            if end == -1:
                break
            result.append(' ')
            i = end + 2
        elif sql[i] == '-' and i + 1 < len(sql) and sql[i + 1] == '-':
            end = sql.find('\n', i)
            if end == -1:
                break
            result.append(' ')
            i = end + 1
        else:
            result.append(sql[i])
            i += 1
    return ''.join(result)


def extract_direct_sql_blocks(plsql_text: str) -> list[str]:
    """Extract INSERT INTO / MERGE / DELETE / UPDATE statements from direct PL/SQL."""
    text = plsql_text.replace('\r\n', '\n')
    blocks = []
    sql_start = re.compile(
        r'^\s*(INSERT\s+(?:/\*.*?\*/\s*)?INTO\s|MERGE\s|DELETE\s+FROM\s|UPDATE\s)',
        re.IGNORECASE | re.MULTILINE | re.DOTALL
    )
    for match in sql_start.finditer(text):
        keyword_start = match.start(1)
        line_start = text.rfind('\n', 0, keyword_start) + 1
        line_prefix = text[line_start:keyword_start]
        if '--' in line_prefix:
            continue
        last_open = text.rfind('/*', 0, keyword_start)
        last_close = text.rfind('*/', 0, keyword_start)
        if last_open > last_close:
            continue
        sql_text = _extract_until_semicolon(text, keyword_start)
        if sql_text and len(sql_text.strip()) > 10:
            blocks.append(sql_text)
    logger.info(f"Found {len(blocks)} direct SQL block(s)")
    return blocks


def _extract_until_semicolon(text: str, pos: int) -> Optional[str]:
    i = pos
    while i < len(text) and text[i] in ' \t\n\r':
        i += 1
    start = i
    in_single_quote = False
    while i < len(text):
        ch = text[i]
        if in_single_quote:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                else:
                    in_single_quote = False
                    i += 1
            else:
                i += 1
        elif ch == "'":
            in_single_quote = True
            i += 1
        elif ch == '-' and i + 1 < len(text) and text[i + 1] == '-':
            i = text.find('\n', i)
            if i == -1:
                i = len(text)
        elif ch == '/' and i + 1 < len(text) and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            i = end + 2 if end != -1 else len(text)
        elif ch == ';':
            return text[start:i].strip()
        else:
            i += 1
    return text[start:].strip() if start < len(text) else None


def _parse_declare_variables(plsql_text: str) -> set:
    variables = set()
    text_upper = plsql_text.upper()
    decl_start = text_upper.find('DECLARE')
    begin_pos = text_upper.find('BEGIN', decl_start if decl_start >= 0 else 0)
    if decl_start < 0 or begin_pos < 0:
        return variables
    decl_section = plsql_text[decl_start + 7:begin_pos]
    var_pattern = re.compile(
        r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(?:NUMBER|VARCHAR2|DATE|CHAR|INTEGER|BOOLEAN|TIMESTAMP)',
        re.IGNORECASE | re.MULTILINE
    )
    for m in var_pattern.finditer(decl_section):
        variables.add(m.group(1).upper())
    return variables


def clean_direct_sql(raw_sql: str, plsql_variables: set) -> str:
    sql = raw_sql.strip().rstrip(';').strip()
    sql = _strip_comments_and_hints(sql)
    for var in plsql_variables:
        pattern = r'(?<![A-Za-z0-9_.])\b' + re.escape(var) + r'\b(?![A-Za-z0-9_.])'
        sql = re.sub(pattern, '0', sql, flags=re.IGNORECASE)
    sql = re.sub(r':=\s*\S+', '', sql)
    sql = re.sub(r'\s+', ' ', sql).strip()
    return sql



# ===========================================================================
# Phase 3: Recursive AST walk — build DAG edge table
# ===========================================================================
#
# One recursive function: process_query(node, ...)
# It handles Select, Insert, Union by walking the AST children in order:
#   1. with_ (CTEs) — process sequentially, each sees previous
#   2. from + joins — build from_scope {alias: (table, col_map)}
#   3. where — gather columns, recurse into subqueries
#   4. group — gather columns
#   5. having — gather columns, recurse into subqueries
#   6. expressions (SELECT list) — resolve columns, handle *, subqueries
#
# Each edge: (output_table, output_col) → (source_table, source_col)
# Leaf edges: (base_table, col) → (None, None)
# The DAG is a flat list of edge dicts — that's the single source of truth.


class LineageDAG:
    """Flat edge table. Each edge = one dict. That's it."""

    def __init__(self):
        self.edges = []
        self._seen = set()  # dedup
        # Cross-block scope: scope_name -> col_map
        self._scope_columns = {}

    def add_edge(self, out_tbl, out_col, src_tbl, src_col,
                 block_index, scope_name, scope_type, clause="SELECT",
                 ambiguous=False):
        ot = out_tbl.upper() if out_tbl else None
        oc = out_col.upper() if out_col else None
        st = src_tbl.upper() if src_tbl else None
        sc = src_col.upper() if src_col else None

        key = (ot, oc, st, sc, scope_name, clause)
        if key in self._seen:
            return
        self._seen.add(key)

        self.edges.append({
            "output_table": ot, "output_column": oc,
            "source_table": st, "source_column": sc,
            "block_index": block_index, "scope_name": scope_name,
            "scope_type": scope_type, "clause": clause,
            "ambiguous": ambiguous,
        })

    def add_leaf(self, table, column, block_index, scope_name, clause="SELECT"):
        """Base table column — leaf node."""
        self.add_edge(table, column, None, None,
                      block_index, scope_name, "base", clause)

    def register_scope(self, name, col_map):
        self._scope_columns[name.upper()] = col_map

    def get_scope(self, name):
        return self._scope_columns.get(name.upper(), {})

    def has_scope(self, name):
        return name.upper() in self._scope_columns

    def get_base_tables(self):
        return sorted({e["output_table"] for e in self.edges
                       if e["scope_type"] == "base" and e["output_table"]})

    def get_leaf_columns(self):
        return sorted({(e["output_table"], e["output_column"])
                       for e in self.edges
                       if e["scope_type"] == "base"
                       and e["output_table"] and e["output_column"]})


# ---------------------------------------------------------------------------
# The recursive engine
# ---------------------------------------------------------------------------

def process_block(cleaned_sql: str, dag: LineageDAG, block_index: int) -> dict:
    """Parse one SQL block and recursively walk the AST."""
    result = {
        "block_index": block_index,
        "cleaned_sql": cleaned_sql,
        "insert_target": None,
        "ctes_found": [],
        "base_tables_found": [],
        "parse_errors": [],
    }

    if not SQLGLOT_AVAILABLE:
        result["parse_errors"].append("sqlglot not installed — pip install sqlglot")
        return result

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

    # known_tables carries cross-block INSERT INTO targets
    # Format: {TABLE_UPPER: col_map}
    # col_map = {COL: [(src_tbl, src_col, ambiguous), ...]}
    known_tables = {}
    for name, col_map in dag._scope_columns.items():
        known_tables[name] = col_map

    # Process the AST recursively
    col_map, scope_name = _process_node(
        parsed, dag, known_tables, block_index, "__ROOT"
    )

    # For pure SELECT blocks (no INSERT), add root edges
    if scope_name == "__ROOT" and col_map:
        root_name = f"__ROOT_BLOCK_{block_index}"
        for out_col, sources in col_map.items():
            for item in sources:
                src_tbl, src_col, amb = item[0], item[1], item[2]
                dag.add_edge(root_name, out_col, src_tbl, src_col,
                             block_index, root_name, "root",
                             ambiguous=amb)

    # Extract metadata for the result
    if scope_name and scope_name != "__ROOT":
        result["insert_target"] = scope_name

    result["ctes_found"] = [k for k in known_tables
                            if k not in dag._scope_columns]
    result["base_tables_found"] = dag.get_base_tables()

    return result


def _process_node(node, dag, known_tables, block_index, default_scope):
    """
    Dispatch to the right handler based on node type.
    Returns (col_map, scope_name).
    """
    # Block node: multiple statements (e.g., two INSERT statements parsed together)
    if hasattr(exp, 'Block') and isinstance(node, exp.Block):
        # Process each statement sequentially
        last_col_map = {}
        last_scope = default_scope
        for stmt in node.expressions:
            last_col_map, last_scope = _process_node(
                stmt, dag, known_tables, block_index, default_scope
            )
        return last_col_map, last_scope

    if isinstance(node, exp.Insert):
        return _process_insert(node, dag, known_tables, block_index)
    elif isinstance(node, exp.Select):
        return _process_select(node, dag, known_tables, block_index, default_scope)
    elif isinstance(node, exp.Union):
        return _process_union(node, dag, known_tables, block_index, default_scope)
    elif isinstance(node, exp.Subquery):
        inner = node.this
        alias = node.alias.upper() if node.alias else default_scope
        return _process_node(inner, dag, known_tables, block_index, alias)
    else:
        # Try to find an Insert or Select inside
        insert = node.find(exp.Insert)
        if insert:
            return _process_insert(insert, dag, known_tables, block_index)
        select = node.find(exp.Select)
        if select:
            return _process_select(select, dag, known_tables, block_index, default_scope)
        return {}, default_scope


def _process_insert(node, dag, known_tables, block_index):
    """Handle INSERT INTO target_table ... SELECT ..."""
    target_table = None
    # node.this is the target Table for INSERT
    target_node = node.this
    if isinstance(target_node, exp.Table):
        target_table = target_node.name.upper()
    else:
        # Fallback
        target_node = node.find(exp.Table)
        if target_node:
            target_table = target_node.name.upper()

    # node.expression is the SELECT part
    select_expr = node.expression
    if select_expr is None:
        return {}, target_table

    col_map, _ = _process_node(
        select_expr, dag, known_tables, block_index,
        target_table or "__INSERT"
    )

    # Register the INSERT target
    if target_table and col_map:
        # Add root edges: target.col → source.col
        for out_col, sources in col_map.items():
            for item in sources:
                src_tbl, src_col, amb = item[0], item[1], item[2]
                dag.add_edge(target_table, out_col, src_tbl, src_col,
                             block_index, target_table, "root",
                             ambiguous=amb)

        known_tables[target_table] = col_map
        dag.register_scope(target_table, col_map)

    return col_map, target_table


def _process_union(node, dag, known_tables, block_index, scope_name):
    """Handle UNION / UNION ALL — process left and right, merge col_maps."""
    left_map, _ = _process_node(
        node.this, dag, known_tables, block_index, scope_name
    )
    right_map, _ = _process_node(
        node.expression, dag, known_tables, block_index, scope_name
    )
    # Use left side's column names as output (standard SQL behavior)
    # Merge sources: each output col comes from both branches
    merged = {}
    for col, sources in left_map.items():
        merged[col] = list(sources)
    for col, sources in right_map.items():
        if col in merged:
            merged[col].extend(sources)
        else:
            merged[col] = list(sources)
    return merged, scope_name


def _process_select(node, dag, known_tables, block_index, scope_name):
    """
    The main recursive handler for a SELECT node.
    Walks: with_ → from → joins → where → group → having → expressions
    Returns col_map for this SELECT's output columns.
    """
    logger.debug(f"  _process_select [{scope_name}] "
                 f"args: {[k for k,v in node.args.items() if v is not None]}")

    # --- Step 1: CTEs (with_ or with) ---
    # sqlglot uses "with" in some versions and "with_" in others
    with_clause = node.args.get("with") or node.args.get("with_")
    if not with_clause and node.parent:
        parent_args = node.parent.args if hasattr(node.parent, 'args') else {}
        with_clause = parent_args.get("with") or parent_args.get("with_")
    if with_clause:
        for cte_node in with_clause.expressions:
            if not isinstance(cte_node, exp.CTE):
                continue
            cte_name = cte_node.alias_or_name.upper()
            cte_body = cte_node.this

            cte_col_map, _ = _process_node(
                cte_body, dag, known_tables, block_index, cte_name
            )

            # Add CTE edges
            for out_col, sources in cte_col_map.items():
                for item in sources:
                    src_tbl, src_col, amb = item[0], item[1], item[2]
                    dag.add_edge(cte_name, out_col, src_tbl, src_col,
                                 block_index, cte_name, "cte",
                                 ambiguous=amb)

            # Register so subsequent CTEs and main query can see it
            known_tables[cte_name] = cte_col_map
            dag.register_scope(cte_name, cte_col_map)

    # --- Step 2: FROM → build from_scope ---
    from_scope = {}

    from_clause = node.args.get("from") or node.args.get("from_")
    if from_clause:
        logger.debug(f"  [{scope_name}] FROM node type: {type(from_clause).__name__}, "
                     f".this type: {type(from_clause.this).__name__ if from_clause.this else None}")
        _collect_from_sources(from_clause, from_scope, dag, known_tables,
                              block_index, scope_name)
    else:
        logger.debug(f"  [{scope_name}] No FROM clause found!")

    # --- Step 3: JOINs ---
    joins = node.args.get("joins") or []
    logger.debug(f"  [{scope_name}] {len(joins)} JOIN(s) found")
    for join_node in joins:
        logger.debug(f"  [{scope_name}] JOIN .this type: {type(join_node.this).__name__}, "
                     f"name: {getattr(join_node.this, 'name', '?')}, "
                     f"alias: {getattr(join_node.this, 'alias', '?')}")
        _collect_from_sources(join_node, from_scope, dag, known_tables,
                              block_index, scope_name)

    logger.debug(f"  [{scope_name}] from_scope: {list(from_scope.keys())}")

    # --- Step 3b: JOIN ON conditions — gather columns used as join keys ---
    for join_node in joins:
        on_clause = join_node.args.get("on")
        if on_clause:
            _gather_columns(on_clause, from_scope, dag, known_tables,
                            block_index, scope_name, "JOIN_ON")

    # --- Step 4: WHERE — gather columns ---
    where_clause = node.args.get("where")
    if where_clause:
        _gather_columns(where_clause, from_scope, dag, known_tables,
                        block_index, scope_name, "WHERE")

    # --- Step 5: GROUP BY ---
    group_clause = node.args.get("group")
    if group_clause:
        _gather_columns(group_clause, from_scope, dag, known_tables,
                        block_index, scope_name, "GROUP_BY")

    # --- Step 6: HAVING ---
    having_clause = node.args.get("having")
    if having_clause:
        _gather_columns(having_clause, from_scope, dag, known_tables,
                        block_index, scope_name, "HAVING")

    # --- Step 7: ORDER BY ---
    order_clause = node.args.get("order")
    if order_clause:
        _gather_columns(order_clause, from_scope, dag, known_tables,
                        block_index, scope_name, "ORDER_BY")

    # --- Step 8: SELECT expressions → build col_map ---
    col_map = {}
    for sel_expr in node.expressions:
        _process_select_expr(sel_expr, from_scope, dag, known_tables,
                             col_map, block_index, scope_name)

    return col_map, scope_name


# ---------------------------------------------------------------------------
# Helpers: source collection, column resolution
# ---------------------------------------------------------------------------

def _collect_from_sources(node, from_scope, dag, known_tables,
                          block_index, scope_name):
    """
    Walk a FROM or JOIN node to find Table and Subquery sources.

    We are EXPLICIT about what to collect:
    - exp.Table → register as base or known derived
    - exp.Subquery → recurse, register as derived
    - exp.Join → look at its .this (the joined table/subquery) only,
                 NOT the ON condition (which contains columns, not sources)
    - exp.From → look at its .this

    We do NOT blindly iterate all children — that would pick up
    ON conditions, WHERE subqueries, etc.
    """
    if isinstance(node, exp.Table):
        tname = node.name.upper()
        talias = node.alias.upper() if node.alias else tname
        logger.debug(f"    Found Table: name={tname} alias={talias}")

        if tname in known_tables:
            from_scope[talias] = (tname, known_tables[tname])
        else:
            from_scope[talias] = (tname, None)
        return

    if isinstance(node, exp.Subquery):
        alias = node.alias.upper() if node.alias else "__SUBQ"
        inner = node.this
        sub_col_map, _ = _process_node(
            inner, dag, known_tables, block_index, alias
        )
        for out_col, sources in sub_col_map.items():
            for item in sources:
                src_tbl, src_col, amb = item[0], item[1], item[2]
                dag.add_edge(alias, out_col, src_tbl, src_col,
                             block_index, alias, "subquery",
                             ambiguous=amb)

        known_tables[alias] = sub_col_map
        from_scope[alias] = (alias, sub_col_map)
        return

    if isinstance(node, exp.Join):
        # A Join's .this is the table/subquery being joined
        # Its .args.get("on") is the ON condition — we skip that
        table_expr = node.this
        if table_expr:
            _collect_from_sources(table_expr, from_scope, dag, known_tables,
                                  block_index, scope_name)
        return

    if isinstance(node, exp.From):
        # From's .this is the table expression (could be Table, Subquery, etc.)
        table_expr = node.this
        if table_expr:
            _collect_from_sources(table_expr, from_scope, dag, known_tables,
                                  block_index, scope_name)
        # Also check for comma-joined tables (multiple expressions under From)
        for child in node.iter_expressions():
            if child is not node.this:
                _collect_from_sources(child, from_scope, dag, known_tables,
                                      block_index, scope_name)
        return

    # For other structural nodes (e.g., Paren wrapping a subquery),
    # iterate children but only collect Table/Subquery/Join
    for child in node.iter_expressions():
        if isinstance(child, (exp.Table, exp.Subquery, exp.Join, exp.From)):
            _collect_from_sources(child, from_scope, dag, known_tables,
                                  block_index, scope_name)


def _inject_parent_scope(from_scope, known_tables):
    """
    Make parent scope aliases available to child subqueries via known_tables.
    This enables correlated subquery resolution: e.g., WHERE al.ref_id = et.id
    where 'et' is an alias from the outer query's FROM clause.

    Maps alias → actual_table's col_map (if derived) or a marker (if base).
    """
    for alias, (actual_table, col_map) in from_scope.items():
        if alias not in known_tables:
            if col_map is not None:
                # Derived table (CTE, subquery) — alias gets its col_map
                known_tables[alias] = col_map
            elif actual_table in known_tables:
                # Alias for a known derived table — share its col_map
                known_tables[alias] = known_tables[actual_table]
            # For base table aliases: _resolve_column will check known_tables,
            # not find it, then add a leaf — which is correct behavior


def _resolve_column(col_name, col_table_ref, from_scope, dag,
                     known_tables, block_index, scope_name, clause):
    """
    Resolve one column reference against from_scope.
    Returns [(source_table, source_col, ambiguous), ...]
    Also adds leaf edges for base table columns.
    """
    col_name = col_name.upper()

    if col_table_ref:
        col_table_ref = col_table_ref.upper()
        info = from_scope.get(col_table_ref)
        if info:
            actual_table, col_map = info
            if col_map is not None:
                if col_name in col_map:
                    # Propagate from inner col_map (already has ambiguous flag)
                    return col_map[col_name]
                return [(actual_table, col_name, False)]
            else:
                dag.add_leaf(actual_table, col_name, block_index, scope_name, clause)
                return [(actual_table, col_name, False)]

        if col_table_ref in known_tables:
            col_map = known_tables[col_table_ref]
            if col_name in col_map:
                return col_map[col_name]
            return [(col_table_ref, col_name, False)]

        dag.add_leaf(col_table_ref, col_name, block_index, scope_name, clause)
        return [(col_table_ref, col_name, False)]

    # Unqualified column
    if len(from_scope) == 1:
        alias, (actual_table, col_map) = next(iter(from_scope.items()))
        if col_map is not None:
            if col_name in col_map:
                return col_map[col_name]
            return [(actual_table, col_name, False)]
        else:
            dag.add_leaf(actual_table, col_name, block_index, scope_name, clause)
            return [(actual_table, col_name, False)]

    # Multiple sources — try derived tables with known columns first
    derived_matches = []
    base_matches = []
    for alias, (actual_table, col_map) in from_scope.items():
        if col_map is not None:
            if col_name in col_map:
                derived_matches.append((actual_table, col_name, False))
        else:
            base_matches.append((actual_table, col_name, False))

    if len(derived_matches) == 1:
        return derived_matches
    if derived_matches:
        return [(t, c, True) for t, c, _ in derived_matches]  # ambiguous
    if len(base_matches) == 1:
        tbl = base_matches[0][0]
        dag.add_leaf(tbl, col_name, block_index, scope_name, clause)
        return base_matches
    if base_matches:
        for tbl, _, _ in base_matches:
            dag.add_leaf(tbl, col_name, block_index, scope_name, clause)
        return [(t, c, True) for t, c, _ in base_matches]  # ambiguous

    return [("UNRESOLVED", col_name, False)]


def _gather_columns(node, from_scope, dag, known_tables,
                     block_index, scope_name, clause):
    """
    Walk an AST node (WHERE, GROUP BY, HAVING, ORDER BY) for Column refs.
    Stop at Subquery boundaries — recurse into them separately.
    """
    for child in node.iter_expressions():
        if isinstance(child, exp.Column):
            if isinstance(child.this, exp.Star):
                continue
            col_name = child.name.upper()
            col_table_ref = child.table.upper() if child.table else ""

            sources = _resolve_column(
                col_name, col_table_ref, from_scope, dag,
                known_tables, block_index, scope_name, clause
            )
            # Add edges
            for item in sources:
                src_tbl, src_col, amb = item[0], item[1], item[2]
                dag.add_edge(scope_name, col_name, src_tbl, src_col,
                             block_index, scope_name, "filter", clause,
                             ambiguous=amb)

        elif isinstance(child, exp.Subquery):
            # Separate scope — but may have correlated references to outer scope
            # Inject parent from_scope aliases into known_tables for resolution
            alias = child.alias.upper() if child.alias else "__WHERE_SUBQ"
            _inject_parent_scope(from_scope, known_tables)
            _process_node(child, dag, known_tables, block_index, alias)

        elif isinstance(child, exp.Select):
            # Scalar subquery in expression — separate scope
            _inject_parent_scope(from_scope, known_tables)
            _process_node(child, dag, known_tables, block_index, "__SCALAR_SUBQ")

        else:
            # Recurse into non-scope-creating nodes (AND, OR, Compare, etc.)
            _gather_columns(child, from_scope, dag, known_tables,
                            block_index, scope_name, clause)


def _process_select_expr(sel_expr, from_scope, dag, known_tables,
                          col_map, block_index, scope_name):
    """Process one SELECT expression. Adds to col_map."""

    # --- Handle * and t.* ---
    if isinstance(sel_expr, exp.Star):
        _expand_star(None, from_scope, col_map)
        return
    if isinstance(sel_expr, exp.Column) and isinstance(sel_expr.this, exp.Star):
        table_ref = sel_expr.table.upper() if sel_expr.table else None
        _expand_star(table_ref, from_scope, col_map)
        return

    # --- Output name ---
    if isinstance(sel_expr, exp.Alias):
        output_name = sel_expr.alias.upper()
    elif isinstance(sel_expr, exp.Column):
        output_name = sel_expr.name.upper()
    else:
        # Complex expression — find first Column (not in subquery)
        col = _find_column_shallow(sel_expr)
        if col:
            output_name = col.name.upper()
        else:
            return  # pure literal

    # --- Collect column refs ---
    sources = []  # list of (src_tbl, src_col, ambiguous)

    if isinstance(sel_expr, exp.Column):
        # sel_expr IS a column — resolve it directly
        col_name = sel_expr.name.upper()
        col_table_ref = sel_expr.table.upper() if sel_expr.table else ""
        sources = _resolve_column(
            col_name, col_table_ref, from_scope, dag,
            known_tables, block_index, scope_name, "SELECT"
        )
    elif isinstance(sel_expr, exp.Alias):
        # Alias wrapping something — check what's inside
        inner = sel_expr.this
        if isinstance(inner, exp.Column):
            # Alias(Column) — resolve the column directly
            col_name = inner.name.upper()
            col_table_ref = inner.table.upper() if inner.table else ""
            sources = _resolve_column(
                col_name, col_table_ref, from_scope, dag,
                known_tables, block_index, scope_name, "SELECT"
            )
        else:
            # Alias(expression) — walk for columns, stop at subqueries
            _collect_shallow_columns(inner, from_scope, dag, known_tables,
                                      sources, block_index, scope_name)
            # Handle scalar subqueries inside the alias
            _inject_parent_scope(from_scope, known_tables)
            for subq in inner.find_all(exp.Subquery):
                sub_alias = subq.alias.upper() if subq.alias else "__SCALAR"
                sub_map, _ = _process_node(subq, dag, known_tables, block_index, sub_alias)
                for sub_col, sub_sources in sub_map.items():
                    sources.extend(sub_sources)
    else:
        # Complex expression — walk for columns, stop at subqueries
        _collect_shallow_columns(sel_expr, from_scope, dag, known_tables,
                                  sources, block_index, scope_name)
        # Handle scalar subqueries
        _inject_parent_scope(from_scope, known_tables)
        for subq in sel_expr.find_all(exp.Subquery):
            sub_alias = subq.alias.upper() if subq.alias else "__SCALAR"
            sub_map, _ = _process_node(subq, dag, known_tables, block_index, sub_alias)
            for sub_col, sub_sources in sub_map.items():
                sources.extend(sub_sources)

    if sources:
        col_map[output_name] = sources


def _expand_star(table_ref, from_scope, col_map):
    """Expand * or t.* using from_scope's known columns."""
    targets = from_scope
    if table_ref:
        info = from_scope.get(table_ref)
        if info:
            targets = {table_ref: info}
        else:
            return

    for alias, (actual_table, scope_col_map) in targets.items():
        if scope_col_map is not None:
            for col in scope_col_map:
                if col:
                    col_map[col] = [(actual_table, col, False)]
        else:
            col_map["*"] = [(actual_table, "*", False)]


def _find_column_shallow(node):
    """Find first Column without descending into Subquery/Select."""
    for child in node.iter_expressions():
        if isinstance(child, exp.Column) and not isinstance(child.this, exp.Star):
            return child
        if isinstance(child, (exp.Subquery, exp.Select)):
            continue
        result = _find_column_shallow(child)
        if result:
            return result
    return None


def _collect_shallow_columns(node, from_scope, dag, known_tables,
                              sources, block_index, scope_name):
    """Collect Column refs from an expression, stopping at Subquery boundaries.
    Appends (src_tbl, src_col, ambiguous) triples to sources."""
    for child in node.iter_expressions():
        if isinstance(child, exp.Column):
            if isinstance(child.this, exp.Star):
                continue
            col_name = child.name.upper()
            col_table_ref = child.table.upper() if child.table else ""
            resolved = _resolve_column(
                col_name, col_table_ref, from_scope, dag,
                known_tables, block_index, scope_name, "SELECT"
            )
            sources.extend(resolved)

        elif isinstance(child, (exp.Subquery, exp.Select)):
            continue

        else:
            _collect_shallow_columns(child, from_scope, dag, known_tables,
                                      sources, block_index, scope_name)


# ===========================================================================
# Phase 4: Output — JSON + single DataFrame from DAG edges
# ===========================================================================

def generate_report(block_results: list, dag: LineageDAG,
                    output_path: str) -> dict:
    report = {
        "summary": {
            "total_blocks": len(block_results),
            "total_base_tables": len(dag.get_base_tables()),
            "total_base_columns": len(dag.get_leaf_columns()),
            "base_tables": dag.get_base_tables(),
            "base_columns": dag.get_leaf_columns(),
        },
        "lineage_edges": dag.edges,
        "blocks": [
            {
                "block_index": br["block_index"],
                "insert_target": br["insert_target"],
                "ctes": br["ctes_found"],
                "base_tables": br["base_tables_found"],
                "parse_errors": br["parse_errors"],
            }
            for br in block_results
        ],
    }

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    logger.info(f"JSON report written to {output_path}")
    return report


def build_dataframe(dag: LineageDAG):
    """
    Single DataFrame from the DAG edges. That's it.

    Filtering:
        df[df.scope_type == 'base']                   # leaf/base columns
        df[df.scope_type == 'root']                    # final output
        df[df.scope_type == 'cte']                     # CTE edges
        df[df.source_table.isna()]                     # leaf nodes
        df[df.output_table == 'SEQ_ALERTS']            # what feeds a CTE
        df[df.source_table == 'KDD_TRANSACTION']       # where a table's data goes
    """
    if not dag.edges:
        if PANDAS_AVAILABLE:
            return pd.DataFrame()
        return []

    if not PANDAS_AVAILABLE:
        return dag.edges

    df = pd.DataFrame(dag.edges)
    return df.sort_values(
        ["block_index", "scope_type", "output_table", "output_column"]
    ).reset_index(drop=True)


# ===========================================================================
# Main
# ===========================================================================

def analyze_file(input_path: str, output_path: str = "analysis_results.json"):
    """
    Full pipeline. Returns (report_dict, df_edges).

    df_edges: the complete DAG. Filter for what you need:
        df[df.source_table.isna()]   → base table columns (leaves)
        df[df.scope_type == 'root']  → final output columns
    """
    logger.info(f"Reading {input_path}")
    text = Path(input_path).read_text(encoding='utf-8', errors='replace')

    # Phase 1: Extract blocks
    raw_blocks = extract_execute_immediate_blocks(text)
    extraction_mode = "execute_immediate"

    if not raw_blocks:
        logger.info("No EXECUTE IMMEDIATE found. Trying direct SQL...")
        raw_blocks = extract_direct_sql_blocks(text)
        extraction_mode = "direct_sql"

    if not raw_blocks:
        logger.warning("No SQL blocks found! Parsing entire file...")
        raw_blocks = [text]
        extraction_mode = "raw"

    logger.info(f"Extraction mode: {extraction_mode}, {len(raw_blocks)} block(s)")

    plsql_variables = set()
    if extraction_mode == "direct_sql":
        plsql_variables = _parse_declare_variables(text)

    # Phase 2+3: Clean and process
    dag = LineageDAG()
    block_results = []

    for i, raw_sql in enumerate(raw_blocks):
        logger.info(f"Processing block {i + 1}/{len(raw_blocks)}")

        if extraction_mode == "direct_sql":
            cleaned = clean_direct_sql(raw_sql, plsql_variables)
        else:
            cleaned = clean_sql_for_parsing(raw_sql)

        logger.debug(f"  Cleaned (first 300): {cleaned[:300]}")

        res = process_block(cleaned, dag, block_index=i + 1)
        block_results.append(res)

        if res["parse_errors"]:
            for err in res["parse_errors"]:
                logger.warning(f"  Block {i + 1}: {err}")
        else:
            logger.info(
                f"  Block {i + 1}: "
                f"{len(res['base_tables_found'])} base tables, "
                f"{len(res['ctes_found'])} CTEs"
                + (f", INSERT INTO {res['insert_target']}"
                   if res['insert_target'] else "")
            )

    # Phase 4: Output
    report = generate_report(block_results, dag, output_path)
    df_edges = build_dataframe(dag)

    print(f"\n{'=' * 60}")
    print(f"  Analysis Complete ({extraction_mode})")
    print(f"{'=' * 60}")
    print(f"  Blocks analyzed:    {len(block_results)}")
    print(f"  Base tables:        {dag.get_base_tables()}")
    print(f"  Base columns:       {len(dag.get_leaf_columns())}")
    print(f"  Lineage edges:      {len(dag.edges)}")
    print(f"  JSON report:        {output_path}")
    print(f"{'=' * 60}\n")

    return report, df_edges


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
