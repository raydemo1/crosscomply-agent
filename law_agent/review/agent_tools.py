"""Bounded tools exposed to the single compliance Agent."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from law_agent.config import (
    RerankMode,
    load_rerank_config,
    require_service_config,
)
from law_agent.review.agent import AgentState
from law_agent.review.citations import group_citations
from law_agent.review.evidence import run_self_check
from law_agent.review.ids import make_id
from law_agent.review.result_builder import (
    LLMReviewResultDraft,
    MaterialEvidenceDraft,
    ReviewIssueDraft,
    attach_citation_refs,
    validate_decision_summary,
    validate_grounded_claims,
)
from law_agent.review.retrieval.boosts import apply_boosts_to_hits
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.retrieval.fusion import rrf_fuse, source_aware_fuse
from law_agent.review.retrieval.hits import merge_hits_by_chunk_id
from law_agent.review.retrieval.neighbors import expand_neighbors
from law_agent.review.retrieval.rerank import rerank_hits
from law_agent.review.retrieval.service_backends import build_service_adapters
from law_agent.review.retrieval.temporal import filter_hits_as_of
from law_agent.review.schemas import (
    CitationGroup,
    GroundedClaim,
    MaterialEvidenceRef,
    RetrievalHit,
    RetrievalQuery,
    ReviewFacts,
    ReviewIssue,
    ReviewResult,
    SourceEvidencePacket,
)
from law_agent.review.service import (
    build_source_evidence_packets,
    flatten_source_evidence_packets,
)
from law_agent.review.web_research import (
    WebFinding,
    WebResearch,
    build_web_search_client,
    canonical_url,
)

if TYPE_CHECKING:
    from law_agent.review.enterprise_store import MaterialVersion


def _ground_material_evidence(
    drafts: list[MaterialEvidenceDraft],
    material_versions_by_id: dict[str, MaterialVersion],
) -> list[MaterialEvidenceRef]:
    """Verify each proposed excerpt against the frozen material versions.

    The model never supplies filename, version number or offsets: an excerpt
    is accepted only when its ``material_version_id`` is part of this task's
    frozen snapshot and its ``quote`` occurs exactly once in that version's
    parsed text.
    """

    grounded: list[MaterialEvidenceRef] = []
    for draft in drafts:
        version = material_versions_by_id.get(draft.material_version_id)
        if version is None:
            raise ValueError(f"材料引用不属于本次冻结材料：{draft.material_version_id}")
        quote = draft.quote
        if not quote.strip():
            raise ValueError("材料引用必须包含非空原文")
        parsed_text = version.parsed_text or ""
        start = parsed_text.find(quote)
        if start < 0:
            raise ValueError(f"材料原文中找不到该引用：{quote[:80]}")
        if parsed_text.find(quote, start + 1) >= 0:
            raise ValueError(f"引用文本在材料中出现多次，请提供更完整原文：{quote[:80]}")
        grounded.append(
            MaterialEvidenceRef(
                material_version_id=version.id,
                logical_name=version.logical_name,
                filename=version.filename,
                version_number=version.version_number,
                quote=quote,
                start_offset=start,
                end_offset=start + len(quote),
            )
        )
    return grounded


def finalize_issues(
    drafts: list[ReviewIssueDraft],
    *,
    evidence: list[RetrievalHit],
    citation_groups: list[CitationGroup],
    material_versions_by_id: dict[str, MaterialVersion],
) -> list[ReviewIssue]:
    """Apply the deterministic gates for each issue kind.

    ``material_conflict`` needs two distinct grounded excerpts, ``legal_gap``
    needs a material excerpt *and* a citable legal chunk from the current
    evidence set, and ``missing_information`` needs at least one unknown. Legal
    chunk ids reuse the same claim grounding as the report, so an issue can
    never carry a citation the report itself could not.
    """

    issues: list[ReviewIssue] = []
    for draft in drafts:
        material_evidence = _ground_material_evidence(
            draft.material_evidence, material_versions_by_id
        )
        excerpts = {(item.material_version_id, item.start_offset) for item in material_evidence}
        if draft.kind == "material_conflict" and len(excerpts) < 2:
            raise ValueError("material_conflict 至少需要两条不同的材料原文作为冲突双方证据")
        if draft.kind == "missing_information" and not any(
            unknown.strip() for unknown in draft.unknowns
        ):
            raise ValueError("missing_information 必须写明尚未确认的内容")
        grounded_claims = validate_grounded_claims(
            [
                GroundedClaim(
                    text=draft.finding,
                    supporting_chunk_ids=draft.supporting_chunk_ids,
                )
            ],
            evidence,
        )
        if draft.kind == "legal_gap" and (not material_evidence or not grounded_claims):
            raise ValueError("legal_gap 必须同时具备材料事实和可引用法条支持")
        grounded_claims = attach_citation_refs(grounded_claims, citation_groups)
        issues.append(
            ReviewIssue(
                id=make_id("issue"),
                kind=draft.kind,
                title=draft.title,
                finding=draft.finding,
                material_evidence=material_evidence,
                supporting_chunk_ids=(
                    grounded_claims[0].supporting_chunk_ids if grounded_claims else []
                ),
                supporting_citation_refs=(
                    grounded_claims[0].supporting_citation_refs if grounded_claims else []
                ),
                unknowns=draft.unknowns,
                recommended_action=draft.recommended_action,
            )
        )
    return issues


class ComplianceAgentTools:
    """One-search-batch and governed-finalization capabilities; no arbitrary I/O."""

    def __init__(
        self,
        *,
        chunks_path: Path | str = DEFAULT_CHUNKS_PATH,
        top_k: int = 10,
        question: str = "",
        material_text: str = "",
        material_versions: Sequence[MaterialVersion] = (),
        rerank_mode: RerankMode = "off",
    ):
        self._chunks = load_corpus(chunks_path)
        self._chunks_by_id = {chunk.chunk_id: chunk for chunk in self._chunks}
        self._top_k = top_k
        self._question = question
        self._material_text = material_text
        self._material_versions_by_id = {version.id: version for version in material_versions}
        self._rerank_mode = rerank_mode
        self._rerank_config = load_rerank_config(mode=rerank_mode)
        self._candidate_hits: dict[str, RetrievalHit] = {}
        self._neighbor_hits: dict[str, RetrievalHit] = {}
        self._adapters = build_service_adapters(require_service_config())
        self._web_research: WebResearch | None = None

    def close(self) -> None:
        self._adapters.close()

    def _finalize_issues(
        self,
        drafts: list[ReviewIssueDraft],
        result_evidence: list[RetrievalHit],
        citation_groups: list[CitationGroup],
    ) -> list[ReviewIssue]:
        """Ground issues against this task's frozen material and evidence only."""

        return finalize_issues(
            drafts,
            evidence=result_evidence,
            citation_groups=citation_groups,
            material_versions_by_id=self._material_versions_by_id,
        )

    def search(
        self, queries: list[RetrievalQuery], facts: ReviewFacts
    ) -> list[RetrievalHit]:
        query_pairs = [(query.text, query.query_type) for query in queries]
        candidate_top_k = max(30, self._top_k, self._rerank_config.window)
        keyword = merge_hits_by_chunk_id(
            self._adapters.keyword.search_many(query_pairs, top_k=candidate_top_k),
            top_k=candidate_top_k,
        )
        vector = merge_hits_by_chunk_id(
            self._adapters.vector.search_many(query_pairs, top_k=candidate_top_k),
            top_k=candidate_top_k,
        )
        as_of = facts.as_of_date or datetime.now(UTC).date()
        keyword = filter_hits_as_of(keyword, self._chunks_by_id, as_of=as_of)
        vector = filter_hits_as_of(vector, self._chunks_by_id, as_of=as_of)
        keyword = apply_boosts_to_hits(keyword, self._chunks_by_id, facts)
        vector = apply_boosts_to_hits(vector, self._chunks_by_id, facts)
        fused = rrf_fuse(keyword, vector, top_k=candidate_top_k)
        representatives = source_aware_fuse(
            fused,
            top_k=max(self._top_k, self._rerank_config.window),
            chunks_by_id=self._chunks_by_id,
        )
        reranked = rerank_hits(
            representatives,
            question=self._question,
            material_text=self._material_text,
            facts=facts,
            queries=queries,
            top_k=self._top_k,
            mode=self._rerank_mode,
            config=self._rerank_config,
        ).hits
        neighbors = expand_neighbors(reranked[:5], self._chunks_by_id)
        self._candidate_hits.update({hit.chunk_id: hit for hit in fused})
        self._neighbor_hits.update({hit.chunk_id: hit for hit in neighbors})
        packets = build_source_evidence_packets(
            representative_hits=reranked,
            candidate_hits=fused,
            neighbor_hits=neighbors,
            chunks_by_id=self._chunks_by_id,
        )
        return flatten_source_evidence_packets(packets)

    def search_web(self, queries: list[RetrievalQuery], facts: ReviewFacts) -> list[WebFinding]:
        """Continue the investigation on official public pages.

        Built lazily so a deployment without a search key still runs reviews
        normally: the Agent only learns the capability is unavailable when it
        actually asks for it.
        """

        return self._web().search(queries, facts)

    def _web(self) -> WebResearch:
        if self._web_research is None:
            self._web_research = WebResearch(
                client=build_web_search_client(),
                corpus_chunks=self._chunks,
            )
        return self._web_research

    def finalize(
        self,
        draft: LLMReviewResultDraft,
        state: AgentState,
        *,
        case_id: str,
        rule_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        finding_urls = {
            canonical_url(item.url) for item in state.web_findings
            if item.known_source_id is None or item.refresh_needed
        }
        if draft.web_impact == "none" and draft.material_web_urls:
            raise ValueError("web_impact=none 时 material_web_urls 必须为空列表")
        if draft.web_impact != "none" and not draft.material_web_urls:
            raise ValueError(
                "Web 影响判断必须指出新官方材料 URL；若本次 Web 结果全部已入库，"
                "请填 web_impact=none、material_web_urls=[]"
            )
        if any(canonical_url(url) not in finding_urls for url in draft.material_web_urls):
            eligible = [item.url for item in state.web_findings
                        if canonical_url(item.url) in finding_urls]
            raise ValueError(
                "material_web_urls 只能包含尚未入库或确有更新的官方材料 URL。"
                f"本次可选 URL：{eligible}。若列表为空，请填 web_impact=none、"
                "material_web_urls=[]；已入库材料请用正式检索结果引用"
            )
        if draft.web_impact == "core" and draft.risk_level != "insufficient_evidence":
            raise ValueError("可能改变核心法律路径的新法源尚未核验，必须暂缓确定结论")
        primary_evidence = [hit for hit in state.evidence if hit.rank >= 0 and hit.retriever != "web"]
        representatives = source_aware_fuse(
            primary_evidence,
            top_k=self._top_k,
            chunks_by_id=self._chunks_by_id,
        )
        source_packets: list[SourceEvidencePacket] = build_source_evidence_packets(
            representative_hits=representatives,
            candidate_hits=list(self._candidate_hits.values()) or primary_evidence,
            neighbor_hits=list(self._neighbor_hits.values()),
            chunks_by_id=self._chunks_by_id,
        )
        result_evidence = flatten_source_evidence_packets(source_packets) or primary_evidence
        self_check = run_self_check(representatives, state.facts, self._chunks_by_id)
        if draft.risk_level != "insufficient_evidence" and self_check.status != "sufficient":
            raise ValueError(
                "确定性证据门禁未通过；请继续检索，或以证据不足结论明确停止"
            )
        claims = validate_grounded_claims(draft.claims, result_evidence)
        if draft.risk_level != "insufficient_evidence" and not claims:
            raise ValueError("正式风险结论至少需要一条可引用法条支持")
        citation_groups, _ = group_citations(
            result_evidence, state.facts, self._chunks_by_id
        )
        claims = attach_citation_refs(claims, citation_groups)
        citations = [citation for group in citation_groups for citation in group.citations]
        issues = self._finalize_issues(draft.issues, result_evidence, citation_groups)
        supported_summary_text = "\n".join(
            [
                draft.conclusion,
                json.dumps(state.facts.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(rule_snapshot, ensure_ascii=False),
                *[f"{hit.title}\n{hit.text}" for hit in result_evidence],
            ]
        )
        decision_summary = validate_decision_summary(
            draft.decision_summary,
            supported_text=supported_summary_text,
        )
        trace_id = make_id("trace")
        result = ReviewResult(
            review_result_id=make_id("result"),
            review_case_id=case_id,
            trace_id=trace_id,
            risk_level=draft.risk_level,
            decision_summary=decision_summary,
            conclusion=draft.conclusion.rstrip(),
            review_facts=state.facts,
            trigger_reasons=draft.trigger_reasons,
            missing_information=draft.missing_information,
            recommended_actions=draft.recommended_actions,
            risk_boundaries=draft.risk_boundaries,
            claims=claims,
            citations=citations,
            applicable_evidence=citation_groups,
            issues=issues,
        )
        return {
            "review_case_id": case_id,
            "trace_id": trace_id,
            "review_facts": state.facts.model_dump(mode="json"),
            "review_result": result.model_dump(mode="json"),
            "evidence_self_check": self_check.model_dump(mode="json"),
            "citation_groups": [item.model_dump(mode="json") for item in citation_groups],
            "second_retrieval_triggered": False,
            "retrieval_queries": [item.model_dump(mode="json") for item in state.queries],
            "evidence_chunks": [item.model_dump(mode="json") for item in result_evidence],
            "source_evidence_packets": [
                item.model_dump(mode="json") for item in source_packets
            ],
            "rule_snapshot": rule_snapshot,
            "web_findings": [item.model_dump(mode="json") for item in state.web_findings],
            "web_impact": draft.web_impact,
            "material_web_urls": draft.material_web_urls,
            "freshness_hold": draft.web_impact == "core",
            "agent": {
                "plan": state.plan,
                "turns": state.turns,
                "searches": state.searches,
            },
        }
