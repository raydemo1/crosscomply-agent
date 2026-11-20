"""Exercise regulation impact with synthetic approved cases in a temporary corpus."""

import argparse
import json
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from law_agent.data.io import write_manifest
from law_agent.data.schemas import SourceRecord
from law_agent.evals.arms.crosscomply import enqueue_eval_task
from law_agent.kb.admin import KnowledgeBaseAdminService
from law_agent.kb.change_detection import diff_source
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.governance_store import InMemoryGovernanceStore
from law_agent.review.impact import RegulationImpact, citation_rows
from law_agent.review.impact_store import InMemoryImpactStore
from law_agent.review.transfer_store import InMemoryTransferStore


def run_demo(count: int) -> dict:
    cases = InMemoryCaseStore(seed_password="synthetic-only")
    enterprise = InMemoryEnterpriseStore()
    governance = InMemoryGovernanceStore()
    transfers = InMemoryTransferStore(cases)
    records = InMemoryImpactStore(cases, transfers)
    impact = RegulationImpact(records, cases, enterprise, governance, transfers)
    owner = next(user for user in cases.list_assignable_users() if user.role == "requester")
    assessment = SourceRecord(
        source_id="demo_assessment_2022", title="数据出境安全评估办法",
        source_url="https://example.test/2022/assessment", source_site="example.test",
        effective_date="2022-09-01", doc_type="regulation",
    )
    answers = assessment.model_copy(update={
        "source_id": "demo_answers_2022", "title": "数据出境安全评估办法问答",
        "source_url": "https://example.test/2022/answers",
    })
    flow = assessment.model_copy(update={
        "source_id": "demo_flow_2024", "title": "促进和规范数据跨境流动规定",
        "source_url": "https://example.test/2024/flow", "effective_date": "2024-03-22",
    })
    for number in range(count):
        row = cases.create_case(question=f"演示审查 {number + 1}", material_text="合成材料", created_by=owner.id)
        queued = enqueue_eval_task(enterprise, case_id=row["id"], files=[("材料.txt", "合成材料")],
                                   intake={}, model_id="not-used")
        enterprise.claim_next_task(worker_id="demo")
        result = {"review_result": {"citations": [{
            "chunk_id": f"{source.source_id}:demo", "source_id": source.source_id,
            "title": source.title, "effective_date": source.effective_date, "can_cite_clause": source == assessment,
        } for source in (assessment, answers)]}}
        enterprise.complete_task(queued.id, result=result, final_node="done")
        records.index_citations(queued.id, list(citation_rows(queued.id, row["id"], result, {})))
        approval = governance.create_approval_record(case_id=row["id"], task_id=queued.id, instance_id=f"demo-{number}")
        governance.record_approval_event(
            approval_id=approval.id, provider_event_id=f"event-{number}", event_type="approval",
            signature_valid=True, payload={}, target_status="approved",
        )
        cases.update_case(row["id"], status="approved")
    with TemporaryDirectory() as directory:
        corpus = Path(directory)
        manifest = corpus / "source_manifest.csv"
        service = KnowledgeBaseAdminService(corpus)
        write_manifest(manifest, [assessment, answers])
        before_answers = service.source_snapshot(answers.source_id)
        expired = answers.model_copy(update={"valid_to": flow.effective_date})
        write_manifest(manifest, [assessment, expired, flow])
        changes = [
            diff_source(None, [], *service.source_snapshot(flow.source_id)),
            diff_source(*before_answers, *service.source_snapshot(answers.source_id)),
        ]
        start = perf_counter()
        for change in changes:
            impact.compute(records.record_change(change))
        elapsed = perf_counter() - start
    flags = records.flags()
    return {
        "synthetic": True, "approved_cases": count, "flagged_cases": len({row["case_id"] for row in flags}),
        "reasons": dict(Counter(row["reason"] for row in flags)), "impact_ms": round(elapsed * 1000, 2),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=20)
    args = parser.parse_args()
    if args.cases < 1:
        parser.error("--cases must be positive")
    print(json.dumps(run_demo(args.cases), ensure_ascii=False))
