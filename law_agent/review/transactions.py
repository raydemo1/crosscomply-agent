"""One transaction for frozen inputs, task replacement and case updates."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

import psycopg
from psycopg.rows import dict_row

_active: ContextVar[tuple[str, Any] | None] = ContextVar("review_transaction", default=None)


class _BorrowedConnection:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self.connection.cursor()

    def commit(self):
        return None


def store_connection(dsn, factory=None):
    active = _active.get()
    if active is not None:
        if active[0] != dsn:
            raise RuntimeError("案件与任务必须使用同一数据库事务")
        return _BorrowedConnection(active[1])
    return factory() if factory else psycopg.connect(dsn, row_factory=dict_row)


@contextmanager
def case_transaction(cases, enterprise, case_id):
    if hasattr(enterprise, "dsn"):
        if cases.dsn != enterprise.dsn:
            raise RuntimeError("案件与任务数据库不一致")
        with enterprise._connect() as connection:
            token = _active.set((enterprise.dsn, connection))
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id FROM review_cases WHERE id = %s FOR UPDATE", (case_id,))
                    if cursor.fetchone() is None:
                        raise KeyError(case_id)
                    cursor.execute(
                        "SELECT id FROM review_tasks WHERE case_id = %s FOR UPDATE", (case_id,),
                    )
                    cursor.fetchall()
                yield
                connection.commit()
            finally:
                _active.reset(token)
    else:
        with enterprise._lock:
            case_backup = deepcopy((cases.cases, cases.events))
            task_backup = deepcopy((enterprise.tasks, enterprise.intake_snapshots, enterprise.task_by_key))
            try:
                yield
            except BaseException:
                cases.cases, cases.events = case_backup
                enterprise.tasks, enterprise.intake_snapshots, enterprise.task_by_key = task_backup
                raise
