from __future__ import annotations

import hashlib

from pypdf import PdfReader

from law_agent.review.reports import (
    AIReviewSummary,
    DecisionReportData,
    LegalSource,
    RemediationDetail,
    generate_decision_report,
    verify_report_hash,
    write_decision_report,
)


def report_data() -> DecisionReportData:
    return DecisionReportData(
        case_number="CASE-2026-001",
        decision="附条件通过",
        material_hashes=("a" * 64, "b" * 64),
        rule_version="national-path-2026.08",
        legal_sources=(
            LegalSource("个人信息保护法", "第三十八条"),
            LegalSource("Data Export Rules", "Article 5"),
        ),
        remediation_items=("签署标准合同", "留存个人信息保护影响评估"),
        approver="审核人张三",
        approved_at="2026-08-18T12:00:00+08:00",
    )


def test_report_is_deterministic_and_hash_is_verifiable() -> None:
    first = generate_decision_report(report_data())
    second = generate_decision_report(report_data())

    assert first.pdf_bytes == second.pdf_bytes
    assert first.sha256 == hashlib.sha256(first.pdf_bytes).hexdigest()
    assert verify_report_hash(first.pdf_bytes, first.sha256)
    assert not verify_report_hash(first.pdf_bytes + b"changed", first.sha256)


def test_report_contains_required_audit_fields(tmp_path) -> None:
    destination = tmp_path / "decision.pdf"
    artifact = write_decision_report(report_data(), destination)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(destination).pages)

    assert destination.read_bytes() == artifact.pdf_bytes
    assert len(PdfReader(destination).pages) == 2
    assert "CASE-2026-001" in text
    assert "a" * 64 not in text
    assert "sha256" not in text.lower()
    assert "national-path-2026.08" in text
    assert "个人信息保护法" in text
    assert "第三十八条" in text
    assert "签署标准合同" in text
    assert "审核人张三" in text
    assert "2026-08-18 12:00:00+08:00" in text


def test_report_surfaces_case_specific_ai_review_and_action_details(tmp_path) -> None:
    data = DecisionReportData(
        case_number="CASE-2026-002",
        case_title="NimbusCRM 境外 SaaS 上线前审查",
        decision="附条件通过",
        material_hashes=("c" * 64,),
        rule_version="national-path-2026.08",
        selected_path="个人信息出境标准合同路径",
        rule_findings=("当前材料未识别重要数据，适用标准合同路径",),
        manual_confirmation_items=("法务确认标准合同与影响评估均已完成",),
        legal_sources=(LegalSource("个人信息保护法", "第三十八条"),),
        remediation_items=("完成影响评估",),
        remediation_details=(
            RemediationDetail(
                title="完成并签署个人信息保护影响评估",
                description="上线前完成评估并留存签署版",
                owner_role="个人信息保护负责人",
                priority="高优先级",
                required_before="上线前",
                status="待完成",
                evidence_expected="签署版影响评估与版本号",
            ),
        ),
        ai_review=AIReviewSummary(
            risk_level="medium",
            conclusion="建议附条件通过，先完成影响评估和标准合同闭环。",
            missing_information=("尚未提供分处理者清单",),
            recommended_actions=("补齐分处理者名称、处理地点和变更通知期",),
            risk_boundaries=("接收方或数据类型变化时需要重新审查",),
            business_activity="企业客户支持与工单协作",
            overseas_recipient="NimbusCRM Inc.",
            processing_purpose="提供客户关系管理和售后支持",
            data_types=("姓名", "工作邮箱", "工单内容"),
        ),
        approver="审核人李四",
        approved_at="2026-08-18T13:00:00+08:00",
    )
    destination = tmp_path / "rich-decision.pdf"
    artifact = write_decision_report(data, destination)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(destination).pages)

    assert destination.read_bytes() == artifact.pdf_bytes
    assert "NimbusCRM 境外 SaaS 上线前审查" in text
    assert "建议附条件通过" in text
    assert "尚未提供分处理者清单" in text
    assert "个人信息保护负责人" in text
    assert "签署版影响评估与版本号" in text
    assert "c" * 64 not in text
