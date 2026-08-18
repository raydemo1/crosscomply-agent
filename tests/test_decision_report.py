from __future__ import annotations

import hashlib

from pypdf import PdfReader

from law_agent.review.reports import (
    DecisionReportData,
    LegalSource,
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
    assert "CASE-2026-001" in text
    assert "a" * 64 in text
    assert "national-path-2026.08" in text
    assert "个人信息保护法 | 第三十八条" in text
    assert "签署标准合同" in text
    assert "审核人张三" in text
    assert "2026-08-18T12:00:00+08:00" in text
