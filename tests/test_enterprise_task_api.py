"""API contract tests for durable asynchronous review tasks."""

import hashlib
import json
from base64 import b64encode
from pathlib import Path

from Crypto.Cipher import AES
from fastapi.testclient import TestClient

from law_agent.review.api import create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.feishu import ApprovalInstance, FeishuApprovalConfig
from law_agent.review.governance_store import InMemoryGovernanceStore
from law_agent.review.object_store import MaterialObjectStore
from law_agent.review.user_admin import InMemoryUserAdminStore


class FakeMinio:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def put_object(self, bucket, key, stream, size, **_kwargs):
        self.objects[(bucket, key)] = stream.read(size)

    def get_object(self, bucket: str, key: str):
        from io import BytesIO

        return BytesIO(self.objects[(bucket, key)])


def _login(client: TestClient, username: str = "reviewer@crosscomply.local") -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    assert response.status_code == 200


def _create_case(client: TestClient) -> str:
    response = client.post(
        "/api/cases",
        json={
            "question": "采购境外 CRM SaaS 需要履行哪条出境路径？",
            "material_text": "客户联系信息将传输至境外供应商。",
            "intake": {"cross_border_transfer": True},
        },
    )
    assert response.status_code == 200
    return response.json()["case"]["id"]


def _freeze_inputs(store: InMemoryEnterpriseStore, case_id: str, *, needs_info: bool = False):
    version = store.create_material_version(
        case_id=case_id,
        logical_name="vendor_dpa",
        filename="dpa.pdf",
        content_type="application/pdf",
        object_key=f"cases/{case_id}/dpa.pdf",
        sha256="a" * 64,
        byte_size=100,
        uploaded_by="user_test",
        parse_status="ready",
        parsed_text="脱敏 DPA 原文",
    )
    snapshot = store.create_material_snapshot(
        case_id=case_id, version_ids=[version.id], created_by="user_test"
    )
    rule = store.create_rule_snapshot(
        case_id=case_id,
        material_snapshot_id=snapshot.id,
        ruleset_version="national-cross-border-2024.03-v1",
        facts={"is_ciio": False},
        determination={
            "status": "needs_info" if needs_info else "determined",
            "needs_info": [{"key": "important_data"}] if needs_info else [],
            "candidate_paths": [] if needs_info else ["标准合同"],
        },
    )
    return snapshot, rule


def test_run_enqueues_idempotent_task_and_exposes_polling(tmp_path: Path) -> None:
    case_store = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    app = create_app(chunks_path=chunks, case_store=case_store, enterprise_store=enterprise)

    with TestClient(app) as client:
        _login(client)
        case_id = _create_case(client)
        _freeze_inputs(enterprise, case_id)
        case_store.update_case(case_id, status="pending_review", facts_confirmed=True)

        first = client.post(f"/api/cases/{case_id}/run")
        second = client.post(f"/api/cases/{case_id}/run")

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["task_id"] == first.json()["task_id"]
        task = client.get(f"/api/tasks/{first.json()['task_id']}")
        assert task.status_code == 200
        assert task.json()["status"] == "queued"
        assert task.json()["material_snapshot_id"]
        assert task.json()["rule_snapshot_id"]


def test_case_cannot_be_submitted_without_frozen_rule_inputs(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    app = create_app(
        chunks_path=chunks,
        case_store=InMemoryCaseStore(seed_password="pw"),
        enterprise_store=InMemoryEnterpriseStore(),
    )

    with TestClient(app) as client:
        _login(client)
        case_id = _create_case(client)
        response = client.post(f"/api/cases/{case_id}/status", json={"status": "pending_review"})

        assert response.status_code == 409
        assert "冻结材料" in response.json()["detail"]


def test_missing_critical_facts_cannot_enqueue_review(tmp_path: Path) -> None:
    case_store = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    app = create_app(chunks_path=chunks, case_store=case_store, enterprise_store=enterprise)

    with TestClient(app) as client:
        _login(client)
        case_id = _create_case(client)
        _freeze_inputs(enterprise, case_id, needs_info=True)
        case_store.update_case(case_id, status="pending_review", facts_confirmed=True)

        response = client.post(f"/api/cases/{case_id}/run")

        assert response.status_code == 409
        assert "关键事实缺失" in response.json()["detail"]
        assert not enterprise.tasks


def test_upload_freeze_and_download_preserve_original_hash(tmp_path: Path) -> None:
    case_store = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    object_store = MaterialObjectStore(client=FakeMinio(), bucket="materials")
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    app = create_app(
        chunks_path=chunks,
        case_store=case_store,
        enterprise_store=enterprise,
        material_object_store=object_store,
    )

    with TestClient(app) as client:
        _login(client)
        case_id = _create_case(client)
        upload = client.post(
            f"/api/cases/{case_id}/materials",
            data={"logical_name": "vendor_dpa"},
            files={"file": ("dpa.txt", "脱敏 DPA 原文", "text/plain")},
        )
        assert upload.status_code == 200, upload.text
        version = upload.json()
        assert version["version_number"] == 1
        assert version["parse_status"] == "ready"
        assert len(version["sha256"]) == 64

        frozen = client.post(
            f"/api/cases/{case_id}/material-snapshots",
            json={
                "version_ids": [version["id"]],
                "facts": {
                    "cross_border_transfer": True,
                    "is_ciio": False,
                    "important_data": False,
                    "contains_personal_information": True,
                    "contains_sensitive_personal_information": True,
                    "cumulative_personal_information_subjects": 100,
                    "cumulative_sensitive_personal_information_subjects": 10,
                    "exemption_facts_confirmed": False,
                },
            },
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["material_snapshot"]["fingerprint"]
        assert frozen.json()["rule_decision"]["ruleset_version"]

        downloaded = client.get(f"/api/materials/{version['id']}/download")
        assert downloaded.status_code == 200
        assert downloaded.content == "脱敏 DPA 原文".encode()

        object_store._client.objects[("materials", version["object_key"])] = b"tampered"
        rejected = client.get(f"/api/materials/{version['id']}/download")
        assert rejected.status_code == 409
        assert "完整性校验失败" in rejected.json()["detail"]


def test_failed_task_retry_cannot_reopen_terminal_case(tmp_path: Path) -> None:
    case_store = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    app = create_app(chunks_path=chunks, case_store=case_store, enterprise_store=enterprise)

    with TestClient(app) as client:
        _login(client)
        case_id = _create_case(client)
        snapshot, rule = _freeze_inputs(enterprise, case_id)
        task = enterprise.enqueue_review_task(
            case_id=case_id,
            material_snapshot_id=snapshot.id,
            rule_snapshot_id=rule.id,
            model_id="approved-model",
            data_boundary_summary={},
        )
        enterprise.claim_next_task(worker_id="worker-1")
        enterprise.fail_task(
            task.id,
            failed_node="retrieval",
            error_category="dependency_failure",
            error_message="Elasticsearch unavailable",
        )
        case_store.update_case(case_id, status="approved")

        response = client.post(f"/api/tasks/{task.id}/retry")

        assert response.status_code == 409
        assert enterprise.get_task(task.id).status == "failed"
        assert case_store.get_case(case_id)["status"] == "approved"


def test_admin_can_manage_users_without_preset_user_dependency(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    managed_users = InMemoryUserAdminStore()
    app = create_app(
        chunks_path=chunks,
        case_store=InMemoryCaseStore(seed_password="pw"),
        enterprise_store=InMemoryEnterpriseStore(),
        user_admin_store=managed_users,
    )

    with TestClient(app) as client:
        _login(client, "admin@crosscomply.local")
        created = client.post(
            "/api/admin/users",
            json={
                "username": "buyer@example.com",
                "display_name": "采购申请人",
                "password": "strong-password-2026",
                "role": "requester",
            },
        )
        assert created.status_code == 200, created.text
        user_id = created.json()["id"]
        disabled = client.patch(f"/api/admin/users/{user_id}/state", json={"active": False})
        assert disabled.status_code == 200
        assert disabled.json()["active"] is False
        role = client.patch(f"/api/admin/users/{user_id}/role", json={"role": "reviewer"})
        assert role.status_code == 200
        assert role.json()["role"] == "reviewer"
        listing = client.get("/api/admin/users")
        assert listing.json()["total"] == 1


def test_feishu_authoritative_writeback_and_hashed_report(tmp_path: Path) -> None:
    class FakeFeishuClient:
        def __init__(self) -> None:
            self.created: dict = {}

        def create_instance(self, **kwargs):
            self.created = kwargs
            return ApprovalInstance(instance_id="instance-hero", request_id="request-hero")

    case_store = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    governance = InMemoryGovernanceStore()
    object_store = MaterialObjectStore(client=FakeMinio(), bucket="materials")
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    config = FeishuApprovalConfig(
        app_id="app-id",
        app_secret="secret",
        approval_code="approval-code",
        verification_token="verify-token",
        encrypt_key="encrypt-key",
        initiator_open_id="ou_initiator",
        public_base_url="https://crosscomply.example.com",
    )
    fake_feishu = FakeFeishuClient()
    app = create_app(
        chunks_path=chunks,
        case_store=case_store,
        enterprise_store=enterprise,
        material_object_store=object_store,
        governance_store=governance,
        feishu_client=fake_feishu,  # type: ignore[arg-type]
        feishu_config=config,
    )

    with TestClient(app) as client:
        challenge_payload = json.dumps(
            {
                "type": "url_verification",
                "token": "verify-token",
                "challenge": "challenge-value",
            },
            separators=(",", ":"),
        ).encode()
        padding = AES.block_size - len(challenge_payload) % AES.block_size
        iv = bytes(range(AES.block_size))
        cipher = AES.new(hashlib.sha256(b"encrypt-key").digest(), AES.MODE_CBC, iv)
        encrypted_challenge = b64encode(
            iv + cipher.encrypt(challenge_payload + bytes([padding]) * padding)
        ).decode()
        challenge = client.post(
            "/api/integrations/feishu/approval-events",
            json={"encrypt": encrypted_challenge},
        )
        assert challenge.status_code == 200, challenge.text
        assert challenge.json() == {"challenge": "challenge-value"}

        _login(client)
        case_id = _create_case(client)
        snapshot, rule = _freeze_inputs(enterprise, case_id)
        task = enterprise.enqueue_review_task(
            case_id=case_id,
            material_snapshot_id=snapshot.id,
            rule_snapshot_id=rule.id,
            model_id="approved-model",
            data_boundary_summary={"deployment": "intranet"},
        )
        enterprise.claim_next_task(worker_id="worker-1")
        enterprise.complete_task(
            task.id,
            result={
                "review_result": {
                    "missing_information": [],
                    "risk_level": "medium",
                    "conclusion": "建议采用标准合同路径，并在上线前完成备案。",
                    "recommended_actions": ["完成个人信息保护影响评估"],
                }
            },
            final_node="completed",
        )
        case_store.update_case(case_id, status="pending_feishu_approval")
        reviewer_id = client.get("/api/auth/me").json()["user"]["id"]
        plan = client.post(
            f"/api/cases/{case_id}/remediation-plan",
            json={
                "tasks": [{
                    "title": "上线前完成跨境合同归档",
                    "description": "审批时提交的整改动作说明",
                    "assignee_id": reviewer_id,
                    "due_date": "2026-08-30",
                }],
            },
        )
        assert plan.status_code == 200, plan.text
        remediation = plan.json()["tasks"][0]
        uploaded = client.post(
            f"/api/remediation-tasks/{remediation['id']}/evidence",
            files={"file": ("contract.txt", b"signed contract", "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["kind"] == "file"
        assert uploaded.json()["object_key"]

        created = client.post(f"/api/cases/{case_id}/feishu-approval")
        assert created.status_code == 200, created.text
        assert created.json()["instance_id"] == "instance-hero"
        form = {item["id"]: item["value"] for item in fake_feishu.created["form"]}
        assert form["decision_summary"].startswith("风险：中｜候选路径：标准合同｜AI审查：")
        assert form["key_actions"] == "完成个人信息保护影响评估；上线前完成跨境合同归档"
        assert form["case_url"] == f"https://crosscomply.example.com/?case={case_id}"
        assert form["task_id"] == task.id
        updated_remediation = client.patch(
            f"/api/remediation-tasks/{remediation['id']}",
            json={"description": "审批后的整改跟进说明"},
        )
        assert updated_remediation.status_code == 200, updated_remediation.text
        assert updated_remediation.json()["description"] == "审批后的整改跟进说明"
        assert created.json()["payload"]["remediation_plan_snapshot"]["tasks"][0]["description"] == "审批时提交的整改动作说明"
        assert any(
            item["event_type"] == "remediation_task_updated"
            and item["payload"]["task_id"] == remediation["id"]
            for item in client.get(f"/api/cases/{case_id}").json()["events"]
        )

        event_body = json.dumps(
            {
                "token": "verify-token",
                "header": {
                    "event_id": "event-hero-approved",
                    "event_type": "approval_instance",
                },
                "event": {
                    "instance_code": "instance-hero",
                    "status": "APPROVED",
                    "approver_id": "legal-reviewer",
                    "approval_time": "2026-08-18T12:00:00+08:00",
                },
            },
            separators=(",", ":"),
        ).encode()
        signature = hashlib.sha256(b"123nonceencrypt-key" + event_body).hexdigest()
        callback = client.post(
            "/api/integrations/feishu/approval-events",
            content=event_body,
            headers={
                "content-type": "application/json",
                "x-lark-request-timestamp": "123",
                "x-lark-request-nonce": "nonce",
                "x-lark-signature": signature,
            },
        )
        assert callback.status_code == 200, callback.text
        assert callback.json()["case_status"] == "approved"
        assert callback.json()["report_status"] == "available_on_download"
        before_download = client.get(f"/api/cases/{case_id}")
        assert before_download.status_code == 200, before_download.text
        assert before_download.json()["report"] is None

        later_version = enterprise.create_material_version(
            case_id=case_id,
            logical_name="vendor_dpa",
            filename="unapproved-v2.pdf",
            content_type="application/pdf",
            object_key=f"cases/{case_id}/unapproved-v2.pdf",
            sha256="b" * 64,
            byte_size=10,
            uploaded_by="user_test",
            parse_status="ready",
            parsed_text="unapproved",
        )
        later_snapshot = enterprise.create_material_snapshot(
            case_id=case_id,
            version_ids=[later_version.id],
            created_by="user_test",
        )
        enterprise.create_rule_snapshot(
            case_id=case_id,
            material_snapshot_id=later_snapshot.id,
            ruleset_version="unapproved-rule-version",
            facts={},
            determination={},
        )
        downloaded = client.get(f"/api/cases/{case_id}/reports/download")
        assert downloaded.status_code == 200
        assert downloaded.headers["content-disposition"].endswith(f'"{case_store.get_case(case_id)["case_number"]}.pdf"')
        detail = client.get(f"/api/cases/{case_id}")
        report = detail.json()["report"]
        assert report is not None
        assert report["metadata"]["material_snapshot_id"] == snapshot.id
        assert report["metadata"]["rule_version"] == rule.ruleset_version
        assert hashlib.sha256(downloaded.content).hexdigest() == report["sha256"]
        repeated_download = client.get(f"/api/reports/{report['id']}/download")
        assert repeated_download.status_code == 200
        assert repeated_download.content == downloaded.content
        explicit = client.post(f"/api/cases/{case_id}/reports")
        assert explicit.status_code == 200, explicit.text
        assert explicit.json()["id"] == report["id"]


def test_feishu_network_failure_is_persisted_and_can_be_retried(tmp_path: Path) -> None:
    class FlakyFeishuClient:
        attempts = 0

        def create_instance(self, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("network unavailable")
            return ApprovalInstance(instance_id="instance-retried")

    case_store = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    governance = InMemoryGovernanceStore()
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    app = create_app(
        chunks_path=chunks,
        case_store=case_store,
        enterprise_store=enterprise,
        governance_store=governance,
        feishu_client=FlakyFeishuClient(),  # type: ignore[arg-type]
        feishu_config=FeishuApprovalConfig(
            app_id="app-id",
            app_secret="secret",
            approval_code="approval-code",
            verification_token="verify-token",
            encrypt_key="encrypt-key",
            initiator_open_id="ou_initiator",
            public_base_url="https://crosscomply.example.com",
        ),
    )

    with TestClient(app) as client:
        _login(client)
        case_id = _create_case(client)
        snapshot, rule = _freeze_inputs(enterprise, case_id)
        task = enterprise.enqueue_review_task(
            case_id=case_id,
            material_snapshot_id=snapshot.id,
            rule_snapshot_id=rule.id,
            model_id="approved-model",
            data_boundary_summary={},
        )
        enterprise.claim_next_task(worker_id="worker-1")
        enterprise.complete_task(task.id, result={}, final_node="completed")
        case_store.update_case(case_id, status="pending_feishu_approval")

        failed = client.post(f"/api/cases/{case_id}/feishu-approval")
        assert failed.status_code == 503
        delivery_id = failed.json()["detail"]["delivery_id"]
        delivery = governance.get_approval_delivery(delivery_id)
        assert delivery is not None
        assert delivery.status == "failed"
        assert delivery.attempt_count == 1

        retried = client.post(f"/api/approval-deliveries/{delivery_id}/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json()["instance_id"] == "instance-retried"
        completed = governance.get_approval_delivery(delivery_id)
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.attempt_count == 2
