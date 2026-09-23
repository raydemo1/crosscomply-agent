"""Grounded text revisions derived from a completed review issue."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import Field

from law_agent.config import require_llm_config
from law_agent.data.schemas import StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode


class RevisionError(ValueError):
    pass


class RevisionConflict(RevisionError):
    pass


class RevisionDraft(StrictModel):
    proposed_text: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    open_points: list[str] = Field(default_factory=list)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def locate_target(base_text: str, quote: str) -> tuple[int, int]:
    if not quote or base_text.count(quote) != 1:
        raise RevisionConflict("当前工作稿已改变目标原文，请重新审查该问题")
    start = base_text.index(quote)
    return start, start + len(quote)


def locate_frozen_target(base_text: str, quote: str, original_start: int, base_version: int) -> tuple[int, int]:
    if base_version == 0 and base_text[original_start:original_start + len(quote)] == quote:
        return original_start, original_start + len(quote)
    return locate_target(base_text, quote)


def generate_revision_draft(
    *, issue: dict[str, Any], target_quote: str, base_text: str,
    target_start: int | None = None,
    citations: list[dict[str, Any]] | None = None,
    prior_feedback: list[str] | None = None,
    client: OpenAICompatibleClient | None = None,
) -> RevisionDraft:
    if target_start is not None and base_text[target_start:target_start + len(target_quote)] == target_quote:
        start, end = target_start, target_start + len(target_quote)
    else:
        start, end = locate_target(base_text, target_quote)
    context = base_text[max(0, start - 2500):min(len(base_text), end + 2500)]
    node = StructuredLLMNode(
        node_name="revision_draft", output_model=RevisionDraft,
        client=client or OpenAICompatibleClient(require_llm_config()),
        structured_output_mode="json_object",
    )
    schema = json.dumps(RevisionDraft.model_json_schema(), ensure_ascii=False)
    draft = node.run([
        ChatMessage(role="system", content=(
            "你是同一个案件审查 Agent 的文书修改阶段。只为给定目标原文起草替换文本，不改其他段落。"
            "依据已核实的问题和法源，保留未知事实为待确认项，不编造日期、金额或承诺。"
            "如果没有提供可核实的法条正文，只根据问题事实提出审慎措辞，不自行补造具体法定义务。"
            f"仅输出符合 JSON Schema 的 JSON：{schema}"
        )),
        ChatMessage(role="user", content=json.dumps({
            "issue": issue, "target_quote": target_quote, "surrounding_text": context,
            "verified_legal_sources": citations or [],
            "prior_rejection_feedback": prior_feedback or [],
        }, ensure_ascii=False)),
    ])
    if draft.proposed_text.strip() == target_quote.strip():
        raise RevisionError("建议文本与原文相同")
    return draft


def _new_proposal(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"revision_{uuid4().hex[:16]}", "status": "pending", "version": 1,
        "accepted_text": None, "result_text": None, "result_sha256": None, "result_version": None,
        "decision_note": None, "decided_by": None, "decided_at": None,
        "created_at": datetime.now(UTC).isoformat(), **data,
    }


class InMemoryRevisionStore:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        latest = self.current_draft(data["source_material_version_id"])
        if (latest["result_version"] if latest else 0) != data["base_version"] or (
            latest is not None and latest["result_sha256"] != data["base_sha256"]
        ):
            raise RevisionConflict("工作稿已有新修改，请重新生成修改提案")
        for existing in self.items.values():
            if existing["status"] == "pending" and all(existing[key] == data[key] for key in (
                "case_id", "source_review_result_id", "issue_id", "source_material_version_id",
                "target_start", "target_end", "base_sha256",
            )):
                raise RevisionConflict("该目标段已有待处理提案")
        item = _new_proposal(data)
        self.items[item["id"]] = item
        return self._view(item)

    @staticmethod
    def _view(item: dict[str, Any]) -> dict[str, Any]:
        value = dict(item)
        value["open_points"] = value.pop("open_points_json")
        value["citation_refs"] = value.pop("citation_refs_json")
        return value

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        item = self.items.get(proposal_id)
        return self._view(item) if item else None

    def list(self, case_id: str) -> list[dict[str, Any]]:
        return [self._view(item) for item in self.items.values() if item["case_id"] == case_id]

    def current_draft(self, material_version_id: str) -> dict[str, Any] | None:
        accepted = [item for item in self.items.values() if item["source_material_version_id"] == material_version_id and item["status"] == "accepted"]
        return self._view(max(accepted, key=lambda item: item["result_version"])) if accepted else None

    def decide(
        self, proposal_id: str, *, decision: Literal["accepted", "rejected"],
        expected_version: int, current_review_result_id: str, current_base_text: str,
        replacement: str | None, note: str | None, actor_id: str,
    ) -> dict[str, Any]:
        item = self.items.get(proposal_id)
        if item is None:
            raise KeyError(proposal_id)
        if item["status"] != "pending" or item["version"] != expected_version:
            raise RevisionConflict("修改提案已处理，请刷新页面")
        if item["source_review_result_id"] != current_review_result_id:
            item["status"] = "superseded"
            item["version"] += 1
            raise RevisionConflict("案件已重新审查，请重新生成修改提案")
        if sha256(current_base_text) != item["base_sha256"]:
            item["status"] = "superseded"
            item["version"] += 1
            raise RevisionConflict("工作稿已有新修改，请重新生成修改提案")
        current_version = self.current_draft(item["source_material_version_id"])
        if (current_version["result_version"] if current_version else 0) != item["base_version"]:
            item["status"] = "superseded"
            item["version"] += 1
            raise RevisionConflict("工作稿版本已变化，请重新生成修改提案")
        if decision == "accepted":
            accepted_text = replacement if replacement is not None else item["proposed_text"]
            if not accepted_text.strip() or accepted_text.strip() == item["target_quote"].strip():
                raise RevisionError("接受的建议文本必须与原文不同且非空")
            start, end = item["target_start"], item["target_end"]
            if current_base_text[start:end] != item["target_quote"]:
                raise RevisionConflict("目标原文已改变，请重新生成修改提案")
            result = current_base_text[:start] + accepted_text + current_base_text[end:]
            item.update(accepted_text=accepted_text, result_text=result, result_sha256=sha256(result), result_version=item["base_version"] + 1)
            for other in self.items.values():
                if other["id"] != proposal_id and other["source_material_version_id"] == item["source_material_version_id"] and other["status"] == "pending":
                    other["status"] = "superseded"
                    other["version"] += 1
        item.update(status=decision, version=item["version"] + 1, decision_note=note,
                    decided_by=actor_id, decided_at=datetime.now(UTC).isoformat())
        return self._view(item)


class PostgresRevisionStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _row(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for key in ("created_at", "decided_at"):
            if value.get(key) is not None:
                value[key] = value[key].isoformat()
        value["open_points"] = value.pop("open_points_json")
        value["citation_refs"] = value.pop("citation_refs_json")
        return value

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        item = _new_proposal(data)
        columns = list(item)
        values = [Jsonb(item[key]) if key in {"open_points_json", "citation_refs_json"} else item[key] for key in columns]
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (data["source_material_version_id"],))
                cur.execute(
                    "SELECT result_sha256, result_version FROM revision_proposals WHERE source_material_version_id = %s AND status = 'accepted' ORDER BY result_version DESC LIMIT 1",
                    (data["source_material_version_id"],),
                )
                latest = cur.fetchone()
                if (latest["result_version"] if latest else 0) != data["base_version"] or (
                    latest is not None and latest["result_sha256"] != data["base_sha256"]
                ):
                    raise RevisionConflict("工作稿已有新修改，请重新生成修改提案")
                cur.execute(
                    f"INSERT INTO revision_proposals ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) RETURNING *",
                    values,
                )
                row = cur.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise RevisionConflict("该目标段已有待处理提案") from exc
        return self._row(row)

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM revision_proposals WHERE id = %s", (proposal_id,))
            row = cur.fetchone()
        return self._row(row) if row else None

    def list(self, case_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM revision_proposals WHERE case_id = %s ORDER BY created_at", (case_id,))
            return [self._row(row) for row in cur.fetchall()]

    def current_draft(self, material_version_id: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM revision_proposals WHERE source_material_version_id = %s AND status = 'accepted' ORDER BY result_version DESC LIMIT 1",
                (material_version_id,),
            )
            row = cur.fetchone()
        return self._row(row) if row else None

    def decide(
        self, proposal_id: str, *, decision: Literal["accepted", "rejected"],
        expected_version: int, current_review_result_id: str, current_base_text: str,
        replacement: str | None, note: str | None, actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT source_material_version_id FROM revision_proposals WHERE id = %s", (proposal_id,))
            target_row = cur.fetchone()
            if target_row is None:
                raise KeyError(proposal_id)
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (target_row["source_material_version_id"],))
            cur.execute("SELECT * FROM revision_proposals WHERE id = %s FOR UPDATE", (proposal_id,))
            row = cur.fetchone()
            item = self._row(row)
            if item["status"] != "pending" or item["version"] != expected_version:
                raise RevisionConflict("修改提案已处理，请刷新页面")
            cur.execute("SELECT response_json FROM review_cases WHERE id = %s FOR UPDATE", (item["case_id"],))
            case_row = cur.fetchone()
            persisted_review_id = (((case_row or {}).get("response_json") or {}).get("review_result") or {}).get("review_result_id")
            if item["source_review_result_id"] != current_review_result_id or item["source_review_result_id"] != persisted_review_id:
                cur.execute("UPDATE revision_proposals SET status = 'superseded', version = version + 1 WHERE id = %s", (proposal_id,))
                conn.commit()
                raise RevisionConflict("案件已重新审查，请重新生成修改提案")
            cur.execute(
                "SELECT result_sha256, result_version FROM revision_proposals WHERE source_material_version_id = %s AND status = 'accepted' ORDER BY result_version DESC LIMIT 1",
                (item["source_material_version_id"],),
            )
            latest = cur.fetchone()
            if (latest and latest["result_sha256"] != item["base_sha256"]) or sha256(current_base_text) != item["base_sha256"] or (latest["result_version"] if latest else 0) != item["base_version"]:
                cur.execute("UPDATE revision_proposals SET status = 'superseded', version = version + 1 WHERE id = %s", (proposal_id,))
                conn.commit()
                raise RevisionConflict("工作稿已有新修改，请重新生成修改提案")
            result_text = result_hash = result_version = accepted_text = None
            if decision == "accepted":
                accepted_text = replacement if replacement is not None else item["proposed_text"]
                if not accepted_text.strip() or accepted_text.strip() == item["target_quote"].strip():
                    raise RevisionError("接受的建议文本必须与原文不同且非空")
                start, end = item["target_start"], item["target_end"]
                if current_base_text[start:end] != item["target_quote"]:
                    raise RevisionConflict("目标原文已改变，请重新生成修改提案")
                result_text = current_base_text[:start] + accepted_text + current_base_text[end:]
                result_hash = sha256(result_text)
                result_version = item["base_version"] + 1
            cur.execute(
                "UPDATE revision_proposals SET status = %s, version = version + 1, accepted_text = %s, result_text = %s, result_sha256 = %s, result_version = %s, decision_note = %s, decided_by = %s, decided_at = now() WHERE id = %s RETURNING *",
                (decision, accepted_text, result_text, result_hash, result_version, note, actor_id, proposal_id),
            )
            updated = cur.fetchone()
            if decision == "accepted":
                cur.execute(
                    "UPDATE revision_proposals SET status = 'superseded', version = version + 1 WHERE source_material_version_id = %s AND id <> %s AND status = 'pending'",
                    (item["source_material_version_id"], proposal_id),
                )
        return self._row(updated)
