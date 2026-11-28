"""Per-page-run read cache.

Streamlit re-runs the whole script on every click, and the same read helpers
(profile, credit ledger, staged actions, reconciliation ...) are called several
times per run from the sidebar, status bar, alerts and panel. On a remote
database every call is a network round-trip.

``request_cached`` memoises those reads *only inside an active page run*
(``begin_request()`` is called at the top of app.py) and *only until the next
write*: every INSERT/UPDATE/DELETE on any connection bumps a global write
generation, which invalidates the cache immediately. Outside a page run (tests,
CLI, the background scheduler thread) the decorated functions behave exactly
as before.
"""
from __future__ import annotations

import copy
import functools
import inspect
import threading
from typing import Any, Callable, Dict, Tuple

_local = threading.local()
_generation_lock = threading.Lock()
_write_generation = 0

_READ_PREFIXES = ("SELECT", "PRAGMA", "WITH", "SHOW", "SET", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "EXPLAIN")


def bump_write_generation() -> None:
    """Invalidate all cached reads (call after any data change)."""
    global _write_generation
    with _generation_lock:
        _write_generation += 1


def note_statement(sql: str) -> None:
    """Called for every executed statement; bumps the generation on writes."""
    head = sql.lstrip().upper()
    if not head.startswith(_READ_PREFIXES):
        bump_write_generation()


def begin_request() -> None:
    """Start a fresh cache scope for the current thread (one Streamlit run)."""
    _local.active = True
    _local.cache = {}
    _local.generation = _write_generation


def end_request() -> None:
    _local.active = False
    _local.cache = {}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(_freeze(v) for v in value)
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)


def request_cached(func: Callable) -> Callable:
    """Memoise a pure read helper for the duration of one page run."""

    sig = inspect.signature(func)

    def _key(args, kwargs) -> Tuple:
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            norm = tuple(sorted((k, _freeze(v)) for k, v in bound.arguments.items()))
        except TypeError:
            norm = (_freeze(args), _freeze(kwargs))
        return (func.__module__, func.__qualname__, norm)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not getattr(_local, "active", False):
            return func(*args, **kwargs)
        if getattr(_local, "generation", None) != _write_generation:
            _local.cache = {}
            _local.generation = _write_generation
        key = _key(args, kwargs)
        cache: Dict = _local.cache
        if key in cache:
            return copy.deepcopy(cache[key])
        result = func(*args, **kwargs)
        # A write may have happened while computing; only cache if still current.
        if _local.generation == _write_generation:
            cache[key] = copy.deepcopy(result)
        return result

    wrapper.uncached = func  # type: ignore[attr-defined]
    return wrapper
