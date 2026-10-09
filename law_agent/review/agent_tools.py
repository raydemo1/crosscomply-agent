"""Bounded tools exposed to the single compliance Agent."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from law_agent.config import (
    RerankMode,
    load_rerank_config,
    require_service_config,
)
from law_agent.data.chunking.law import article_ordinal
from law_agent.review.agent import AgentState, EvidenceRead, web_findings_from_steps
from law_agent.review.citations import group_citations
from law_agent.review.ids import make_id
from law_agent.review.result_builder import (
    LLMReviewResultDraft,
    MaterialEvidenceDraft,
    ReviewIssueDraft,
    attach_citation_refs,
    validate_grounded_claims,
    validate_plain_text_summary,
)
from law_agent.review.retrieval.boosts import apply_boosts_to_hits
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.retrieval.fusion import rrf_fuse, source_aware_fuse
from law_agent.review.retrieval.hits import merge_hits_by_chunk_id
from law_agent.review.retrieval.neighbors import expand_neighbors, hit_from_chunk
from law_agent.review.retrieval.rerank import rerank_hits
from law_agent.review.retrieval.service_backends import build_service_adapters
from law_agent.review.retrieval.temporal import filter_hits_as_of
from law_agent.review.schemas import (
    CitationGroup,
    EvidenceSelfCheck,
    GroundedClaim,
    MaterialEvidenceRef,
    RetrievalHit,
    RetrievalQuery,
    ReviewFacts,
    ReviewIssue,
    ReviewResult,
    SourceEvidencePacket,
)
from law_agent.review.semantic_grounding import (
    SemanticGroundingRejected,
    SemanticGroundingVerifier,
    SemanticVerdict,
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


# How many chunks one explicit read may return, and how far above the retrieval
# scores an explicitly requested clause is placed. The Agent asks for a clause
# after reading the first round of evidence, so that clause has to reach the
# final evidence set instead of being re-ranked away as one more candidate.
MAX_READ_CHUNKS = 5
EXPLICIT_READ_SCORE_MARGIN = 1.0


def _is_requested_article(chunk_article: str | None, requested: str) -> bool:
    """Whether a chunk's article label is the clause the Agent asked for.

    The library stores the label the instrument uses ("第十三条") while the
    Agent may ask for "第13条" or with a stray space, so the labels are
    compared by the clause number they denote.
    """

    if not chunk_article:
        return False
    if chunk_article == requested:
        return True
    if "".join(chunk_article.split()) == "".join(requested.split()):
        return True
    ordinal = article_ordinal(requested)
    return ordinal is not None and article_ordinal(chunk_article) == ordinal


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
            raise ValueError(
                f"材料引用不属于本次冻结材料：{draft.material_version_id}。"
                f"material_version_id 只能使用材料头部的版本编号："
                f"{list(material_versions_by_id)}；不能使用事实快照编号"
            )
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
            raise ValueError(
                f"material_conflict「{draft.title}」只有 {len(excerpts)} 条有效材料原文，"
                "需要两条不同的材料原文作为冲突双方证据。"
                "如果另一方来自确认填报或人工陈述，请改用 missing_information，"
                "在 finding 中说明双方来源和值，在 unknowns 中列出待核实事项；"
                "material_evidence 仅保留真实材料摘录，不把事实快照伪装成材料。"
            )
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
        ) if draft.supporting_chunk_ids else []
        if draft.kind == "legal_gap":
            if not material_evidence:
                raise ValueError(
                    f"legal_gap「{draft.title}」缺少有效的材料原文证据；"
                    "如果只是待确认事实，请改为 missing_information 并填写 unknowns。"
                    "只有材料原文与正式法条共同支持的具体问题才能标为 legal_gap。"
                )
            if not grounded_claims:
                raise ValueError(
                    f"legal_gap「{draft.title}」缺少可引用的正式法条 chunk；"
                    "请补充 can_cite_clause=true 的 supporting_chunk_ids，"
                    "或改为 missing_information。"
                )
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
                answer_type=draft.answer_type,
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
        model_id: str | None = None,
    ):
        self._chunks = load_corpus(chunks_path)
        self._chunks_by_id = {chunk.chunk_id: chunk for chunk in self._chunks}
        self._sources_with_articles = {
            chunk.source_id for chunk in self._chunks if chunk.article_no
        }
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
        self._semantic_verifier = SemanticGroundingVerifier(model_id=model_id) if model_id else None

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
        neighbors = filter_hits_as_of(
            expand_neighbors(reranked[:5], self._chunks_by_id),
            self._chunks_by_id, as_of=as_of,
        )
        self._candidate_hits.update({hit.chunk_id: hit for hit in fused})
        self._neighbor_hits.update({hit.chunk_id: hit for hit in neighbors})
        packets = build_source_evidence_packets(
            representative_hits=reranked,
            candidate_hits=fused,
            neighbor_hits=neighbors,
            chunks_by_id=self._chunks_by_id,
        )
        return [
            hit.model_copy(update={
                "source_has_articles": hit.source_id in self._sources_with_articles,
            })
            for hit in flatten_source_evidence_packets(packets)
        ]

    def read_evidence(
        self,
        source_id: str,
        article_no: str | None,
        chunk_id: str | None,
        facts: ReviewFacts,
        offset: int = 0,
    ) -> EvidenceRead:
        """Read one clause, or the paragraphs around one chunk, of a known source.

        The first search returns a few fused chunks per source, which is not
        enough to check every parallel condition, exception and exemption in a
        clause. This lets the Agent continue inside a source it has already
        found — and only there: the read is answered from the same controlled
        corpus, so no path, URL or file handle reaches it, and the returned
        chunks are ordinary hits that go through the same citation validation
        and semantic verification as a search result.

        A clause longer than one read is returned as a slice with the offset the
        next read resumes at, never as the whole clause: a silently cut list of
        parallel conditions reads exactly like a complete one.
        """

        source_chunks = sorted(
            (chunk for chunk in self._chunks if chunk.source_id == source_id),
            key=lambda chunk: chunk.chunk_index,
        )
        if not source_chunks:
            raise ValueError(f"受控法律库中没有来源 {source_id}")

        next_offset: int | None = None
        previous_chunk_id: str | None = None
        following_chunk_id: str | None = None
        if chunk_id:
            if offset:
                raise ValueError("按 chunk_id 读取不使用 offset；请改用相邻 chunk_id 继续读取")
            anchor = self._chunks_by_id.get(chunk_id)
            if anchor is None or anchor.source_id != source_id:
                raise ValueError(f"chunk {chunk_id} 不属于来源 {source_id}")
            anchor_index = source_chunks.index(anchor)
            start = max(0, anchor_index - 1)
            selected = source_chunks[start : start + MAX_READ_CHUNKS]
            previous_chunk_id = selected[0].prev_chunk_id
            following_chunk_id = selected[-1].next_chunk_id
        else:
            requested = (article_no or "").strip()
            matched = [
                chunk
                for chunk in source_chunks
                if _is_requested_article(chunk.article_no, requested)
            ]
            if not matched:
                available = sorted({chunk.article_no for chunk in source_chunks if chunk.article_no})
                raise ValueError(
                    f"来源 {source_id} 中没有条款 {requested}；该来源可读取的条款："
                    f"{available[:20] or '无（该来源未按条款切分）'}"
                )
            selected = matched[offset : offset + MAX_READ_CHUNKS]
            if not selected:
                raise ValueError(
                    f"来源 {source_id} 的 {requested} 只有 {len(matched)} 块，"
                    f"offset={offset} 超出该条范围"
                )
            if offset + len(selected) < len(matched):
                next_offset = offset + len(selected)

        as_of = facts.as_of_date or datetime.now(UTC).date()
        hits = [
            hit_from_chunk(chunk, rank) for rank, chunk in enumerate(selected)
        ]
        hits = filter_hits_as_of(hits, self._chunks_by_id, as_of=as_of)
        if not hits:
            raise ValueError(
                f"来源 {source_id} 的该部分内容在 {as_of.isoformat()} 不是现行有效版本"
            )
        base = max((hit.score for hit in self._candidate_hits.values()), default=0.0)
        return EvidenceRead(
            hits=[
                hit.model_copy(update={
                    "score": round(base + EXPLICIT_READ_SCORE_MARGIN, 6),
                    "source_has_articles": source_id in self._sources_with_articles,
                })
                for hit in hits
            ],
            next_offset=next_offset,
            previous_chunk_id=previous_chunk_id,
            following_chunk_id=following_chunk_id,
        )

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
        intake_snapshot: dict[str, Any],
        system_abstention: bool = False,
    ) -> dict[str, Any]:
        finding_urls = {
            canonical_url(item.url) for item in web_findings_from_steps(state)
            if item.known_source_id is None or item.refresh_needed
        }
        if draft.web_impact == "none" and draft.material_web_urls:
            raise ValueError("web_impact=none 时 material_web_urls 必须为空列表")
        if draft.web_impact != "none" and not draft.material_web_urls:
            raise ValueError(
                "Web 影响判断必须指出新官方材料 URL；若本次 Web 结果均无更新迹象，"
                "请填 web_impact=none、material_web_urls=[]"
            )
        if any(canonical_url(url) not in finding_urls for url in draft.material_web_urls):
            eligible = [item.url for item in web_findings_from_steps(state)
                        if canonical_url(item.url) in finding_urls]
            raise ValueError(
                "material_web_urls 只能包含新官方材料或有更新迹象的已入库 URL。"
                f"本次可选 URL：{eligible}。若列表为空，请填 web_impact=none、"
                "material_web_urls=[]；已入库材料请用正式检索结果引用"
            )
        if draft.web_impact == "core" and draft.risk_level != "insufficient_evidence":
            raise ValueError("可能改变核心法律路径的新法源尚未核验，必须暂缓确定结论")
        if draft.risk_level == "insufficient_evidence" and draft.legal_path is not None:
            raise ValueError("证据不足时不能确定法律路径")
        as_of = state.facts.as_of_date or datetime.now(UTC).date()
        result_evidence = filter_hits_as_of(state.evidence, self._chunks_by_id, as_of=as_of)
        evidence_ids = {hit.chunk_id for hit in result_evidence}
        cited_ids = {chunk_id for claim in draft.claims for chunk_id in claim.supporting_chunk_ids}
        cited_ids.update(chunk_id for issue in draft.issues for chunk_id in issue.supporting_chunk_ids)
        invalid_date_ids = cited_ids & {hit.chunk_id for hit in state.evidence} - evidence_ids
        if invalid_date_ids:
            raise ValueError(
                f"以下引用在审查时点 {as_of.isoformat()} 不可用：{sorted(invalid_date_ids)}。"
                "请依据该时点已发布且有效的证据重新判断。"
            )
        primary_evidence = [hit for hit in result_evidence if hit.rank >= 0]
        representatives = source_aware_fuse(
            primary_evidence,
            top_k=self._top_k,
            chunks_by_id=self._chunks_by_id,
        )
        source_packets: list[SourceEvidencePacket] = build_source_evidence_packets(
            representative_hits=representatives,
            candidate_hits=result_evidence,
            neighbor_hits=[hit for hit in self._neighbor_hits.values() if hit.chunk_id in evidence_ids],
            chunks_by_id=self._chunks_by_id,
        )
        claims = validate_grounded_claims(draft.claims, result_evidence)
        if draft.risk_level != "insufficient_evidence" and not claims:
            raise ValueError("正式风险结论至少需要一条可引用法条支持")
        citation_groups, _ = group_citations(
            result_evidence, state.facts, self._chunks_by_id
        )
        full_articles = {
            citation.chunk_id: citation.full_article_text
            for group in citation_groups for citation in group.citations
            if citation.full_article_text
        }
        verifier_evidence = [
            hit.model_copy(update={"full_article_text": full_articles[hit.chunk_id]})
            if hit.chunk_id in full_articles else hit
            for hit in result_evidence
        ]
        claims = attach_citation_refs(claims, citation_groups)
        citations = [citation for group in citation_groups for citation in group.citations]
        issues = self._finalize_issues(draft.issues, result_evidence, citation_groups)
        try:
            decision_summary = validate_plain_text_summary(draft.decision_summary)
        except ValueError as exc:
            raise ValueError(
                f"decision_summary 必须为单段纯文本，当前摘要为：{draft.decision_summary[:240]}。"
                "请删除 Markdown 标记（如 **、#、反引号）和换行，只保留一段中文摘要。"
            ) from exc
        if self._semantic_verifier is None:
            raise RuntimeError("正式报告缺少独立语义证据校验器")
        if system_abstention:
            if draft.risk_level != "insufficient_evidence" or draft.claims or draft.legal_path is not None:
                raise ValueError("预算耗尽时只能生成无确定法律结论的证据不足报告")
            verdict = SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="证据不足且未提出确定法律路径")
        else:
            if any(
                step.observation.get("provenance") not in {"applicant_statement", "reviewer_instruction"}
                for step in state.steps if step.action == "human_input"
            ):
                raise ValueError("人工回答缺少有效来源，请重新审查")
            human_inputs = [
                {
                    "answer": str(step.observation.get("answer") or ""),
                    "provenance": step.observation["provenance"],
                    "intake_snapshot_id": step.observation.get("intake_snapshot_id"),
                    "gate_id": step.observation.get("gate_id"),
                }
                for step in state.steps
                if step.action == "human_input"
                and str(step.observation.get("answer") or "").strip()
            ]
            verdict = self._semantic_verifier(
                review_goal=state.goal,
                draft=draft,
                confirmed_intake=intake_snapshot["facts"],
                extracted_facts=state.facts,
                material=self._material_text,
                evidence=verifier_evidence,
                chunks_by_id=self._chunks_by_id,
                human_inputs=human_inputs,
                fact_ledger=state.fact_ledger,
            )
        if verdict.status != "supported":
            raise SemanticGroundingRejected(verdict)
        trace_id = make_id("trace")
        result = ReviewResult(
            review_result_id=make_id("result"),
            review_case_id=case_id,
            trace_id=trace_id,
            risk_level=draft.risk_level,
            decision_summary=decision_summary,
            legal_path=draft.legal_path,
            conclusion=draft.conclusion.rstrip(),
            review_facts=state.facts,
            trigger_reasons=draft.trigger_reasons,
            missing_information=draft.missing_information,
            missing_answer_types=draft.missing_answer_types,
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
            "fact_ledger": [entry.model_dump(mode="json") for entry in state.fact_ledger],
            "review_result": result.model_dump(mode="json"),
            "semantic_grounding": verdict.model_dump(mode="json"),
            "evidence_self_check": EvidenceSelfCheck(
                status="insufficient" if draft.risk_level == "insufficient_evidence" else "sufficient",
            ).model_dump(mode="json"),
            "citation_groups": [item.model_dump(mode="json") for item in citation_groups],
            "second_retrieval_triggered": False,
            "retrieval_queries": [item.model_dump(mode="json") for item in state.queries],
            "evidence_chunks": [item.model_dump(mode="json") for item in result_evidence],
            "source_evidence_packets": [
                item.model_dump(mode="json") for item in source_packets
            ],
            "intake_snapshot": intake_snapshot,
            "web_findings": [item.model_dump(mode="json") for item in web_findings_from_steps(state)],
            "web_impact": draft.web_impact,
            "material_web_urls": draft.material_web_urls,
            "freshness_hold": draft.web_impact == "core",
            "agent": {
                "plan": state.plan,
                "turns": state.turns,
                "searches": state.searches,
            },
        }
