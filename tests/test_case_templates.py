"""Behavior tests for reusable new-case templates."""

from pathlib import Path

from fastapi.testclient import TestClient

from law_agent.review.api import create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.enterprise_store import InMemoryEnterpriseStore


def _app(tmp_path: Path):
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    return create_app(
        chunks_path=chunks,
        case_store=InMemoryCaseStore(seed_password="pw"),
        enterprise_store=InMemoryEnterpriseStore(),
    )


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    assert response.status_code == 200, response.text


def _payload(name: str = "出境审查") -> dict:
    return {
        "name": name,
        "description": "个人信息向境外供应商提供的场景",
        "question": "这个业务是否需要数据出境安全评估？",
        "intake": {"business_activity": "推荐系统", "data_types": ["手机号"]},
        "review_mode": "llm",
        "rerank_mode": "off",
    }


def test_template_crud_and_permissions(tmp_path: Path) -> None:
    app = _app(tmp_path)
    requester = TestClient(app)
    reviewer = TestClient(app)
    _login(requester, "requester@crosscomply.local")

    created = requester.post("/api/case-templates", json=_payload())
    assert created.status_code == 200, created.text
    template_id = created.json()["id"]
    assert "material" not in created.json()
    assert requester.get("/api/case-templates").json()["total"] == 1

    _login(reviewer, "reviewer@crosscomply.local")
    denied = reviewer.patch(f"/api/case-templates/{template_id}", json={"name": "不应修改"})
    assert denied.status_code == 403

    updated = requester.patch(
        f"/api/case-templates/{template_id}",
        json={"name": "出境审查新版", "intake": {"business_activity": "营销分析"}},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "出境审查新版"
    assert updated.json()["intake"]["business_activity"] == "营销分析"
    archived = requester.post(f"/api/case-templates/{template_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert requester.get("/api/case-templates").json()["total"] == 0


def test_template_search_is_case_insensitive(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        _login(client, "requester@crosscomply.local")
        assert client.post("/api/case-templates", json=_payload("供应商出境")).status_code == 200
        result = client.get("/api/case-templates?query=供应商")
        assert result.status_code == 200
        assert result.json()["total"] == 1
