"""Bounded tools exposed to the single compliance Agent."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from law_agent.config import (
    RerankMode,
    require_rerank_config,
    require_service_config,
)
from law_agent.data.chunking.law import article_ordinal
from law_agent.review.agent import (
    AgentState,
    EvidenceRead,
    web_findings_from_steps,
)
from law_agent.review.citations import group_citations
from law_agent.review.dates import business_today
from law_agent.review.ids import make_id
from law_agent.review.result_builder import (
    LLMReviewResultDraft,
    MaterialEvidenceDraft,
    ReviewIssueDraft,
    ReviewQuestionDraft,
    attach_citation_refs,
    inject_citation_markers,
    report_evidence,
    validate_business_questions,
    validate_grounded_claims,
    validate_legal_basis_gaps,
    validate_plain_text_summary,
)
from law_agent.review.retrieval.boosts import apply_boosts_to_hits, outside_explicit_scope
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.retrieval.fusion import rrf_fuse, source_aware_fuse
from law_agent.review.retrieval.later_provisions import (
    index_later_provisions,
    later_provisions_as_of,
)
from law_agent.review.retrieval.locator import resolve_source_title
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.retrieval.packets import (
    build_source_evidence_packets,
    enrich_hit_for_citation,
)
from law_agent.review.retrieval.rerank import rerank_hits
from law_agent.review.retrieval.scopes import (
    SCOPE_ALL,
    SCOPE_NATIONAL,
    available_scopes,
    catalog_sources_for_scope,
    chunk_in_scope,
    validate_scope,
)
from law_agent.review.retrieval.selection import select_evidence
from law_agent.review.retrieval.service_backends import build_service_adapters
from law_agent.review.retrieval.temporal import filter_hits_as_of
from law_agent.review.schemas import (
    CitationGroup,
    EvidenceIssue,
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
from law_agent.review.source_navigation import SourceNavigation
from law_agent.review.web_research import (
    DEFAULT_WEB_RESULTS,
    WebResearch,
    WebSearchBatch,
    build_web_search_client,
    canonical_url,
    eligible_material_web_urls,
)
from law_agent.review.workflow import review_needs_source_verification

if TYPE_CHECKING:
    from law_agent.review.enterprise_store import MaterialVersion


# How many chunks one explicit read may return, and how far above the retrieval
# scores an explicitly requested clause is placed. The Agent asks for a clause
# after reading the first round of evidence, so that clause has to reach the
# final evidence set instead of being re-ranked away as one more candidate.
MAX_READ_CHUNKS = 5
RESULT_DEPTH = 8  # per query, ranked locations listed in a search's results
SOURCE_SLOTS = 2  # sections of one non-citable or unarticled document among a query's listed positions
NATIONAL_NON_CLAUSE_SLOTS = 3  # max non-citable (FAQ/guide/standard) locations in a national query's top RESULT_DEPTH
PREVIEW_CHARS = 250  # a listed passage this short is handed over whole; longer ones show this much of their most relevant part


def _location(hit: RetrievalHit) -> tuple[str, str]:
    return hit.source_id, hit.article_no or hit.chunk_id


def _spread_sources(
    hits: list[RetrievalHit],
    *,
    max_non_clause: int | None = None,
) -> list[RetrievalHit]:
    """Keep at most ``SOURCE_SLOTS`` entries of one non-citable or unarticled document in top ``RESULT_DEPTH``.

    A long document split into sections (a regional list, a guide, a Q&A) or a
    non-citable contract template with numbered clauses can otherwise fill the
    listed positions and push other sources out of view. Formal citable legal
    articles (``article_no`` and ``can_cite_clause``) are separate rules and are
    never deferred. When ``max_non_clause`` is set, non-citable passages are
    also capped collectively so formal legal clauses retain slots in the top
    ``RESULT_DEPTH``. Deferred hits keep their order and follow right after the
    top positions (backfilling if fewer than ``RESULT_DEPTH`` locations qualify).
    """

    front: list[RetrievalHit] = []
    deferred: list[RetrievalHit] = []
    per_source: dict[str, set] = {}
    locations: set = set()
    non_clause_locations: set = set()
    index = 0
    while index < len(hits) and len(locations) < RESULT_DEPTH:
        hit = hits[index]
        index += 1
        location = _location(hit)
        is_citable_article = bool(hit.article_no and hit.can_cite_clause)
        section_key = location if is_citable_article else (hit.source_id, hit.chunk_id)
        sections = per_source.setdefault(hit.source_id, set())
        source_ok = is_citable_article or section_key in sections or len(sections) < SOURCE_SLOTS
        clause_ok = (
            max_non_clause is None
            or hit.can_cite_clause
            or section_key in non_clause_locations
            or len(non_clause_locations) < max_non_clause
        )
        if source_ok and clause_ok:
            if not is_citable_article:
                sections.add(section_key)
            if not hit.can_cite_clause:
                non_clause_locations.add(section_key)
            locations.add(location)
            front.append(hit)
        else:
            deferred.append(hit)
    ordered = front + deferred + hits[index:]
    return [hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(ordered)]


_SENTENCE_END = re.compile(r"(?<=[。；！？\n])")


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text)
    return {text[i:i + 2] for i in range(len(text) - 1)}


def preview_for(hit: RetrievalHit, query_text: str) -> str:
    """The part of a passage that bears on the query, so the Agent can judge whether to read it.

    A fixed opening often says nothing: a Q&A section starts with a date line or
    another question, an article with its generic subject. The preview starts at
    the sentence sharing the most character pairs with the query; a chunk that
    continues an earlier section is prefixed with that section's heading.
    """

    text = hit.text
    sentences = [part for part in _SENTENCE_END.split(text) if part.strip()]
    wanted = _bigrams(query_text)
    start, best = 0, 0
    offset = 0
    for sentence in sentences:
        score = len(_bigrams(sentence) & wanted)
        if score > best:
            start, best = offset, score
        offset += len(sentence)
    window = text[start:start + PREVIEW_CHARS].strip()
    heading = hit.heading_path[-1] if hit.heading_path else ""
    shown = heading and heading != "问答" and not hit.article_no and not text.lstrip().startswith(heading[:12])
    prefix = f"【{heading}】" if shown else ""
    return prefix + ("……" if start else "") + window


def _whole_and_short(hit: RetrievalHit) -> bool:
    """The passage is a whole unit (a single-chunk article, or a section chunk) that fits in a preview."""

    return len(hit.text) <= PREVIEW_CHARS and (
        not hit.full_article_text or hit.full_article_text.strip() == hit.text.strip())
EXPLICIT_READ_SCORE_MARGIN = 1.0


def agent_evidence_self_check(draft: LLMReviewResultDraft, verdict: SemanticVerdict | None) -> EvidenceSelfCheck:
    sources = [("报告缺口栏目", draft.missing_information), ("独立核验记录", verdict.missing_facts if verdict else [])]
    issues = [
        EvidenceIssue(issue_type="critical_facts_missing",
                      description=f"{source}包含 {len(dict.fromkeys(facts))} 项待核事实，具体内容保留在该来源记录中。")
        for source, facts in sources if facts
    ]
    if draft.legal_basis_gaps:
        issues.append(EvidenceIssue(issue_type="no_primary_legal_basis", description="；".join(draft.legal_basis_gaps)))
    return EvidenceSelfCheck(
        status="insufficient" if draft.risk_level == "insufficient_evidence" or issues else "sufficient",
        issues=issues,
        triggered_reasons=[f"{source}仍有待核事实" for source, facts in sources if facts]
        + (["当前目标仍有待核法律依据"] if draft.legal_basis_gaps else []),
    )


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
    questions: Sequence[ReviewQuestionDraft] = (),
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
        related_questions = draft.resolve_questions(questions)
        material_evidence = _ground_material_evidence(
            draft.material_evidence, material_versions_by_id
        )
        excerpts = {(item.material_version_id, item.start_offset) for item in material_evidence}
        if draft.kind == "material_conflict" and len(excerpts) < 2:
            raise ValueError(
                f"material_conflict「{draft.title}」只有 {len(excerpts)} 条有效材料原文，"
                "需要两条不同的材料原文作为冲突双方证据。"
                "如果另一方来自确认填报或人工陈述，请改用 missing_information，"
                "在 finding 中说明双方来源和值，通过 question_indexes 关联待核实业务问题；"
                "material_evidence 仅保留真实材料摘录，不把事实快照伪装成材料。"
            )
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
                    "如果只是待确认事实，请改为 missing_information 并通过 question_indexes 关联业务问题。"
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
                unknowns=[question.text.strip() for question in related_questions],
                recommended_action=draft.recommended_action,
                answer_type=related_questions[0].answer_type if len(related_questions) == 1 else None,
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
        rerank_mode: RerankMode | None = None,
        model_id: str | None = None,
        national_non_clause_slots: int | None = NATIONAL_NON_CLAUSE_SLOTS,
    ):
        self._chunks = load_corpus(chunks_path)
        self._chunks_by_id = {chunk.chunk_id: chunk for chunk in self._chunks}
        self._default_as_of_date = business_today()
        self._sources_with_articles = {
            chunk.source_id for chunk in self._chunks if chunk.article_no
        }
        self._later_provisions = index_later_provisions(self._chunks)
        self._top_k = top_k
        self._national_non_clause_slots = national_non_clause_slots
        self._question = question
        self._material_text = material_text
        self._material_versions_by_id = {version.id: version for version in material_versions}
        # Deployment setting (RERANK_MODE); ``rerank_mode`` only overrides it for evaluation A/B runs.
        self._rerank_config = require_rerank_config(mode=rerank_mode)
        self._rerank_mode = self._rerank_config.mode
        self._candidate_hits: dict[str, RetrievalHit] = {}
        self._search_results: list[dict[str, Any]] = []
        self._last_query_candidates: list[list[RetrievalHit]] = []
        self._announced_scopes: set[str] = set()
        self._last_scope_catalog: dict[str, list[dict[str, str]]] = {}
        self._adapters = build_service_adapters(require_service_config())
        self._web_research: WebResearch | None = None
        self._semantic_verifier = SemanticGroundingVerifier(model_id=model_id) if model_id else None

    def available_scopes(self) -> list[str]:
        return available_scopes(self._chunks)

    def close(self) -> None:
        self._adapters.close()

    def evidence_as_of(self, evidence: Sequence[RetrievalHit], as_of: date) -> list[RetrievalHit]:
        return filter_hits_as_of(evidence, self._chunks_by_id, as_of=as_of)

    def _finalize_issues(
        self,
        drafts: list[ReviewIssueDraft],
        result_evidence: list[RetrievalHit],
        citation_groups: list[CitationGroup],
        questions: Sequence[ReviewQuestionDraft],
    ) -> list[ReviewIssue]:
        """Ground issues against this task's frozen material and evidence only."""

        return finalize_issues(
            drafts,
            evidence=result_evidence,
            citation_groups=citation_groups,
            material_versions_by_id=self._material_versions_by_id,
            questions=questions,
        )

    @staticmethod
    def _search_with_fallback(adapter, query_pairs, top_k, allowed_chunk_ids):
        import logging

        try:
            return adapter.search_many(query_pairs, top_k=top_k, allowed_chunk_ids=allowed_chunk_ids)
        except Exception as exc:
            logging.getLogger(__name__).warning("retrieval adapter %s failed: %s", type(adapter).__name__, exc)
            raise RuntimeError(f"检索后端暂时不可用（{type(adapter).__name__}），请稍后重试或改用其他工具") from exc

    def search(
        self, queries: list[RetrievalQuery], facts: ReviewFacts, *, coarse_scope: bool = False,
        excluded_scope_terms: Sequence[str] = (),
        max_chunks: int | None = None,
    ) -> list[RetrievalHit]:
        if max_chunks is not None and (type(max_chunks) is not int or max_chunks < 1):
            raise ValueError("max_chunks须为正整数，表示本批查询返回的正文总数上限")
        scopes = [validate_scope(query.scope) if query.scope is not None else None for query in queries]
        query_pairs = [(query.text, query.query_type) for query in queries]
        candidate_top_k = 50
        as_of = facts.as_of_date or self._default_as_of_date
        available = filter_hits_as_of(
            [hit_from_chunk(chunk, 0) for chunk in self._chunks], self._chunks_by_id, as_of=as_of,
        )
        allowed_chunk_ids = [
            [
                hit.chunk_id for hit in available
                if (scope is None or chunk_in_scope(self._chunks_by_id[hit.chunk_id], scope))
                and not (coarse_scope and outside_explicit_scope(
                    self._chunks_by_id[hit.chunk_id], facts, f"{self._question}\n{query.text}",
                    excluded_scope_terms, requested_text=self._question,
                ))
            ]
            for query, scope in zip(queries, scopes, strict=True)
        ]
        # 检索后端一次瞬时故障不应毁掉整次审查：降级为工具错误，让 Agent 改道或重试
        keyword = self._search_with_fallback(self._adapters.keyword, query_pairs, candidate_top_k, allowed_chunk_ids)
        vector = self._search_with_fallback(self._adapters.vector, query_pairs, candidate_top_k, allowed_chunk_ids)
        candidates: list[list[RetrievalHit]] = []
        non_clause_cap = getattr(self, "_national_non_clause_slots", NATIONAL_NON_CLAUSE_SLOTS)
        for query, scope, keyword_hits, vector_hits in zip(queries, scopes, keyword, vector, strict=True):
            keyword_hits = filter_hits_as_of(keyword_hits, self._chunks_by_id, as_of=as_of)
            vector_hits = filter_hits_as_of(vector_hits, self._chunks_by_id, as_of=as_of)
            if scope is not None:
                keyword_hits = [hit for hit in keyword_hits if (
                    hit.chunk_id in self._chunks_by_id
                    and chunk_in_scope(self._chunks_by_id[hit.chunk_id], scope)
                )]
                vector_hits = [hit for hit in vector_hits if (
                    hit.chunk_id in self._chunks_by_id
                    and chunk_in_scope(self._chunks_by_id[hit.chunk_id], scope)
                )]
            if coarse_scope:
                scope_text = f"{self._question}\n{query.text}"
                keyword_hits = [hit for hit in keyword_hits if not (
                    hit.chunk_id in self._chunks_by_id
                    and outside_explicit_scope(self._chunks_by_id[hit.chunk_id], facts, scope_text, excluded_scope_terms,
                                               requested_text=self._question)
                )]
                vector_hits = [hit for hit in vector_hits if not (
                    hit.chunk_id in self._chunks_by_id
                    and outside_explicit_scope(self._chunks_by_id[hit.chunk_id], facts, scope_text, excluded_scope_terms,
                                               requested_text=self._question)
                )]
            keyword_hits = apply_boosts_to_hits(
                keyword_hits,
                self._chunks_by_id, facts,
                query_scope=scope,
            )
            vector_hits = apply_boosts_to_hits(
                vector_hits,
                self._chunks_by_id, facts,
                query_scope=scope,
            )
            fused_query = rrf_fuse(keyword_hits, vector_hits, top_k=candidate_top_k)
            # The vector index returns only a few columns (no article number, heading or document type),
            # so every candidate takes its metadata from the corpus before anything reads it.
            fused_query = [enrich_hit_for_citation(hit, self._chunks_by_id) for hit in fused_query]
            if self._rerank_mode != "off":
                fused_query = self._rerank_or_keep(fused_query, facts, query, candidate_top_k)
            candidates.append(
                _spread_sources(
                    fused_query,
                    max_non_clause=non_clause_cap if scope == SCOPE_NATIONAL else None,
                )
            )
        query_candidates = [
            hit.model_copy(update={
                "source_has_articles": hit.source_id in self._sources_with_articles,
                "matched_query_ids": [query.query_id],
            })
            for query, hits in zip(queries, candidates, strict=True) for hit in hits
        ]
        self._candidate_hits.update({hit.chunk_id: hit for hit in query_candidates})
        self._last_query_candidates = candidates
        bodies = select_evidence(
            query_candidates,
            max_chunks=max_chunks if max_chunks is not None else self._top_k,
            listed_depth=RESULT_DEPTH,
        )
        bodies += self._whole_short_passages(queries, candidates, bodies)
        self._search_results = self._result_listing(queries, candidates, bodies)
        self._update_scope_catalog(scopes, [self._chunks_by_id[hit.chunk_id] for hit in available])
        return self._with_later_provisions(bodies, as_of)

    def _update_scope_catalog(self, scopes: Sequence[str | None], available_chunks: Sequence[Any]) -> None:
        announced = getattr(self, "_announced_scopes", None)
        if announced is None:
            announced = set()
            self._announced_scopes = announced
        catalog: dict[str, list[dict[str, str]]] = {}
        for scope in scopes:
            if not scope or scope == SCOPE_ALL or scope in announced:
                continue
            entries = catalog_sources_for_scope(available_chunks, scope)
            if entries:
                catalog[scope] = entries
            announced.add(scope)
        self._last_scope_catalog = catalog

    def scope_catalog(self) -> dict[str, list[dict[str, str]]]:
        """Newly announced source titles for scopes first queried in the latest search."""
        return dict(getattr(self, "_last_scope_catalog", {}))

    def _with_later_provisions(self, hits: list[RetrievalHit], as_of: date) -> list[RetrievalHit]:
        later = later_provisions_as_of([hit.source_id for hit in hits], self._later_provisions,
                                       self._chunks_by_id, as_of=as_of)
        return [hit.model_copy(update={"later_provisions": later[hit.source_id]}) if hit.source_id in later else hit
                for hit in hits]

    def _rerank_or_keep(self, fused, facts, query, top_k):
        """Rerank one query's fused list; on a reranker failure keep the fused order."""

        import logging

        try:
            return rerank_hits(
                fused, question=self._question, material_text=self._material_text, facts=facts,
                queries=[query], top_k=top_k, mode=self._rerank_mode, config=self._rerank_config,
                query_text=query.text, chunks_by_id=self._chunks_by_id,
            ).hits
        except Exception as exc:  # 重排只改善排序，不可用时不应让整次检索失败
            logging.getLogger(__name__).warning("rerank failed, keeping fused order: %s", exc)
            return fused

    def _whole_short_passages(self, queries, candidates, bodies):
        """Listed passages short enough to show whole are handed over as evidence, outside ``max_chunks``.

        Their full text costs about as much as a preview would; showing it as an
        uncitable preview would only force a read to get the same words cited.
        """

        taken = {_location(hit) for hit in bodies}
        found: dict[tuple, tuple[RetrievalHit, list[str]]] = {}
        for query, hits in zip(queries, candidates, strict=True):
            for hit in hits[:RESULT_DEPTH]:
                location = _location(hit)
                enriched = self._candidate_hits[hit.chunk_id]
                if location not in taken and _whole_and_short(enriched):
                    found.setdefault(location, (enriched, []))[1].append(query.query_id)
        return [hit.model_copy(update={"matched_query_ids": list(dict.fromkeys(ids))}) for hit, ids in found.values()]

    def _result_listing(self, queries, candidates, bodies):
        """One ranked list per search, grouped by source.

        Each location is either acquired (its text is in the evidence and citable)
        or listed with a preview to read if needed. Every query contributes its top
        ``RESULT_DEPTH`` locations, plus wherever its acquired passages ranked.
        """

        acquired = {_location(hit): hit.chunk_id for hit in bodies}
        entries: dict[tuple, dict[str, Any]] = {}
        for query, hits in zip(queries, candidates, strict=True):
            for rank, hit in enumerate(hits, start=1):
                location = _location(hit)
                if rank > RESULT_DEPTH and location not in acquired:
                    continue
                entry = entries.setdefault(location, {
                    "source_id": hit.source_id, "title": hit.title, "doc_type": hit.doc_type,
                    "location": {"chunk_id": acquired.get(location, hit.chunk_id), "article_no": hit.article_no,
                                 **({"acquired": True} if location in acquired else
                                    {"preview": preview_for(hit, query.text)}),
                                 "query_matches": []},
                })
                matches = entry["location"]["query_matches"]
                if not any(match["query_id"] == query.query_id for match in matches):
                    matches.append({"query_id": query.query_id, "rank": rank})
        groups: dict[str, dict[str, Any]] = {}
        for entry in sorted(entries.values(), key=lambda e: min(m["rank"] for m in e["location"]["query_matches"])):
            group = groups.setdefault(entry["source_id"], {
                "source_id": entry["source_id"], "title": entry["title"], "doc_type": entry["doc_type"],
                "source_has_articles": entry["source_id"] in self._sources_with_articles,
                "outline_read_request": {"source_id": entry["source_id"]}, "locations": [],
            })
            group["locations"].append(entry["location"])
        return list(groups.values())

    def search_results(self) -> list[dict[str, Any]]:
        return [dict(group) for group in self._search_results]

    def last_query_candidates(self) -> list[list[RetrievalHit]]:
        """Each query's own fused ranking from the latest search, before evidence selection."""
        return [list(hits) for hits in self._last_query_candidates]

    def read_evidence(
        self,
        source_id: str | None,
        article_no: str | None,
        chunk_id: str | None,
        facts: ReviewFacts,
        offset: int = 0,
        *,
        node_id: str | None = None,
        parent_node_id: str | None = None,
        source_title: str | None = None,
    ) -> EvidenceRead:
        """Read controlled passages by discovered ID or explicit title and article."""

        as_of = facts.as_of_date or self._default_as_of_date
        if source_title is not None:
            if source_id is not None or article_no is None:
                raise ValueError("source_title须与article_no一起使用，不能同时指定source_id")
            source_id = resolve_source_title(source_title, self._chunks_by_id, as_of=as_of)

        if offset < 0:
            raise ValueError("offset 必须大于或等于 0")
        if any(value is not None and not value.strip() for value in (
            article_no, chunk_id, node_id, parent_node_id,
        )):
            raise ValueError("证据定位参数不能为空")
        if sum(value is not None for value in (
            article_no, chunk_id, node_id, parent_node_id,
        )) > 1:
            raise ValueError("article_no、chunk_id、node_id、parent_node_id 最多指定一个")
        source_chunks = sorted(
            (chunk for chunk in self._chunks if chunk.source_id == source_id),
            key=lambda chunk: (chunk.doc_id, chunk.chunk_index, chunk.chunk_id),
        )
        if not source_chunks:
            raise ValueError(f"受控法律库中没有来源 {source_id}")

        available = filter_hits_as_of(
            [hit_from_chunk(chunk, rank) for rank, chunk in enumerate(source_chunks)],
            self._chunks_by_id, as_of=as_of,
        )
        available_chunks = [self._chunks_by_id[hit.chunk_id] for hit in available]
        navigation = (
            SourceNavigation(source_chunks, available_chunks, as_of)
            if node_id or not (article_no or chunk_id) else None
        )
        if not (article_no or chunk_id or node_id):
            assert navigation is not None
            entries = navigation.children(parent_node_id)
            page = entries[offset:offset + 30]
            if not page and (offset or not available_chunks):
                raise ValueError(f"来源 {source_id} 在 {as_of.isoformat()} 没有 offset={offset} 的目录条目")
            return EvidenceRead(
                hits=[], mode="outline", outline=page,
                next_offset=offset + len(page) if offset + len(page) < len(entries) else None,
            )

        next_offset: int | None = None
        previous_chunk_id: str | None = None
        following_chunk_id: str | None = None
        if node_id:
            assert navigation is not None
            matched = navigation.node(node_id).chunks
            selected = matched[offset:offset + MAX_READ_CHUNKS]
            if not selected:
                raise ValueError(f"章节只有 {len(matched)} 块，offset={offset} 超出章节范围")
            if offset + len(selected) < len(matched):
                next_offset = offset + len(selected)
        elif chunk_id:
            if offset:
                raise ValueError("按 chunk_id 读取不使用 offset；请改用相邻 chunk_id 继续读取")
            anchor = self._chunks_by_id.get(chunk_id)
            if anchor is None or anchor.source_id != source_id:
                raise ValueError(f"chunk {chunk_id} 不属于来源 {source_id}")
            if anchor not in available_chunks:
                raise ValueError(f"chunk {chunk_id} 在 {as_of.isoformat()} 不是现行有效版本")
            document_chunks = [chunk for chunk in available_chunks if chunk.doc_id == anchor.doc_id]
            anchor_index = document_chunks.index(anchor)
            start = anchor_index
            selected = [anchor]
            previous_chunk_id = document_chunks[start - 1].chunk_id if start else None
            end = start + len(selected)
            following_chunk_id = document_chunks[end].chunk_id if end < len(document_chunks) else None
        else:
            requested = (article_no or "").strip()
            matched = [
                chunk
                for chunk in available_chunks
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
            hits=self._with_later_provisions([
                hit.model_copy(update={
                    "score": round(base + EXPLICIT_READ_SCORE_MARGIN, 6),
                    "source_has_articles": source_id in self._sources_with_articles,
                })
                for hit in hits
            ], as_of),
            next_offset=next_offset,
            previous_chunk_id=previous_chunk_id,
            following_chunk_id=following_chunk_id,
        )

    def search_web(
        self, queries: list[RetrievalQuery], facts: ReviewFacts, max_results: int = DEFAULT_WEB_RESULTS,
    ) -> WebSearchBatch:
        """Continue the investigation on public pages.

        Built lazily so a deployment without a search key still runs reviews
        normally: the Agent only learns the capability is unavailable when it
        actually asks for it.
        """

        return self._web().search(queries, facts, max_results=max_results)

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
        validate_legal_basis_gaps(draft)
        validate_business_questions(draft)
        web_findings = web_findings_from_steps(state)
        eligible_urls = eligible_material_web_urls(web_findings)
        finding_urls = {canonical_url(url) for url in eligible_urls}
        if any(canonical_url(url) not in finding_urls for url in draft.material_web_urls):
            raise ValueError(
                "web_impacts只能包含新官方材料或有更新迹象的已入库 URL。"
                f"本次可选 URL：{eligible_urls}。若列表为空，请填web_impacts=[]；"
                "已入库材料请用正式检索结果引用"
            )
        if draft.web_impact == "core" and draft.risk_level != "insufficient_evidence":
            raise ValueError("可能改变核心法律路径的新法源尚未核验，必须暂缓确定结论")
        if draft.risk_level == "insufficient_evidence" and draft.legal_path is not None:
            raise ValueError("证据不足时不能确定法律路径")
        as_of = state.facts.as_of_date or self._default_as_of_date
        result_evidence = filter_hits_as_of(state.evidence, self._chunks_by_id, as_of=as_of)
        evidence_ids = {hit.chunk_id for hit in result_evidence}
        cited_ids = {chunk_id for claim in draft.claims for chunk_id in claim.supporting_chunk_ids}
        cited_ids.update(chunk_id for issue in draft.issues for chunk_id in issue.supporting_chunk_ids)
        cited_ids.update(draft.reference_chunk_ids)
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
            neighbor_hits=[],
            chunks_by_id=self._chunks_by_id,
        )
        claims = validate_grounded_claims(draft.claims, result_evidence)
        if draft.risk_level != "insufficient_evidence" and not claims:
            raise ValueError("正式风险结论至少需要一条可引用法条支持")
        all_citation_groups, _ = group_citations(
            result_evidence, state.facts, self._chunks_by_id
        )
        full_articles = {
            citation.chunk_id: citation.full_article_text
            for group in all_citation_groups for citation in group.citations
            if citation.full_article_text
        }
        verifier_evidence = [
            hit.model_copy(update={"full_article_text": full_articles[hit.chunk_id]})
            if hit.chunk_id in full_articles else hit
            for hit in result_evidence
        ]
        selected = report_evidence(result_evidence, claims=claims, reference_chunk_ids=draft.reference_chunk_ids,
                                   additional_chunk_ids=[cid for issue in draft.issues for cid in issue.supporting_chunk_ids])
        citation_groups, _ = group_citations(selected, state.facts, self._chunks_by_id)
        claims = attach_citation_refs(claims, citation_groups)
        citations = [citation for group in citation_groups for citation in group.citations]
        issues = self._finalize_issues(draft.issues, result_evidence, citation_groups, draft.questions)
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
            verdict = None
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
                as_of_date=as_of,
                web_findings=web_findings,
            )
        if verdict is not None and verdict.status != "supported":
            raise SemanticGroundingRejected(verdict)
        trace_id = make_id("trace")
        result = ReviewResult(
            review_result_id=make_id("result"),
            review_case_id=case_id,
            trace_id=trace_id,
            risk_level=draft.risk_level,
            decision_summary=decision_summary,
            legal_path=draft.legal_path,
            conclusion=inject_citation_markers(
                draft.conclusion.rstrip(), claims,
                {c.chunk_id: c.citation_ref for g in citation_groups for c in g.citations},
            ),
            review_facts=state.facts,
            trigger_reasons=draft.trigger_reasons,
            missing_information=draft.missing_information,
            legal_basis_gaps=draft.legal_basis_gaps,
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
            "semantic_grounding": verdict.model_dump(mode="json") if verdict else {
                "status": "not_run", "reason": "system_abstention",
            },
            "evidence_self_check": agent_evidence_self_check(draft, verdict).model_dump(mode="json"),
            "citation_groups": [item.model_dump(mode="json") for item in citation_groups],
            "second_retrieval_triggered": False,
            "retrieval_queries": [item.model_dump(mode="json") for item in state.queries],
            "evidence_chunks": [item.model_dump(mode="json") for item in result_evidence],
            "source_evidence_packets": [
                item.model_dump(mode="json") for item in source_packets
            ],
            "intake_snapshot": intake_snapshot,
            "web_findings": [item.model_dump(mode="json") for item in web_findings],
            "web_impact": draft.web_impact,
            "material_web_urls": draft.material_web_urls,
            "freshness_hold": review_needs_source_verification({
                "review_result": result.model_dump(mode="json"), "web_impact": draft.web_impact,
            }),
            "agent": {
                "plan": state.plan,
                "turns": state.turns,
                "searches": state.searches,
            },
        }
