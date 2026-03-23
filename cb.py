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
            # Block comment — handle nesting: /* ... /* ... */ ... */
            depth = 1
            i += 2
            while i < len(sql) and depth > 0:
                if sql[i] == '/' and i + 1 < len(sql) and sql[i + 1] == '*':
                    depth += 1
                    i += 2
                elif sql[i] == '*' and i + 1 < len(sql) and sql[i + 1] == '/':
                    depth -= 1
                    i += 2
                else:
                    i += 1
            result.append(' ')
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
# Phase 3: Recursive AST walk with clean scope propagation
# ===========================================================================
#
# Two data structures:
#   known_tables: {TABLE_NAME: col_map} — global, CTEs + INSERT targets
#                 col_map = {COL: [(src_tbl, src_col, ambiguous), ...]}
#   parent_scope: {ALIAS: TABLE_NAME} — local, inherited from parent SELECT
#                 To check if TABLE_NAME is derived: TABLE_NAME in known_tables
#
# Scope rules:
#   CTE body        → parent_scope = {} (can't see main FROM)
#   FROM subquery   → parent_scope = parent_scope (can't see sibling FROM)
#   WHERE subquery  → parent_scope = local_scope (correlated refs allowed)
#   HAVING subquery → parent_scope = local_scope
#   SELECT subquery → parent_scope = local_scope
#   JOIN ON subquery→ parent_scope = local_scope (built so far)


class LineageDAG:
    """Flat edge table. Single source of truth."""

    def __init__(self):
        self.edges = []
        self._seen = set()
        self._scope_columns = {}  # cross-block: scope_name -> col_map

    def add_edge(self, out_tbl, out_col, src_tbl, src_col,
                 block_index, scope_name, scope_type, clause="SELECT",
                 ambiguous=False, unqualified=False):
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
            "ambiguous": ambiguous, "unqualified": unqualified,
        })

    def add_leaf(self, table, column, block_index, scope_name, clause="SELECT",
                 unqualified=False):
        self.add_edge(table, column, None, None,
                      block_index, scope_name, "base", clause,
                      unqualified=unqualified)

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
# Entry point for each extracted SQL block
# ---------------------------------------------------------------------------

def process_block(cleaned_sql: str, dag: LineageDAG, block_index: int) -> dict:
    result = {
        "block_index": block_index, "cleaned_sql": cleaned_sql,
        "insert_target": None, "ctes_found": [], "base_tables_found": [],
        "parse_errors": [],
    }
    if not SQLGLOT_AVAILABLE:
        result["parse_errors"].append("sqlglot not installed")
        return result

    try:
        parsed = sqlglot.parse_one(cleaned_sql, read="oracle")
    except Exception as e:
        result["parse_errors"].append(f"Parse error: {e}")
        try:
            parsed = sqlglot.parse_one(cleaned_sql, read="oracle",
                                       error_level=sqlglot.ErrorLevel.WARN)
        except Exception as e2:
            result["parse_errors"].append(f"Lenient parse failed: {e2}")
            return result

    # known_tables: global col_map registry (CTEs + cross-block INSERT targets)
    known_tables = {}
    for name, col_map in dag._scope_columns.items():
        known_tables[name] = col_map

    # Process recursively — parent_scope starts empty
    col_map, scope_name = _process_node(
        parsed, dag, known_tables, {}, block_index, "__ROOT"
    )

    # For pure SELECT (no INSERT), add root edges
    if scope_name == "__ROOT" and col_map:
        root_name = f"__ROOT_BLOCK_{block_index}"
        for out_col, sources in col_map.items():
            for item in sources:
                src_tbl, src_col = item[0], item[1]
                amb = item[2] if len(item) > 2 else False
                unq = item[3] if len(item) > 3 else False
                dag.add_edge(root_name, out_col, src_tbl, src_col,
                             block_index, root_name, "root", ambiguous=amb, unqualified=False)

    if scope_name and scope_name != "__ROOT":
        result["insert_target"] = scope_name

    result["ctes_found"] = [k for k in known_tables if k not in dag._scope_columns]
    result["base_tables_found"] = dag.get_base_tables()
    return result


# ---------------------------------------------------------------------------
# Node dispatcher
# ---------------------------------------------------------------------------

def _process_node(node, dag, known_tables, parent_scope, block_index, default_scope):
    """Returns (col_map, scope_name)."""

    # Block: multiple statements
    if hasattr(exp, 'Block') and isinstance(node, exp.Block):
        last_map, last_scope = {}, default_scope
        for stmt in node.expressions:
            last_map, last_scope = _process_node(
                stmt, dag, known_tables, parent_scope, block_index, default_scope)
        return last_map, last_scope

    if isinstance(node, exp.Insert):
        return _process_insert(node, dag, known_tables, parent_scope, block_index)
    if isinstance(node, exp.Select):
        return _process_select(node, dag, known_tables, parent_scope,
                               block_index, default_scope)
    if isinstance(node, exp.Union):
        return _process_union(node, dag, known_tables, parent_scope,
                              block_index, default_scope)
    if isinstance(node, exp.Subquery):
        alias = node.alias.upper() if node.alias else default_scope
        return _process_node(node.this, dag, known_tables, parent_scope,
                             block_index, alias)

    # Fallback
    insert = node.find(exp.Insert)
    if insert:
        return _process_insert(insert, dag, known_tables, parent_scope, block_index)
    select = node.find(exp.Select)
    if select:
        return _process_select(select, dag, known_tables, parent_scope,
                               block_index, default_scope)
    return {}, default_scope


# ---------------------------------------------------------------------------
# INSERT
# ---------------------------------------------------------------------------

def _process_insert(node, dag, known_tables, parent_scope, block_index):
    target_table = None
    target_node = node.this
    if isinstance(target_node, exp.Table):
        target_table = target_node.name.upper()
    elif node.find(exp.Table):
        target_table = node.find(exp.Table).name.upper()

    select_expr = node.expression
    if select_expr is None:
        return {}, target_table

    col_map, _ = _process_node(select_expr, dag, known_tables, parent_scope,
                                block_index, target_table or "__INSERT")

    if target_table and col_map:
        for out_col, sources in col_map.items():
            for item in sources:
                src_tbl, src_col = item[0], item[1]
                amb = item[2] if len(item) > 2 else False
                unq = item[3] if len(item) > 3 else False
                dag.add_edge(target_table, out_col, src_tbl, src_col,
                             block_index, target_table, "root", ambiguous=amb, unqualified=False)
        known_tables[target_table] = col_map
        dag.register_scope(target_table, col_map)

    return col_map, target_table


# ---------------------------------------------------------------------------
# UNION
# ---------------------------------------------------------------------------

def _process_union(node, dag, known_tables, parent_scope, block_index, scope_name):
    left_map, _ = _process_node(node.this, dag, known_tables, parent_scope,
                                 block_index, scope_name)
    right_map, _ = _process_node(node.expression, dag, known_tables, parent_scope,
                                  block_index, scope_name)
    merged = {}
    for col, sources in left_map.items():
        merged[col] = list(sources)
    for col, sources in right_map.items():
        if col in merged:
            merged[col].extend(sources)
        else:
            merged[col] = list(sources)
    return merged, scope_name


# ---------------------------------------------------------------------------
# SELECT — the heart
# ---------------------------------------------------------------------------

def _process_select(node, dag, known_tables, parent_scope, block_index, scope_name):
    logger.debug(f"  _process_select [{scope_name}] "
                 f"args: {[k for k,v in node.args.items() if v is not None and v is not False and v != []]}")

    # --- Step 1: CTEs (parent_scope={} — CTEs can't see main FROM) ---
    with_clause = node.args.get("with") or node.args.get("with_")
    if not with_clause and node.parent:
        pa = node.parent.args if hasattr(node.parent, 'args') else {}
        with_clause = pa.get("with") or pa.get("with_")

    if with_clause:
        for cte_node in with_clause.expressions:
            if not isinstance(cte_node, exp.CTE):
                continue
            cte_name = cte_node.alias_or_name.upper()
            cte_body = cte_node.this

            # CTEs get empty parent_scope — they can't see the main FROM
            # But they CAN see previously defined CTEs via known_tables
            cte_col_map, _ = _process_node(
                cte_body, dag, known_tables, {}, block_index, cte_name)

            for out_col, sources in cte_col_map.items():
                for item in sources:
                    src_tbl, src_col = item[0], item[1]
                    amb = item[2] if len(item) > 2 else False
                    unq = item[3] if len(item) > 3 else False
                    dag.add_edge(cte_name, out_col, src_tbl, src_col,
                                 block_index, cte_name, "cte", ambiguous=amb, unqualified=False)

            known_tables[cte_name] = cte_col_map
            dag.register_scope(cte_name, cte_col_map)

    # --- Step 2: Build own_scope (this SELECT's tables) and local_scope (own + parent) ---
    own_scope = {}  # only this SELECT's FROM/JOIN tables

    from_clause = node.args.get("from") or node.args.get("from_")
    if from_clause:
        _collect_from_sources(from_clause, own_scope, dag, known_tables,
                              parent_scope, block_index, scope_name)

    # --- Step 3: JOINs → add to own_scope ---
    joins = node.args.get("joins") or []
    for join_node in joins:
        _collect_from_sources(join_node, own_scope, dag, known_tables,
                              parent_scope, block_index, scope_name)

    # local_scope = own + parent (for qualified correlated refs)
    local_scope = dict(parent_scope)
    local_scope.update(own_scope)

    logger.debug(f"  [{scope_name}] own_scope: {list(own_scope.keys())}, "
                 f"inherited: {[k for k in parent_scope if k not in own_scope]}")

    # --- Step 3b: JOIN ON conditions ---
    for join_node in joins:
        on_clause = join_node.args.get("on")
        if on_clause:
            _gather_columns(on_clause, dag, known_tables, own_scope, local_scope,
                            block_index, scope_name, "JOIN_ON")

    # --- Step 4: WHERE ---
    where_clause = node.args.get("where")
    if where_clause:
        _gather_columns(where_clause, dag, known_tables, own_scope, local_scope,
                        block_index, scope_name, "WHERE")

    # --- Step 5: GROUP BY ---
    group_clause = node.args.get("group")
    if group_clause:
        _gather_columns(group_clause, dag, known_tables, own_scope, local_scope,
                        block_index, scope_name, "GROUP_BY")

    # --- Step 6: HAVING ---
    having_clause = node.args.get("having")
    if having_clause:
        _gather_columns(having_clause, dag, known_tables, own_scope, local_scope,
                        block_index, scope_name, "HAVING")

    # --- Step 7: ORDER BY ---
    order_clause = node.args.get("order")
    if order_clause:
        _gather_columns(order_clause, dag, known_tables, own_scope, local_scope,
                        block_index, scope_name, "ORDER_BY")

    # --- Step 8: SELECT expressions ---
    col_map = {}
    for sel_expr in node.expressions:
        _process_select_expr(sel_expr, dag, known_tables, own_scope, local_scope,
                             col_map, block_index, scope_name)

    return col_map, scope_name


# ---------------------------------------------------------------------------
# FROM/JOIN source collection (whitelist approach)
# ---------------------------------------------------------------------------

def _collect_from_sources(node, local_scope, dag, known_tables,
                          parent_scope, block_index, scope_name):
    """
    Collect Table and Subquery sources into local_scope.
    FROM subqueries get parent_scope (can't see sibling FROM tables).
    """
    if isinstance(node, exp.Table):
        tname = node.name.upper()
        talias = node.alias.upper() if node.alias else tname
        local_scope[talias] = tname
        return

    if isinstance(node, exp.Subquery):
        alias = node.alias.upper() if node.alias else "__SUBQ"
        # FROM subqueries get parent_scope, NOT local_scope
        sub_col_map, _ = _process_node(
            node.this, dag, known_tables, parent_scope, block_index, alias)
        for out_col, sources in sub_col_map.items():
            for item in sources:
                src_tbl, src_col = item[0], item[1]
                amb = item[2] if len(item) > 2 else False
                unq = item[3] if len(item) > 3 else False
                dag.add_edge(alias, out_col, src_tbl, src_col,
                             block_index, alias, "subquery", ambiguous=amb, unqualified=False)
        known_tables[alias] = sub_col_map
        local_scope[alias] = alias
        return

    if isinstance(node, exp.Join):
        table_expr = node.this
        if table_expr:
            _collect_from_sources(table_expr, local_scope, dag, known_tables,
                                  parent_scope, block_index, scope_name)
        return

    if isinstance(node, exp.From):
        table_expr = node.this
        if table_expr:
            _collect_from_sources(table_expr, local_scope, dag, known_tables,
                                  parent_scope, block_index, scope_name)
        for child in node.iter_expressions():
            if child is not node.this:
                _collect_from_sources(child, local_scope, dag, known_tables,
                                      parent_scope, block_index, scope_name)
        return

    # Other structural nodes
    for child in node.iter_expressions():
        if isinstance(child, (exp.Table, exp.Subquery, exp.Join, exp.From)):
            _collect_from_sources(child, local_scope, dag, known_tables,
                                  parent_scope, block_index, scope_name)


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------

def _resolve_column(col_name, col_table_ref, own_scope, local_scope, dag,
                     known_tables, block_index, scope_name, clause):
    """
    Resolve one column. Returns [(src_tbl, src_col, ambiguous, unqualified), ...].

    own_scope: {ALIAS: TABLE_NAME} — this SELECT's FROM/JOIN only
    local_scope: {ALIAS: TABLE_NAME} — own + parent (for qualified correlated refs)

    Qualified (t.col): look up alias in local_scope → ambiguous=False, unqualified=False
    Unqualified (col): look up in own_scope only → unqualified=True always
    """
    col_name = col_name.upper()

    if col_table_ref:
        col_table_ref = col_table_ref.upper()
        table_name = local_scope.get(col_table_ref)

        if table_name:
            if table_name in known_tables:
                col_map = known_tables[table_name]
                if col_name in col_map:
                    # Propagate from col_map — ensure 4-tuple
                    return [_ensure_4tuple(item, False) for item in col_map[col_name]]
                return [(table_name, col_name, False, False)]
            else:
                dag.add_leaf(table_name, col_name, block_index, scope_name, clause)
                return [(table_name, col_name, False, False)]

        if col_table_ref in known_tables:
            col_map = known_tables[col_table_ref]
            if col_name in col_map:
                return [_ensure_4tuple(item, False) for item in col_map[col_name]]
            return [(col_table_ref, col_name, False, False)]

        dag.add_leaf(col_table_ref, col_name, block_index, scope_name, clause)
        return [(col_table_ref, col_name, False, False)]

    # Unqualified column — own_scope ONLY, always unqualified=True
    if len(own_scope) == 0:
        return [("UNRESOLVED", col_name, False, True)]

    if len(own_scope) == 1:
        alias, table_name = next(iter(own_scope.items()))
        if table_name in known_tables:
            col_map = known_tables[table_name]
            if col_name in col_map:
                return [_ensure_4tuple(item, True) for item in col_map[col_name]]
            return [(table_name, col_name, False, True)]
        else:
            dag.add_leaf(table_name, col_name, block_index, scope_name, clause,
                         unqualified=True)
            return [(table_name, col_name, False, True)]

    # Multiple tables — disambiguate
    derived_matches = []
    base_matches = []
    for alias, table_name in own_scope.items():
        if table_name in known_tables:
            col_map = known_tables[table_name]
            if col_name in col_map:
                derived_matches.append((table_name, col_name))
        else:
            base_matches.append((table_name, col_name))

    if len(derived_matches) == 1 and not base_matches:
        return [(derived_matches[0][0], derived_matches[0][1], False, True)]
    if derived_matches and not base_matches:
        return [(t, c, True, True) for t, c in derived_matches]
    if len(base_matches) == 1 and not derived_matches:
        dag.add_leaf(base_matches[0][0], col_name, block_index, scope_name, clause,
                     unqualified=True)
        return [(base_matches[0][0], base_matches[0][1], False, True)]

    all_matches = derived_matches + base_matches
    for t, c in base_matches:
        dag.add_leaf(t, col_name, block_index, scope_name, clause,
                     unqualified=True)
    return [(t, c, True, True) for t, c in all_matches]


def _ensure_4tuple(item, unqualified_override):
    """Ensure item is a 4-tuple (src_tbl, src_col, ambiguous, unqualified)."""
    if len(item) == 4:
        return (item[0], item[1], item[2], unqualified_override or item[3])
    elif len(item) == 3:
        return (item[0], item[1], item[2], unqualified_override)
    else:
        return (item[0], item[1], False, unqualified_override)


# ---------------------------------------------------------------------------
# Gather columns from WHERE, GROUP BY, HAVING, ORDER BY, JOIN ON
# ---------------------------------------------------------------------------

def _gather_columns(node, dag, known_tables, own_scope, local_scope,
                     block_index, scope_name, clause):
    """
    Recursive walk. Handles arbitrary nesting: And→Not→Exists→Select etc.
    Column → resolve. Subquery/Select → new scope (gets local_scope as parent).
    Everything else → recurse.
    """
    for child in node.iter_expressions():
        if isinstance(child, exp.Column):
            if isinstance(child.this, exp.Star):
                continue
            col_name = child.name.upper()
            col_table_ref = child.table.upper() if child.table else ""
            sources = _resolve_column(col_name, col_table_ref, own_scope,
                                       local_scope, dag, known_tables,
                                       block_index, scope_name, clause)
            for item in sources:
                src_tbl, src_col = item[0], item[1]
                amb = item[2] if len(item) > 2 else False
                unq = item[3] if len(item) > 3 else False
                dag.add_edge(scope_name, col_name, src_tbl, src_col,
                             block_index, scope_name, "filter", clause,
                             ambiguous=amb, unqualified=unq)

        elif isinstance(child, (exp.Subquery, exp.Select)):
            # New scope — gets local_scope as parent_scope for correlated refs
            alias = "__WHERE_SUBQ"
            if isinstance(child, exp.Subquery) and child.alias:
                alias = child.alias.upper()
            _process_node(child, dag, known_tables, local_scope,
                          block_index, alias)

        else:
            _gather_columns(child, dag, known_tables, own_scope, local_scope,
                            block_index, scope_name, clause)


# ---------------------------------------------------------------------------
# SELECT expression processing
# ---------------------------------------------------------------------------

def _process_select_expr(sel_expr, dag, known_tables, own_scope, local_scope,
                          col_map, block_index, scope_name):
    # --- Star ---
    if isinstance(sel_expr, exp.Star):
        _expand_star(None, local_scope, col_map, dag, known_tables,
                     block_index, scope_name)
        return
    if isinstance(sel_expr, exp.Column) and isinstance(sel_expr.this, exp.Star):
        table_ref = sel_expr.table.upper() if sel_expr.table else None
        _expand_star(table_ref, local_scope, col_map, dag, known_tables,
                     block_index, scope_name)
        return

    # --- Output name ---
    if isinstance(sel_expr, exp.Alias):
        output_name = sel_expr.alias.upper()
    elif isinstance(sel_expr, exp.Column):
        output_name = sel_expr.name.upper()
    else:
        col = _find_column_shallow(sel_expr)
        output_name = col.name.upper() if col else None
        if not output_name:
            return

    # --- Resolve sources ---
    sources = []

    if isinstance(sel_expr, exp.Column):
        col_name = sel_expr.name.upper()
        col_ref = sel_expr.table.upper() if sel_expr.table else ""
        sources = _resolve_column(col_name, col_ref, own_scope, local_scope,
                                   dag, known_tables, block_index, scope_name, "SELECT")

    elif isinstance(sel_expr, exp.Alias):
        inner = sel_expr.this
        if isinstance(inner, exp.Column):
            col_name = inner.name.upper()
            col_ref = inner.table.upper() if inner.table else ""
            sources = _resolve_column(col_name, col_ref, own_scope, local_scope,
                                       dag, known_tables, block_index, scope_name, "SELECT")
        elif isinstance(inner, (exp.Subquery, exp.Select)):
            sub_map, _ = _process_node(inner, dag, known_tables, local_scope,
                                        block_index, "__SCALAR")
            for sub_col, sub_sources in sub_map.items():
                sources.extend(sub_sources)
        else:
            _collect_shallow_columns(inner, own_scope, local_scope, dag,
                                      known_tables, sources, block_index, scope_name)
            for subq in inner.find_all(exp.Subquery):
                sub_map, _ = _process_node(subq, dag, known_tables, local_scope,
                                            block_index, "__SCALAR")
                for sub_col, sub_sources in sub_map.items():
                    sources.extend(sub_sources)
    else:
        _collect_shallow_columns(sel_expr, own_scope, local_scope, dag,
                                  known_tables, sources, block_index, scope_name)
        for subq in sel_expr.find_all(exp.Subquery):
            sub_map, _ = _process_node(subq, dag, known_tables, local_scope,
                                        block_index, "__SCALAR")
            for sub_col, sub_sources in sub_map.items():
                sources.extend(sub_sources)

    if sources:
        col_map[output_name] = sources


def _expand_star(table_ref, local_scope, col_map, dag, known_tables,
                  block_index, scope_name):
    targets = local_scope
    if table_ref:
        if table_ref in local_scope:
            targets = {table_ref: local_scope[table_ref]}
        else:
            return

    for alias, table_name in targets.items():
        if table_name in known_tables:
            scope_col_map = known_tables[table_name]
            for col in scope_col_map:
                if col:
                    col_map[col] = [(table_name, col, False)]
        else:
            dag.add_leaf(table_name, "*", block_index, scope_name, "SELECT")
            col_map["*"] = [(table_name, "*", False)]


def _find_column_shallow(node):
    for child in node.iter_expressions():
        if isinstance(child, exp.Column) and not isinstance(child.this, exp.Star):
            return child
        if isinstance(child, (exp.Subquery, exp.Select)):
            continue
        result = _find_column_shallow(child)
        if result:
            return result
    return None


def _collect_shallow_columns(node, own_scope, local_scope, dag, known_tables,
                              sources, block_index, scope_name):
    for child in node.iter_expressions():
        if isinstance(child, exp.Column):
            if isinstance(child.this, exp.Star):
                continue
            col_name = child.name.upper()
            col_ref = child.table.upper() if child.table else ""
            resolved = _resolve_column(col_name, col_ref, own_scope, local_scope,
                                        dag, known_tables, block_index,
                                        scope_name, "SELECT")
            sources.extend(resolved)
        elif isinstance(child, (exp.Subquery, exp.Select)):
            continue
        else:
            _collect_shallow_columns(child, own_scope, local_scope, dag,
                                      known_tables, sources, block_index,
                                      scope_name)


# ===========================================================================
# Phase 4: Output
# ===========================================================================

def generate_report(block_results, dag, output_path):
    report = {
        "summary": {
            "total_blocks": len(block_results),
            "total_base_tables": len(dag.get_base_tables()),
            "total_base_columns": len(dag.get_leaf_columns()),
            "base_tables": dag.get_base_tables(),
            "base_columns": dag.get_leaf_columns(),
        },
        "lineage_edges": dag.edges,
        "blocks": [{
            "block_index": br["block_index"],
            "insert_target": br["insert_target"],
            "ctes": br["ctes_found"],
            "base_tables": br["base_tables_found"],
            "parse_errors": br["parse_errors"],
        } for br in block_results],
    }
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)
    logger.info(f"JSON report written to {output_path}")
    return report


def build_dataframe(dag):
    if not dag.edges:
        return pd.DataFrame() if PANDAS_AVAILABLE else []
    if not PANDAS_AVAILABLE:
        return dag.edges
    df = pd.DataFrame(dag.edges)
    return df.sort_values(
        ["block_index", "scope_type", "output_table", "output_column"]
    ).reset_index(drop=True)


# ===========================================================================
# Main
# ===========================================================================

def analyze_file(input_path: str, output_path: str = "analysis_results.json",
                 raise_on_error: bool = False):
    logger.info(f"Reading {input_path}")
    text = Path(input_path).read_text(encoding='utf-8', errors='replace')

    raw_blocks = extract_execute_immediate_blocks(text)
    extraction_mode = "execute_immediate"
    if not raw_blocks:
        logger.info("No EXECUTE IMMEDIATE. Trying direct SQL...")
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
                f"  Block {i + 1}: {len(res['base_tables_found'])} base tables, "
                f"{len(res['ctes_found'])} CTEs"
                + (f", INSERT INTO {res['insert_target']}" if res['insert_target'] else ""))

    # Raise if any block had errors
    if raise_on_error:
        all_errors = []
        for br in block_results:
            for err in br["parse_errors"]:
                all_errors.append(f"Block {br['block_index']}: {err}")
        if all_errors:
            raise RuntimeError(
                f"Parse errors in {input_path}:\n" + "\n".join(all_errors)
            )

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
        description="Analyze Oracle PL/SQL scripts for base tables and columns")
    parser.add_argument("input", help="Path to the .sql file")
    parser.add_argument("--output", "-o", default="analysis_results.json")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    analyze_file(args.input, args.output)
