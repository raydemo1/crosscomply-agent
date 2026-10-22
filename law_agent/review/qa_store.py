"""Private Q&A conversations and leased investigation runs."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def now():
    return datetime.now(UTC).isoformat()


def identifier(prefix):
    return f"{prefix}_{uuid4().hex}"


class QAConflict(ValueError):
    pass


class QAStore:
    def create(self, owner_id):
        row = {"id": identifier("qa"), "owner_id": owner_id, "title": "新的合规问答",
               "created_at": now(), "updated_at": now(), "materials": [], "runs": [], "case_links": {}}
        self._insert(row)
        return deepcopy(row)

    def add_material(self, conversation_id, material):
        def change(row):
            row["materials"].append(material)
            return material
        return self._edit(conversation_id, change)

    def enqueue(self, conversation_id, *, question, material_ids, request_id):
        def change(row):
            for run in row["runs"]:
                if run["request_id"] == request_id:
                    if run["question"] != question or run["material_ids"] != material_ids:
                        raise QAConflict("重复请求的内容不一致")
                    return run
            if any(r["status"] in {"queued", "running"} for r in row["runs"]):
                raise QAConflict("请等待当前回答完成")
            known = {m["id"] for m in row["materials"]}
            if not set(material_ids) <= known:
                raise QAConflict("材料不属于当前会话")
            run = {"id": identifier("qarun"), "request_id": request_id, "question": question,
                   "material_ids": material_ids, "status": "queued", "attempt": 0,
                   "state": {}, "answer": "", "result": None, "error": None,
                   "progress": "等待开始调查", "lease_expires_at": None, "created_at": now()}
            row["runs"].append(run)
            if len(row["runs"]) == 1:
                row["title"] = question[:60]
            return run
        return self._edit(conversation_id, change)

    def retry(self, conversation_id, run_id):
        def change(row):
            run = next((r for r in row["runs"] if r["id"] == run_id), None)
            if run is None:
                raise KeyError(run_id)
            if run["status"] != "failed" or any(r["status"] in {"queued", "running"} for r in row["runs"]):
                raise QAConflict("当前回答不能重试")
            if row["runs"][-1]["id"] != run_id:
                raise QAConflict("已有后续对话，请在会话末尾重新提问")
            run.update(status="queued", answer="", result=None, error=None, state={}, progress="等待重新调查")
            return run
        return self._edit(conversation_id, change)

    def claim(self):
        for conversation_id in self._candidates():
            def change(row):
                for run in row["runs"]:
                    expired = run["status"] == "running" and run["lease_expires_at"] < now()
                    if expired:
                        run.update(status="failed", error="执行中断，请重试", progress="执行中断")
                        run["result"] = {"citations": [], "material_citations": [], "conditions": [],
                                         **(run["result"] or {}), "generation_status": "failed"}
                    if run["status"] == "queued":
                        run.update(status="running", attempt=run["attempt"] + 1,
                                   lease_expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                                   progress="正在分析问题")
                        return {"conversation": deepcopy(row), "run": run}
                return None
            claimed = self._edit(conversation_id, change)
            if claimed:
                return claimed
        return None

    def checkpoint(self, conversation_id, run_id, attempt, **changes):
        def change(row):
            run = next(r for r in row["runs"] if r["id"] == run_id)
            if run["status"] != "running" or run["attempt"] != attempt:
                raise QAConflict("本次执行已失效")
            run.update(changes)
            if run["status"] == "failed":
                run["result"] = {"citations": [], "material_citations": [], "conditions": [],
                                 **(run["result"] or {}), "generation_status": "failed"}
            run["lease_expires_at"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            return run
        return self._edit(conversation_id, change)


class InMemoryQAStore(QAStore):
    def __init__(self):
        self.rows = {}
        self._lock = RLock()

    def _insert(self, row):
        with self._lock:
            self.rows[row["id"]] = deepcopy(row)

    def get(self, conversation_id):
        with self._lock:
            return deepcopy(self.rows.get(conversation_id))

    def list(self, owner_id):
        with self._lock:
            return sorted([deepcopy(r) for r in self.rows.values() if r["owner_id"] == owner_id],
                          key=lambda r: r["updated_at"], reverse=True)

    def _candidates(self):
        with self._lock:
            return [r["id"] for r in self.rows.values() if any(
                run["status"] == "queued" or (run["status"] == "running" and run["lease_expires_at"] < now())
                for run in r["runs"])]

    def _edit(self, conversation_id, change):
        with self._lock:
            row = deepcopy(self.rows[conversation_id])
            result = change(row)
            row["updated_at"] = now()
            self.rows[conversation_id] = row
            return deepcopy(result)


class PostgresQAStore(QAStore):
    def __init__(self, dsn):
        self.dsn = dsn

    def _connect(self):
        from law_agent.review.transactions import store_connection
        return store_connection(self.dsn, lambda: psycopg.connect(self.dsn, row_factory=dict_row))

    def _insert(self, row):
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("INSERT INTO qa_conversations (id, owner_id, data) VALUES (%s, %s, %s)",
                           (row["id"], row["owner_id"], Jsonb(row)))

    def get(self, conversation_id):
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT data FROM qa_conversations WHERE id = %s", (conversation_id,))
            row = cursor.fetchone()
            return row["data"] if row else None

    def list(self, owner_id):
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT data FROM qa_conversations WHERE owner_id = %s ORDER BY updated_at DESC", (owner_id,))
            return [r["data"] for r in cursor.fetchall()]

    def _candidates(self):
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("""SELECT id FROM qa_conversations WHERE EXISTS (
                SELECT 1 FROM jsonb_array_elements(data->'runs') r
                WHERE r->>'status' = 'queued' OR (r->>'status' = 'running'
                AND (r->>'lease_expires_at')::timestamptz < now())) ORDER BY updated_at LIMIT 32""")
            return [r["id"] for r in cursor.fetchall()]

    def _edit(self, conversation_id, change):
        from law_agent.review.transactions import _active
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT data FROM qa_conversations WHERE id = %s FOR UPDATE", (conversation_id,))
            record = cursor.fetchone()
            if record is None:
                raise KeyError(conversation_id)
            row = record["data"]
            token = _active.set((self.dsn, conn))
            try:
                result = change(row)
                row["updated_at"] = now()
                cursor.execute("UPDATE qa_conversations SET data = %s, updated_at = now() WHERE id = %s",
                               (Jsonb(row), conversation_id))
            finally:
                _active.reset(token)
            return deepcopy(result)
