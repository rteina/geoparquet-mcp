"""Read-only SQL against the datasets in scope.

The other operations each answer one shape of question. This one lets a caller
ask a question nobody anticipated — a join, a window, a `HAVING` — without
handing it the database.

How the perimeter survives raw SQL
----------------------------------
The danger is not `DROP TABLE`: there is nothing to drop, the database is an
empty in-memory instance and every byte lives in a remote file. The danger is
`read_parquet('s3://someone-elses-bucket/...')`, which turns the engine into an
open HTTP proxy that reads any object DuckDB can reach and returns it.

So the guard is not a keyword blocklist over the query text — those lose to a
comment or an unusual amount of whitespace. The statement is parsed by DuckDB
itself, twice, before anything runs:

1. `extract_statements` must yield exactly one statement, of type `SELECT`.
2. `json_serialize_sql` renders that statement's syntax tree, and the tree is
   walked. Every table reference must resolve to a view this scope registered,
   and no table function may appear at all. A table function is the only way
   to name a path in DuckDB SQL, so refusing all of them is what closes the
   hole — and it costs nothing, because the scope's datasets are already
   reachable by name through the views.

The result is that the set of readable bytes is identical to the set the typed
operations can reach: exactly the scope, and nothing else.
"""

from __future__ import annotations

import json
import re
from typing import Any

import duckdb

from geoparquet_mcp.engine import sources
from geoparquet_mcp.engine.errors import InvalidRequestError
from geoparquet_mcp.engine.session import (
    MAX_ROW_LIMIT,
    Measurement,
    Session,
    clamp_limit,
    get_session,
)
from geoparquet_mcp.engine.sources import DatasetScope

# Default ceiling on bytes pulled over the network by one ad-hoc query.
DEFAULT_MAX_BYTES = 200_000_000

# A view name must be a bare identifier: it is interpolated into `CREATE VIEW`.
_VIEW_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def view_names(scope: DatasetScope) -> list[str]:
    """The table names an ad-hoc query may reference, one per dataset in scope."""
    return sorted(name for name in scope.names if _VIEW_NAME.match(name))


def _require_single_select(sql: str) -> None:
    """Reject anything that is not exactly one SELECT, before it is bound."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise InvalidRequestError(f"could not parse the SQL: {exc}") from exc
    if len(statements) != 1:
        raise InvalidRequestError(
            f"expected exactly one statement, got {len(statements)}; run one SELECT at a time"
        )
    # Compared by value, not identity: DuckDB hands back an enum member from
    # its extension module, which is equal to but not the same object as the
    # one re-exported on the `duckdb` package.
    kind = statements[0].type
    if kind != duckdb.StatementType.SELECT:
        raise InvalidRequestError(
            f"only SELECT is allowed here, and this is a {kind.name} statement. "
            f"This engine reads remote files in place; nothing can be written."
        )


def _syntax_tree(measurement: Measurement, sql: str) -> dict[str, Any]:
    """DuckDB's own parse tree for a statement, as plain JSON.

    Parsing is local: no remote file is touched to produce it.
    """
    document = json.loads(measurement.one("SELECT json_serialize_sql(?) AS tree", [sql])["tree"])
    if document.get("error"):
        raise InvalidRequestError(
            f"could not parse the SQL: {document.get('error_message', 'unknown parse error')}"
        )
    return document


def _walk(node: Any) -> Any:
    """Every dict in a syntax tree, in no particular order."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _assert_reads_only_the_scope(tree: dict[str, Any], allowed: list[str]) -> list[str]:
    """Check every table reference against the scope, and return the ones used.

    Common table expressions are collected first: a `WITH` name is a reference
    to the query's own body, not to a table, so it must be allowed without
    being a dataset.
    """
    nodes = list(_walk(tree))
    cte_names = {
        entry["key"]
        for node in nodes
        for entry in (node.get("cte_map") or {}).get("map") or []
        if isinstance(entry, dict) and isinstance(entry.get("key"), str)
    }

    referenced: set[str] = set()
    for node in nodes:
        kind = node.get("type")
        if kind == "TABLE_FUNCTION":
            function = node.get("function") or {}
            name = function.get("function_name") or "a table function"
            raise InvalidRequestError(
                f"table functions are not available here ({name}). The datasets in "
                f"scope are already queryable by name: {', '.join(allowed)}. Naming a "
                f"file directly is what the scope exists to prevent."
            )
        if kind == "BASE_TABLE":
            name = node.get("table_name")
            if name in cte_names:
                continue
            if name not in allowed:
                raise InvalidRequestError(
                    f"unknown table {name!r}. Query the datasets in scope by name: "
                    f"{', '.join(allowed)}."
                )
            referenced.add(name)
    return sorted(referenced)


def _register_views(measurement: Measurement, scope: DatasetScope, names: list[str]) -> None:
    """Bind each in-scope dataset to a view name on the measured cursor.

    Only the datasets the query actually named are bound, so an unused source
    costs no footer read.
    """
    for name in names:
        measurement.execute(
            f"CREATE OR REPLACE TEMP VIEW {name} AS "
            f"SELECT * FROM read_parquet('{scope.target(name)}')"
        )


def run_sql(
    sql: str,
    max_rows: int = 100,
    max_bytes: int = DEFAULT_MAX_BYTES,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Run one read-only SELECT against the datasets in scope.

    Contract: each dataset in scope is available as a view named after it, and
    those views are the only readable tables — see this module's docstring for
    how that is enforced. The statement is wrapped in an outer `LIMIT` so the
    row ceiling holds whatever the query asks for.

    On the byte ceiling: `max_bytes` is checked after the query, not before,
    and the result says so in `byte_budget_exceeded`. There is no honest way
    to make it pre-emptive — DuckDB cannot abort a scan on bytes transferred,
    and a plan gives no reliable estimate of them. So the rows are returned
    (they have already been paid for) together with the verdict, which is the
    signal to narrow the next query. The row ceiling, by contrast, is real.

    Prefer a typed operation when one fits: they push their filters down by
    construction, whereas an ad-hoc query pushes down only what its `WHERE`
    clause happens to express over the `bbox` struct.
    """
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise InvalidRequestError("`sql` is empty; pass a SELECT statement")
    _require_single_select(text)

    scope = scope if scope is not None else sources.default_scope()
    active = session if session is not None else get_session()
    allowed = view_names(scope)
    row_limit = clamp_limit(max_rows)

    with active.measure() as measurement:
        tree = _syntax_tree(measurement, text)
        referenced = _assert_reads_only_the_scope(tree, allowed)
        _register_views(measurement, scope, referenced)
        wrapped = f"SELECT * FROM ({text}) LIMIT {row_limit}"
        rows = measurement.records(wrapped)

    report = measurement.report.as_dict()
    return {
        "release": scope.release,
        "tables_available": allowed,
        "tables_read": referenced,
        "sql": text,
        "executed_sql": wrapped,
        "row_count": len(rows),
        "rows": rows,
        "row_limit": row_limit,
        "truncated": len(rows) == row_limit,
        "max_row_limit": MAX_ROW_LIMIT,
        "byte_budget": max_bytes,
        "byte_budget_exceeded": report["bytes_scanned"] > max_bytes,
        "scan": report,
    }


# The guidance a caller needs to write a *good* query against this engine, not
# merely a legal one. It lives here, beside the dialect it describes, for two
# reasons: it has to contain SQL, and the tool layer is required to contain
# none — in its prose as much as in its code. `tools/sql.py` imports it as the
# MCP tool description.
USAGE = """\
Run one read-only SELECT against the datasets in scope, for questions the \
other tools do not have a shape for.

WHEN TO USE IT. Last, not first. The typed tools push their filters into the \
remote Parquet file by construction; an ad-hoc query pushes down only what its \
WHERE clause happens to express, so a query that forgets a bbox predicate can \
read gigabytes to answer something `geoparquet_aggregate_attribute` would have \
answered in kilobytes. Reach for it for genuine gaps: a join between two \
datasets, a HAVING clause, a window function, a self-join.

WHAT YOU CAN QUERY. Each dataset in scope is a table named exactly as the \
dataset is — `overture_places`, `overture_divisions`, `overture_buildings` — \
and those are the only tables that exist. There is no way to name a file: \
table functions such as read_parquet are refused, and so is anything that is \
not a single SELECT. That is a perimeter, not a lint rule.

WRITING A FAST ONE. Constrain `bbox` explicitly, as four comparisons on its \
members, because that is the form Parquet statistics can prune on:

  SELECT categories.primary AS category, count(*) AS n
  FROM overture_places
  WHERE bbox.xmin <= 2.40 AND bbox.xmax >= 2.30
    AND bbox.ymin <= 48.88 AND bbox.ymax >= 48.85
  GROUP BY 1 ORDER BY n DESC

Writing that filter with a geometry function instead would be correct and \
would read the entire file, because the Parquet reader cannot see through it. \
Select named columns rather than *, for the same reason: unread columns are \
unfetched.

PARAMETERS.
  sql: one SELECT statement.
  max_rows: row ceiling, applied as an outer LIMIT. Hard, and capped at 1000.
  max_bytes: byte ceiling. Reported, not pre-emptive — see below.

WHAT COMES BACK. `rows`, `tables_read`, the `executed_sql` actually run, the \
`scan` block, and `byte_budget_exceeded`. That last one is a verdict after the \
fact, not a brake: DuckDB cannot abort a scan on bytes already transferred, so \
the rows are returned — they have been paid for — and the flag tells you the \
query was too expensive and the next one should be narrower. The row ceiling, \
by contrast, is enforced."""


__all__ = ["DEFAULT_MAX_BYTES", "USAGE", "run_sql", "view_names"]
