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
# Phase 3: DAG-based lineage using sqlglot.optimizer.scope.traverse_scope
# ===========================================================================
#
# The DAG edge table is the SINGLE SOURCE OF TRUTH.
# Every edge: (output_table, output_column) → (source_table, source_column)
# Base tables are leaf nodes: (base_table, col) → (None, None)
#
# df_edges = the raw DAG
# df_columns = DERIVED from df_edges by tracing root edges to leaf edges
#
# No separate resolution path. Everything comes from the edges.

try:
    from sqlglot.optimizer.scope import traverse_scope, Scope, ScopeType
    SCOPE_AVAILABLE = True
except ImportError:
    SCOPE_AVAILABLE = False


class LineageDAG:
    """
    Column lineage as an edge table.
    Each edge: (output_table, output_column) → (source_table, source_column)
    Base table leaf edges have source_table = None, source_column = None.
    """

    def __init__(self):
        self.edges = []
        # Index: (out_tbl, out_col) -> [(src_tbl, src_col), ...]
        self._children = {}
        # Scope column maps for cross-scope/cross-block lookup
        self._scope_columns = {}  # scope_name_upper -> {col: [(src_tbl, src_col)]}
        # Track which (table, col) already have a base edge to avoid duplicates
        self._base_edges_added = set()

    def add_edge(self, output_table, output_column,
                 source_table, source_column,
                 block_index, scope_name, scope_type, clause="SELECT"):
        """Add one lineage edge. Deduplicates automatically."""
        ot = output_table.upper() if output_table else None
        oc = output_column.upper() if output_column else None
        st = source_table.upper() if source_table else None
        sc = source_column.upper() if source_column else None

        # Deduplicate
        dedup_key = (ot, oc, st, sc, scope_type, clause)
        if hasattr(self, '_seen_edges'):
            if dedup_key in self._seen_edges:
                return
        else:
            self._seen_edges = set()
        self._seen_edges.add(dedup_key)

        edge = {
            "output_table": ot,
            "output_column": oc,
            "source_table": st,
            "source_column": sc,
            "block_index": block_index,
            "scope_name": scope_name,
            "scope_type": scope_type,
            "clause": clause,
        }
        self.edges.append(edge)

        # Index
        out_key = (ot, oc)
        if out_key not in self._children:
            self._children[out_key] = []
        if st is not None:
            self._children[out_key].append((st, sc))

    def add_base_leaf(self, table, column, block_index, scope_name, clause="SELECT"):
        """Add a base table leaf edge: (table, col) → (None, None)."""
        key = (table.upper(), column.upper())
        if key in self._base_edges_added:
            return
        self._base_edges_added.add(key)
        self.add_edge(table, column, None, None,
                      block_index, scope_name, "base", clause)

    def register_scope_columns(self, scope_name, col_map):
        self._scope_columns[scope_name.upper()] = col_map

    def get_scope_columns(self, scope_name):
        return self._scope_columns.get(scope_name.upper(), {})

    def has_scope(self, scope_name):
        return scope_name.upper() in self._scope_columns

    def get_base_tables(self):
        """Base tables = tables that have leaf edges (source_table is None)."""
        return sorted({e["output_table"] for e in self.edges
                       if e["scope_type"] == "base" and e["output_table"]})

    def get_leaf_columns(self):
        """All (table, column) pairs from base/leaf edges."""
        return sorted({(e["output_table"], e["output_column"])
                       for e in self.edges
                       if e["scope_type"] == "base"
                       and e["output_table"] and e["output_column"]})

    def get_root_edges(self, block_index=None):
        """Get edges from root scope (the final SELECT output)."""
        roots = [e for e in self.edges if e["scope_type"] == "root"]
        if block_index is not None:
            roots = [e for e in roots if e["block_index"] == block_index]
        return roots

    def trace_to_leaves(self, table, column, _visited=None):
        """
        Follow children edges down to base table leaves.
        Returns list of (base_table, base_column).
        """
        if _visited is None:
            _visited = set()
        key = (table.upper() if table else None,
               column.upper() if column else None)
        if key in _visited:
            return [key]
        _visited.add(key)

        children = self._children.get(key, [])
        if not children:
            return [key]  # leaf

        results = []
        for src_tbl, src_col in children:
            results.extend(self.trace_to_leaves(src_tbl, src_col, _visited.copy()))
        return results if results else [key]


# ===========================================================================
# Phase 3b: Walk scopes, build edges
# ===========================================================================

def process_block(cleaned_sql: str, dag: LineageDAG,
                  block_index: int) -> dict:
    """
    Parse one SQL block, traverse scopes, add all lineage edges to the DAG.
    """
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

    # --- Traverse scopes (post-order: CTEs first, main SELECT last) ---
    try:
        scopes = traverse_scope(parsed)
    except Exception as e:
        result["parse_errors"].append(f"traverse_scope error: {e}")
        return result

    scope_col_maps = {}  # id(scope) -> col_map
    main_col_map = {}

    for scope in scopes:
        scope_type = scope.scope_type
        scope_name = _get_scope_name(scope)

        # Build column map for this scope AND add edges to the DAG
        col_map = _process_scope(scope, dag, scope_col_maps,
                                  block_index, scope_name)
        scope_col_maps[id(scope)] = col_map

        if scope_type == ScopeType.CTE:
            cte_name = _get_cte_name(scope)
            if cte_name:
                result["ctes_found"].append(cte_name)
                dag.register_scope_columns(cte_name, col_map)
                # CTE edges: cte.col → source.col
                for out_col, sources in col_map.items():
                    for src_tbl, src_col in sources:
                        dag.add_edge(cte_name, out_col, src_tbl, src_col,
                                     block_index, cte_name, "cte")

        if scope_type == ScopeType.ROOT:
            main_col_map = col_map

    # --- Root scope edges ---
    root_name = insert_target if insert_target else f"__ROOT_BLOCK_{block_index}"

    for out_col, sources in main_col_map.items():
        for src_tbl, src_col in sources:
            dag.add_edge(root_name, out_col, src_tbl, src_col,
                         block_index, root_name, "root")

    dag.register_scope_columns(root_name, main_col_map)

    # --- Filter column edges (WHERE, GROUP BY, etc.) from root scope ---
    for scope in scopes:
        if scope.scope_type == ScopeType.ROOT:
            _add_filter_edges(scope, dag, scope_col_maps,
                              block_index, root_name)
            break

    result["base_tables_found"] = dag.get_base_tables()
    return result


def _get_scope_name(scope) -> str:
    if scope.scope_type == ScopeType.CTE:
        return _get_cte_name(scope) or "CTE_UNKNOWN"
    elif scope.scope_type == ScopeType.ROOT:
        return "__ROOT"
    elif scope.scope_type == ScopeType.SUBQUERY:
        return "__SUBQUERY"
    elif scope.scope_type == ScopeType.DERIVED_TABLE:
        alias = scope.expression.parent.alias if scope.expression.parent else None
        return alias.upper() if alias else "__DERIVED"
    return "__UNKNOWN"


def _get_cte_name(scope) -> Optional[str]:
    parent = scope.expression.parent
    if parent and hasattr(parent, 'alias') and parent.alias:
        return parent.alias.upper()
    return None


def _process_scope(scope, dag, scope_col_maps, block_index, scope_name):
    """
    Build {OUTPUT_COL: [(source_table, source_col), ...]} for one scope.
    Also adds base leaf edges for physical table columns.
    """
    col_map = {}
    select_node = scope.expression
    if not isinstance(select_node, exp.Select):
        return col_map

    # Build source lookup from scope.sources
    source_info = {}  # alias_upper -> (actual_name, is_scope_like, scope_ref)
    # Note: is_scope_like=True means it's either a sqlglot Scope OR a table
    # that we know is derived (from a previous block's INSERT INTO).
    for alias_name, source in scope.sources.items():
        alias_upper = alias_name.upper()
        if isinstance(source, exp.Table):
            tname = source.name.upper()
            # Check if this "physical" table is actually a derived table
            # from a previous EXECUTE IMMEDIATE block (INSERT INTO target)
            if dag.has_scope(tname):
                # Treat it like a scope — we know its columns from the DAG
                source_info[alias_upper] = (tname, True, None)
            else:
                source_info[alias_upper] = (tname, False, None)
        elif isinstance(source, Scope):
            source_info[alias_upper] = (alias_upper, True, source)

    # Process SELECT expressions
    for sel_expr in select_node.selects:
        _process_select_expr(sel_expr, source_info, scope, dag,
                             scope_col_maps, col_map, block_index, scope_name)

    return col_map


def _process_select_expr(sel_expr, source_info, scope, dag,
                          scope_col_maps, col_map, block_index, scope_name):
    """Process one SELECT expression, populate col_map, add base leaf edges."""

    # Handle * and t.*
    if isinstance(sel_expr, exp.Star):
        _expand_star(None, source_info, scope, dag, scope_col_maps,
                     col_map, block_index, scope_name)
        return
    if isinstance(sel_expr, exp.Column) and isinstance(sel_expr.this, exp.Star):
        table_ref = sel_expr.table.upper() if sel_expr.table else None
        _expand_star(table_ref, source_info, scope, dag, scope_col_maps,
                     col_map, block_index, scope_name)
        return

    # Output name
    if isinstance(sel_expr, exp.Alias):
        output_name = sel_expr.alias.upper()
    elif isinstance(sel_expr, exp.Column):
        output_name = sel_expr.name.upper()
    else:
        col = sel_expr.find(exp.Column)
        if col and not isinstance(col.this, exp.Star):
            output_name = col.name.upper()
        else:
            return

    # Resolve column references
    sources = []
    for col_node in sel_expr.find_all(exp.Column):
        if isinstance(col_node.this, exp.Star):
            continue
        col_name = col_node.name.upper()
        col_table_ref = col_node.table.upper() if col_node.table else ""

        resolved = _resolve_column(col_name, col_table_ref, source_info,
                                    scope, dag, scope_col_maps,
                                    block_index, scope_name)
        sources.extend(resolved)

    if sources:
        col_map[output_name] = sources


def _expand_star(table_ref, source_info, scope, dag, scope_col_maps,
                  col_map, block_index, scope_name):
    """Expand * or t.* by looking up source columns."""
    targets = {table_ref: source_info.get(table_ref)} if table_ref else source_info

    for alias_upper, info in targets.items():
        if info is None:
            continue
        actual_name, is_scope_like, scope_ref = info

        if is_scope_like:
            if scope_ref is not None:
                # Within-block scope (CTE/subquery)
                sub_map = scope_col_maps.get(id(scope_ref), {})
                if not sub_map:
                    sub_map = dag.get_scope_columns(alias_upper)
            else:
                # Cross-block derived table
                sub_map = dag.get_scope_columns(actual_name)

            for sub_col in sub_map:
                if sub_col:
                    col_map[sub_col] = [(actual_name, sub_col)]
        else:
            # Physical table — check if we know its columns
            known = dag.get_scope_columns(actual_name)
            if known:
                for col in known:
                    if col:
                        col_map[col] = [(actual_name, col)]
            else:
                logger.debug(f"    * from base table {actual_name} — columns unknown")


def _resolve_column(col_name, col_table_ref, source_info,
                     scope, dag, scope_col_maps,
                     block_index, scope_name):
    """
    Resolve one column. If it resolves to a physical table (not known as
    derived in the DAG), add a base leaf edge.
    If it resolves to a table known in the DAG from a previous block,
    return its scope columns so the trace continues through.
    """
    if col_table_ref:
        info = source_info.get(col_table_ref)
        if info:
            actual_name, is_scope_like, scope_ref = info
            if is_scope_like:
                if scope_ref is not None:
                    # sqlglot Scope (CTE/subquery within this block)
                    sub_map = scope_col_maps.get(id(scope_ref), {})
                    if col_name in sub_map:
                        return sub_map[col_name]
                    return [(actual_name, col_name)]
                else:
                    # Cross-block derived table (INSERT INTO from earlier block)
                    cross_map = dag.get_scope_columns(actual_name)
                    if col_name in cross_map:
                        return cross_map[col_name]
                    return [(actual_name, col_name)]
            else:
                # Truly physical table — add base leaf edge
                dag.add_base_leaf(actual_name, col_name,
                                  block_index, scope_name)
                return [(actual_name, col_name)]
        # Unknown alias — check DAG before assuming base
        if dag.has_scope(col_table_ref):
            cross_map = dag.get_scope_columns(col_table_ref)
            if col_name in cross_map:
                return cross_map[col_name]
            return [(col_table_ref, col_name)]
        dag.add_base_leaf(col_table_ref, col_name, block_index, scope_name)
        return [(col_table_ref, col_name)]

    # Unqualified — single source
    if len(source_info) == 1:
        alias_upper, (actual_name, is_scope_like, scope_ref) = next(iter(source_info.items()))
        if is_scope_like:
            if scope_ref is not None:
                sub_map = scope_col_maps.get(id(scope_ref), {})
                if col_name in sub_map:
                    return sub_map[col_name]
            else:
                cross_map = dag.get_scope_columns(actual_name)
                if col_name in cross_map:
                    return cross_map[col_name]
            return [(actual_name, col_name)]
        dag.add_base_leaf(actual_name, col_name, block_index, scope_name)
        return [(actual_name, col_name)]

    # Multiple sources — disambiguate
    scope_matches = []
    base_matches = []
    for alias_upper, (actual_name, is_scope_like, scope_ref) in source_info.items():
        if is_scope_like:
            if scope_ref is not None:
                sub_map = scope_col_maps.get(id(scope_ref), {})
            else:
                sub_map = dag.get_scope_columns(actual_name)
            if col_name in sub_map:
                scope_matches.append((actual_name, col_name))
        else:
            base_matches.append((actual_name, col_name))

    if len(scope_matches) == 1:
        return scope_matches
    if scope_matches:
        return scope_matches
    if len(base_matches) == 1:
        tbl = base_matches[0][0]
        dag.add_base_leaf(tbl, col_name, block_index, scope_name)
        return base_matches
    if base_matches:
        for tbl, col in base_matches:
            dag.add_base_leaf(tbl, col, block_index, scope_name)
        return base_matches

    return [("UNRESOLVED", col_name)]


def _add_filter_edges(scope, dag, scope_col_maps, block_index, root_name):
    """Add edges for WHERE, JOIN ON, GROUP BY, HAVING, ORDER BY columns."""
    source_info = {}
    for alias_name, source in scope.sources.items():
        alias_upper = alias_name.upper()
        if isinstance(source, exp.Table):
            tname = source.name.upper()
            if dag.has_scope(tname):
                source_info[alias_upper] = (tname, True, None)
            else:
                source_info[alias_upper] = (tname, False, None)
        elif isinstance(source, Scope):
            source_info[alias_upper] = (alias_upper, True, source)

    for col_node in scope.columns:
        if isinstance(col_node.this, exp.Star):
            continue
        col_name = col_node.name.upper()
        col_table_ref = col_node.table.upper() if col_node.table else ""

        clause = _detect_clause(col_node)
        if clause == "SELECT":
            continue

        resolved = _resolve_column(col_name, col_table_ref, source_info,
                                    scope, dag, scope_col_maps,
                                    block_index, root_name)
        for src_tbl, src_col in resolved:
            dag.add_edge(root_name, col_name, src_tbl, src_col,
                         block_index, root_name, "filter", clause)


def _detect_clause(col_node) -> str:
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


# ===========================================================================
# Phase 4: Output — everything derived from the DAG edges
# ===========================================================================

def generate_report(block_results: list, dag: LineageDAG,
                    output_path: str) -> dict:
    base_tables = dag.get_base_tables()
    leaf_columns = dag.get_leaf_columns()

    report = {
        "summary": {
            "total_blocks": len(block_results),
            "total_base_tables": len(base_tables),
            "total_base_columns": len(leaf_columns),
            "base_tables": base_tables,
            "base_columns": leaf_columns,
        },
        "lineage_edges": dag.edges,
        "blocks": [
            {
                "block_index": br["block_index"],
                "insert_target": br["insert_target"],
                "ctes": br["ctes_found"],
                "base_tables_referenced": br["base_tables_found"],
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
    Build TWO DataFrames, both derived from the DAG edges.

    df_edges: the raw DAG — one row per edge.
        Filtering:
            df_edges[df_edges.scope_type == 'base']      # leaf/base edges
            df_edges[df_edges.scope_type == 'root']       # final output edges
            df_edges[df_edges.output_table == 'SEQ_ALERTS']  # what feeds a CTE

    df_columns: flattened view — for each root output column, trace to base.
        One row per (root_output_col, base_table, base_column).
        Filtering:
            df_columns                                   # all base columns
            df_columns[df_columns.clause == 'SELECT']    # SELECT only
            df_columns.groupby('base_table')['base_column'].apply(set)

    Returns (df_columns, df_edges)
    """
    # --- df_edges ---
    edge_records = dag.edges

    # --- df_columns: derived by tracing root edges to leaves ---
    col_records = []
    root_edges = [e for e in dag.edges if e["scope_type"] in ("root", "filter")]

    for e in root_edges:
        out_col = e["output_column"]
        src_tbl = e["source_table"]
        src_col = e["source_column"]
        clause = e["clause"]
        block_idx = e["block_index"]

        if src_tbl is None:
            continue

        # Trace to base table leaves
        base_list = dag.trace_to_leaves(src_tbl, src_col)
        for base_tbl, base_col in base_list:
            col_records.append({
                "block_index": block_idx,
                "output_column": out_col,
                "source_table": src_tbl,
                "source_column": src_col,
                "base_table": base_tbl,
                "base_column": base_col,
                "clause": clause,
            })

    if not PANDAS_AVAILABLE:
        logger.warning("pandas not installed — returning lists of dicts")
        return col_records, edge_records

    df_edges = pd.DataFrame(edge_records) if edge_records else pd.DataFrame()
    df_columns = pd.DataFrame(col_records) if col_records else pd.DataFrame()

    if not df_edges.empty:
        df_edges = df_edges.sort_values(
            ["block_index", "scope_type", "output_table", "output_column"]
        ).reset_index(drop=True)

    if not df_columns.empty:
        df_columns = df_columns.drop_duplicates(
            subset=["block_index", "base_table", "base_column", "clause"]
        ).sort_values(
            ["block_index", "base_table", "base_column", "clause"]
        ).reset_index(drop=True)

    return df_columns, df_edges


# ===========================================================================
# Main
# ===========================================================================

def analyze_file(input_path: str, output_path: str = "analysis_results.json"):
    """
    Full pipeline. Returns (report_dict, df_columns, df_edges).

    df_columns: flattened base table columns, derived from the DAG.
    df_edges: the full lineage DAG edge table.
    """
    logger.info(f"Reading {input_path}")
    text = Path(input_path).read_text(encoding='utf-8', errors='replace')

    raw_blocks = extract_execute_immediate_blocks(text)
    if not raw_blocks:
        logger.warning("No EXECUTE IMMEDIATE blocks found!")
        logger.info("Attempting to parse entire file as plain SQL...")
        raw_blocks = [text]

    dag = LineageDAG()
    block_results = []

    for i, raw_sql in enumerate(raw_blocks):
        logger.info(f"Processing block {i + 1}/{len(raw_blocks)}")

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

    report = generate_report(block_results, dag, output_path)
    df_columns, df_edges = build_dataframe(dag)

    print(f"\n{'=' * 60}")
    print(f"  Analysis Complete")
    print(f"{'=' * 60}")
    print(f"  Blocks analyzed:    {len(block_results)}")
    print(f"  Base tables:        {dag.get_base_tables()}")
    print(f"  Base columns:       {len(dag.get_leaf_columns())}")
    print(f"  Lineage edges:      {len(dag.edges)}")
    print(f"  JSON report:        {output_path}")
    print(f"{'=' * 60}\n")

    return report, df_columns, df_edges


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
