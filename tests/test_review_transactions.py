"""Store calls borrow a single connection and commit only after all writes."""

from types import SimpleNamespace

import pytest

from law_agent.review.transactions import case_transaction, store_connection


class Connection:
    def __init__(self):
        self.commits = 0
        self.rollback = False
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_args):
        self.rollback = exc_type is not None

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1


class Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def execute(self, sql, args):
        self.conn.sql.append((sql, args))

    def fetchone(self):
        return {"id": "case"}

    def fetchall(self):
        return []


@pytest.mark.parametrize("fail", [False, True])
def test_borrowed_calls_defer_commit_and_rollback_on_failure(fail):
    conn = Connection()
    store = SimpleNamespace(dsn="test", _connect=lambda: conn)
    def operation():
        with case_transaction(store, store, "case"):
            with store_connection("test") as borrowed:
                assert borrowed.connection is conn
                borrowed.commit()
            assert conn.commits == 0
            with pytest.raises(RuntimeError, match="同一数据库"):
                store_connection("other")
            if fail:
                raise ValueError("write failed")
    if fail:
        with pytest.raises(ValueError):
            operation()
    else:
        operation()
    assert conn.commits == (0 if fail else 1)
    assert conn.rollback is fail
    assert "FOR UPDATE" in conn.sql[0][0]
    standalone = object()
    assert store_connection("test", factory=lambda: standalone) is standalone
