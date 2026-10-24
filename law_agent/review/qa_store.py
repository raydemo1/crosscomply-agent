"""Private Q&A conversations and leased investigation runs.

Conversation metadata/materials stay in qa_conversations.data (JSONB); runs
live in their own qa_runs table (migration 0019) so worker claim polling is an
indexed status lookup instead of a full-table jsonb_array_elements scan.
"""

from __future__ import annotations

import json

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

LEASE_SECONDS = 5 * 60


def now():
    return datetime.now(UTC).isoformat()


def identifier(prefix):
    return f"{prefix}_{uuid4().hex}"


class QAConflict(ValueError):
    pass


def _failed_result(run: dict) -> dict:
    return {"citations": [], "material_citations": [], "conditions": [],
            **(run.get("result") or {}), "generation_status": "failed"}


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

    def checkpoint(self, conversation_id, run_id, attempt, **changes):
        def change(row):
            run = next(r for r in row["runs"] if r["id"] == run_id)
            if run["status"] != "running" or run["attempt"] != attempt:
                raise QAConflict("本次执行已失效")
            run.update(changes)
            if run["status"] == "failed":
                run["result"] = _failed_result(run)
            run["lease_expires_at"] = (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat()
            return run
        return self._edit(conversation_id, change)


class InMemoryQAStore(QAStore):
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._lock = RLock()

    def _insert(self, row):
        with self._lock:
            self.rows[row["id"]] = row

    def get(self, conversation_id):
        with self._lock:
            row = self.rows.get(conversation_id)
            return deepcopy(row) if row else None

    def list(self, owner_id):
        with self._lock:
            rows = [row for row in self.rows.values() if row["owner_id"] == owner_id]
            rows.sort(key=lambda row: row["updated_at"], reverse=True)
            return deepcopy(rows)

    def _edit(self, conversation_id, change):
        with self._lock:
            row = deepcopy(self.rows[conversation_id])
            before = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            result = change(row)
            if json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) != before:
                row["updated_at"] = now()
                self.rows[conversation_id] = row
            return deepcopy(result)

    def claim(self):
        with self._lock:
            for row in self.rows.values():
                for run in row["runs"]:
                    expired = run["status"] == "running" and run["lease_expires_at"] < now()
                    if expired:
                        run.update(status="failed", error="执行中断，请重试", progress="执行中断")
                        run["result"] = _failed_result(run)
                    if run["status"] == "queued":
                        run.update(status="running", attempt=run["attempt"] + 1,
                                   lease_expires_at=(datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                                   progress="正在分析问题")
                        return {"conversation": deepcopy(row), "run": deepcopy(run)}
        return None


class PostgresQAStore(QAStore):
    def __init__(self, dsn):
        self.dsn = dsn

    def _connect(self):
        from law_agent.review.transactions import store_connection

        # 不传 factory：无共享事务时走 db_pool 连接池（与 case/enterprise 一致）
        return store_connection(self.dsn)

    def _insert(self, row):
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("INSERT INTO qa_conversations (id, owner_id, data) VALUES (%s, %s, %s)",
                           (row["id"], row["owner_id"], Jsonb({k: v for k, v in row.items() if k != "runs"})))

    def get(self, conversation_id):
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT data FROM qa_conversations WHERE id = %s", (conversation_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            conversation = row["data"]
            conversation["runs"] = self._load_runs(cursor, conversation_id)
            return conversation

    def list(self, owner_id):
        with self._connect() as conn, conn.cursor() as cursor:
            # 列表投影：不展开 runs/materials 大字段，标题与时间足够侧栏展示
            cursor.execute(
                "SELECT data - 'runs' - 'materials' AS head FROM qa_conversations "
                "WHERE owner_id = %s ORDER BY updated_at DESC LIMIT 100",
                (owner_id,),
            )
            heads = [r["head"] for r in cursor.fetchall()]
            for head in heads:
                head.setdefault("materials", [])
                head.setdefault("runs", [])
            return heads

    @staticmethod
    def _load_runs(cursor, conversation_id):
        cursor.execute(
            "SELECT * FROM qa_runs WHERE conversation_id = %s ORDER BY created_at",
            (conversation_id,),
        )
        return [
            {
                "id": r["id"],
                "request_id": r["request_id"],
                "question": r["question"],
                "material_ids": r["material_ids"],
                "status": r["status"],
                "attempt": r["attempt"],
                "state": r["state"] or {},
                "answer": r["answer"],
                "result": r["result"],
                "error": r["error"],
                "progress": r["progress"],
                "lease_expires_at": r["lease_expires_at"].isoformat() if r["lease_expires_at"] else None,
                "created_at": r["created_at"].isoformat(),
            }
            for r in cursor.fetchall()
        ]

    def _edit(self, conversation_id, change):
        """会话级编辑（材料/标题/run 状态变更）：runs 已拆表，编辑后单独同步。"""

        from law_agent.review.transactions import _active

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT data FROM qa_conversations WHERE id = %s FOR UPDATE", (conversation_id,))
            record = cursor.fetchone()
            if record is None:
                raise KeyError(conversation_id)
            row = record["data"]
            row["runs"] = self._load_runs(cursor, conversation_id)
            token = _active.set((self.dsn, conn))
            try:
                result = change(row)
                self._sync_runs(cursor, conversation_id, row["runs"])
                stored = {k: v for k, v in row.items() if k != "runs"}
                stored["updated_at"] = now()
                cursor.execute(
                    "UPDATE qa_conversations SET data = %s, updated_at = now() WHERE id = %s",
                    (Jsonb(stored), conversation_id),
                )
            finally:
                _active.reset(token)
            return deepcopy(result)

    @staticmethod
    def _sync_runs(cursor, conversation_id, runs):
        for run in runs:
            cursor.execute(
                """
                INSERT INTO qa_runs (
                    id, conversation_id, request_id, question, material_ids, status,
                    attempt, state, answer, result, error, progress, lease_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    attempt = EXCLUDED.attempt,
                    state = EXCLUDED.state,
                    answer = EXCLUDED.answer,
                    result = EXCLUDED.result,
                    error = EXCLUDED.error,
                    progress = EXCLUDED.progress,
                    lease_expires_at = EXCLUDED.lease_expires_at
                """,
                (
                    run["id"], conversation_id, run["request_id"], run["question"],
                    Jsonb(run["material_ids"]), run["status"], run["attempt"],
                    Jsonb(run["state"]), run["answer"],
                    Jsonb(run["result"]) if run["result"] is not None else None,
                    run["error"], run["progress"],
                    run.get("lease_expires_at"),
                ),
            )

    def claim(self):
        """从 qa_runs 部分索引领取：先标记过期租约为失败，再领取最早排队任务。"""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE qa_runs SET status = 'failed', error = '执行中断，请重试',
                    progress = '执行中断',
                    result = jsonb_build_object(
                        'citations', '[]'::jsonb, 'material_citations', '[]'::jsonb,
                        'conditions', '[]'::jsonb, 'generation_status', 'failed')
                WHERE status = 'running' AND lease_expires_at < now()
                """
            )
            cursor.execute(
                """
                UPDATE qa_runs SET status = 'running', attempt = attempt + 1,
                    progress = '正在分析问题',
                    lease_expires_at = now() + (%s * interval '1 second')
                WHERE id = (
                    SELECT id FROM qa_runs
                    WHERE status = 'queued'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING conversation_id
                """,
                (LEASE_SECONDS,),
            )
            claimed = cursor.fetchone()
            if claimed is None:
                conn.rollback()
                return None
            conversation_id = claimed["conversation_id"]
            cursor.execute("SELECT data FROM qa_conversations WHERE id = %s", (conversation_id,))
            conversation = cursor.fetchone()["data"]
            conversation["runs"] = self._load_runs(cursor, conversation_id)
            run = next(r for r in conversation["runs"] if r["status"] == "running")
            conn.commit()
            return {"conversation": conversation, "run": run}
