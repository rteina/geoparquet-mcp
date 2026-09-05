"""The error vocabulary of the engine.

Every failure a caller can provoke is reported as one of these. The contract
is deliberately stronger than "don't crash": an engine error must name what
was wrong *and* what a caller could pass instead. The consumer of these
messages is usually a language model deciding what to try next, and a message
that only says "Binder Error: Referenced column not found" costs it a turn.

Raw `duckdb.Error` never reaches a caller. It is caught at the boundary and
re-raised as `RemoteReadError` with the SQL that produced it attached.
"""

from __future__ import annotations


class EngineError(Exception):
    """Base class for every failure the engine reports."""


class InvalidRequestError(EngineError, ValueError):
    """A caller supplied arguments the engine cannot act on.

    Also a `ValueError` so that callers written against the standard
    exception hierarchy keep working.
    """


class UnknownSourceError(InvalidRequestError, KeyError):
    """A dataset name that is not in the scope the engine was built with.

    Also a `KeyError` because a source registry is a mapping, and callers
    written before the scope existed catch `KeyError`.
    """

    def __str__(self) -> str:  # KeyError would otherwise repr() the message
        return self.args[0] if self.args else ""


class ScopeViolationError(EngineError):
    """A read was attempted against a path outside the engine's scope.

    Distinct from `UnknownSourceError`: that one is a typo, this one is the
    boundary refusing a path that was constructed rather than named. It is not
    an `InvalidRequestError` because reaching it means a bug or an attempt,
    not a mistyped argument.
    """


class UnknownColumnError(InvalidRequestError):
    """A column name that is not in the dataset's Parquet schema."""


class CapabilityUnavailableError(EngineError):
    """An optional DuckDB extension the operation needs could not be loaded.

    Raised instead of silently degrading, so a caller can tell "no H3 cells
    here" from "this build cannot compute H3 cells".
    """


class RemoteReadError(EngineError):
    """DuckDB failed while reading the remote dataset.

    Wraps the underlying `duckdb.Error`; the SQL is attached so the failure
    can be reproduced by hand.
    """

    def __init__(self, message: str, sql: str | None = None) -> None:
        super().__init__(message)
        self.sql = sql
