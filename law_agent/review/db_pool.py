"""Shared PostgreSQL connection pooling.

Every store class opens a fresh connection per call, which multiplies TCP +
auth handshakes under the 8s polling frontend. This module provides a
process-level pool keyed by DSN; when psycopg_pool is not installed the
fallback keeps the old direct-connect behaviour.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_pools: dict[str, "object"] = {}
_lock = threading.Lock()
try:  # pragma: no cover - availability depends on deployment extras
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover
    ConnectionPool = None  # type: ignore[assignment]


def _get_pool(dsn: str):
    if ConnectionPool is None:
        return None
    with _lock:
        pool = _pools.get(dsn)
        if pool is None:
            from psycopg.rows import dict_row

            pool = ConnectionPool(
                dsn,
                min_size=1,
                max_size=10,
                open=True,
                kwargs={"autocommit": False, "row_factory": dict_row},
            )
            _pools[dsn] = pool
        return pool


@contextmanager
def pooled_connection(dsn: str):
    """Yield a pooled connection when available, else a direct one."""

    pool = _get_pool(dsn)
    if pool is None:
        import psycopg

        with psycopg.connect(dsn) as conn:
            yield conn
        return
    with pool.connection() as conn:
        yield conn
