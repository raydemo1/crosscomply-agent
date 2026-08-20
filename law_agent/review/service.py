"""Application services for material-driven review runs."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from law_agent.config import RerankMode, load_rerank_config
from law_agent.review.agents import (
    build_evidence_dossiers,
    gate_revision_actions,
    run_case_analyst,
    run_evidence_critic,
    run_evidence_researcher,
    select_issue_aware_hits,
    should_run_evidence_critic,
)
from law_agent.review.evidence import (
    evaluate_after_second_retrieval,
    needs_llm_self_check,
    run_self_check,
    run_self_check_with_deepseek,
    validate_llm_self_check,
)
from law_agent.review.facts import (
    FactsExtractor,
    extract_facts_with_deepseek,
)
from law_agent.review.ids import make_id, utc_now_iso
from law_agent.review.io import (
    read_retrieval_traces,
    read_review_cases,
    read_review_results,
    retrieval_traces_path,
    review_cases_path,
    review_results_path,
    write_retrieval_traces,
    write_review_cases,
    write_review_results,
)
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.materials import material_from_text
from law_agent.review.query_planner import (
    QueryPlanner,
    plan_queries_with_deepseek,
)
from law_agent.review.result_builder import (
    build_review_result_with_deepseek,
    revise_review_result_with_deepseek,
)
from law_agent.review.retrieval.adapters import (
    KeywordSearchAdapter,
    VectorSearchAdapter,
)
from law_agent.review.retrieval.boosts import (
    apply_boosts_to_hits,
    compute_boosts_summary,
)
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.retrieval.fusion import rrf_fuse, rrf_fuse_many, source_aware_fuse
from law_agent.review.retrieval.hits import merge_hits_by_chunk_id
from law_agent.review.retrieval.neighbors import expand_neighbors
from law_agent.review.retrieval.rerank import rerank_hits
from law_agent.review.schemas import (
    AgentStep,
    CaseAnalysis,
    EvidenceSelfCheck,
    IssuePlan,
    IssueResearchResult,
    MaterialRecord,
    RetrievalHit,
    RetrievalQuery,
    RetrievalTrace,
    ReviewCase,
    ReviewFacts,
    ReviewMode,
    ReviewResult,
    ReviewRunResponse,
    SourceEvidencePacket,
)
from law_agent.review.telemetry import current_telemetry, reset_telemetry

DEFAULT_REVIEW_RUNS_DIR = Path("data/review_runs")
PLACEHOLDER_CONCLUSION = "Review case created. Evidence retrieval has not run yet."
DEFAULT_TOP_K = 10
DEFAULT_CANDIDATE_TOP_K = 50
ResultFormat = Literal["plain", "markdown"]
DEFAULT_SUPPORTING_CHUNKS_PER_SOURCE = 2
CaseAnalystRunner = Callable[..., CaseAnalysis]


def _build_issue_hit_pools(
    *,
    issue_plan: IssuePlan,
    queries: list[RetrievalQuery],
    keyword_hits_per_query: list[list[RetrievalHit]],
    vector_hits_per_query: list[list[RetrievalHit]],
    chunks_by_id: dict[str, object],
    facts: ReviewFacts,
    top_k: int,
) -> dict[str, list[RetrievalHit]]:
    """Fuse each issue's own queries before cross-issue evidence allocation."""

    query_index = {query.query_id: index for index, query in enumerate(queries)}
    pools: dict[str, list[RetrievalHit]] = {}
    for issue in issue_plan.issues:
        indexes = [query_index[qid] for qid in issue.query_ids if qid in query_index]
        kw = merge_hits_by_chunk_id(
            [keyword_hits_per_query[index] for index in indexes], top_k=top_k
        )
        vec = merge_hits_by_chunk_id(
            [vector_hits_per_query[index] for index in indexes], top_k=top_k
        )
        boosted_kw = apply_boosts_to_hits(kw, chunks_by_id, facts)
        boosted_vec = apply_boosts_to_hits(vec, chunks_by_id, facts)
        pools[issue.issue_id] = source_aware_fuse(
            rrf_fuse(boosted_kw, boosted_vec, top_k=top_k),
            top_k=top_k,
            chunks_by_id=chunks_by_id,
        )
    return pools


def _validate_non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def create_review_case(
    *,
    question: str,
    material_text: str | None = None,
    material: MaterialRecord | None = None,
    output_dir: Path = DEFAULT_REVIEW_RUNS_DIR,
    now: Callable[[], str] = utc_now_iso,
    id_factory: Callable[[str], str] = make_id,
    review_mode: ReviewMode = "llm",
    facts_extractor: FactsExtractor | None = None,
    query_planner: QueryPlanner | None = None,
) -> ReviewRunResponse:
    """Create and persist either an LLM-ready case or a multi-agent shell."""

    reset_telemetry()
    started = time.perf_counter()
    question = _validate_non_blank(question, "question")
    if material is None:
        if material_text is None:
            raise ValueError("material_text is required")
        material = material_from_text(_validate_non_blank(material_text, "material_text"))
    elif material_text is not None:
        raise ValueError("provide either material or material_text, not both")

    created_at = now()
    review_case_id = id_factory("review")
    trace_id = id_factory("trace")
    review_result_id = id_factory("result")

    if review_mode == "llm":
        if facts_extractor is None:
            facts_extractor = extract_facts_with_deepseek
        if query_planner is None:
            query_planner = plan_queries_with_deepseek
        facts = facts_extractor(material.material_text, question)
        queries: list[RetrievalQuery] = query_planner(question, facts, material.material_text)
    else:
        facts = ReviewFacts()
        queries = []
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    telemetry = current_telemetry()

    result = ReviewResult(
        review_result_id=review_result_id,
        review_case_id=review_case_id,
        trace_id=trace_id,
        risk_level="insufficient_evidence",
        decision_summary=(
            "审查任务尚未完成，目前没有足够证据形成可供审批的风险结论。"
            "请等待事实提取、法源检索和证据自检完成后，再依据正式审查结果作出决定。"
        ),
        conclusion=PLACEHOLDER_CONCLUSION,
        review_facts=facts,
    )
    case = ReviewCase(
        review_case_id=review_case_id,
        created_at=created_at,
        question=question,
        material=material,
        review_facts=facts,
        review_mode=review_mode,
        trace_id=trace_id,
        latest_result_id=review_result_id,
    )
    trace = RetrievalTrace(
        trace_id=trace_id,
        review_case_id=review_case_id,
        created_at=created_at,
        evidence_self_check=EvidenceSelfCheck(status="not_checked"),
        queries=queries,
        latency_ms=elapsed_ms,
        total_latency_ms=elapsed_ms,
        llm_call_count=telemetry.llm_call_count,
        retry_count=telemetry.retry_count,
    )

    case_path = review_cases_path(output_dir)
    trace_path = retrieval_traces_path(output_dir)
    result_path = review_results_path(output_dir)
    write_review_cases(case_path, [case])
    write_retrieval_traces(trace_path, [trace])
    write_review_results(result_path, [result])

    return ReviewRunResponse(
        review_case=case,
        trace=trace,
        result=result,
        case_path=case_path,
        trace_path=trace_path,
        result_path=result_path,
    )


# ---------------------------------------------------------------------------
# Hybrid retrieval orchestration
# ---------------------------------------------------------------------------

DEFAULT_NEIGHBOR_COUNT = 10


def _load_case_and_trace(
    case_id: str,
    output_dir: Path,
) -> tuple[ReviewCase, RetrievalTrace, list[RetrievalTrace]]:
    """Load a review case and its trace from the output directory."""

    trace_path = retrieval_traces_path(output_dir)
    if not trace_path.exists():
        raise ValueError(
            f"retrieval traces file does not exist: {trace_path}. "
            "Create a review case first with create_review_case."
        )

    traces = read_retrieval_traces(trace_path)
    target_trace: RetrievalTrace | None = None
    for trace in traces:
        if trace.review_case_id == case_id:
            target_trace = trace
            break

    if target_trace is None:
        raise ValueError(f"review case {case_id} not found in {trace_path}")

    cases_path = review_cases_path(output_dir)
    cases = read_review_cases(cases_path)
    target_case: ReviewCase | None = None
    for case in cases:
        if case.review_case_id == case_id:
            target_case = case
            break

    if target_case is None:
        raise ValueError(f"review case {case_id} not found in {cases_path}")

    return target_case, target_trace, traces


def _dedupe_hits(hits: list[RetrievalHit]) -> list[RetrievalHit]:
    seen: set[str] = set()
    deduped: list[RetrievalHit] = []
    for hit in hits:
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        deduped.append(hit)
    return deduped


def flatten_source_evidence_packets(
    packets: list[SourceEvidencePacket],
) -> list[RetrievalHit]:
    """Return packet evidence as an ordered, de-duplicated chunk list."""

    hits: list[RetrievalHit] = []
    for packet in packets:
        hits.append(packet.representative_chunk)
        hits.extend(packet.supporting_chunks)
        hits.extend(packet.neighbor_chunks)
    return _dedupe_hits(hits)


def _enrich_hit_for_citation(
    hit: RetrievalHit,
    chunks_by_id: dict[str, object],
) -> RetrievalHit:
    """Attach authoritative metadata and the complete cited article to a hit."""

    chunk = chunks_by_id.get(hit.chunk_id)
    if chunk is None:
        return hit

    updates: dict[str, object] = {
        "doc_type": getattr(chunk, "doc_type", hit.doc_type),
        "authority": getattr(chunk, "authority", hit.authority),
        "law_status": getattr(chunk, "law_status", hit.law_status),
        "publish_date": getattr(chunk, "publish_date", hit.publish_date),
        "effective_date": getattr(chunk, "effective_date", hit.effective_date),
        "issuing_body": getattr(chunk, "issuing_body", hit.issuing_body),
        "article_no": getattr(chunk, "article_no", hit.article_no),
        "citation_label": getattr(chunk, "citation_label", hit.citation_label),
        "heading_path": getattr(chunk, "heading_path", hit.heading_path),
    }
    article_no = getattr(chunk, "article_no", None)
    if hit.can_cite_clause and article_no:
        article_chunks = [
            candidate
            for candidate in chunks_by_id.values()
            if getattr(candidate, "source_id", None) == hit.source_id
            and getattr(candidate, "article_no", None) == article_no
        ]
        article_texts: list[str] = []
        seen: set[str] = set()
        for candidate in sorted(
            article_chunks,
            key=lambda item: getattr(item, "chunk_index", 0),
        ):
            text = str(getattr(candidate, "text", "")).strip()
            if text and text not in seen:
                article_texts.append(text)
                seen.add(text)
        if article_texts:
            updates["full_article_text"] = "\n".join(article_texts)
    return hit.model_copy(update=updates)


def build_source_evidence_packets(
    *,
    representative_hits: list[RetrievalHit],
    candidate_hits: list[RetrievalHit],
    neighbor_hits: list[RetrievalHit],
    supporting_per_source: int = DEFAULT_SUPPORTING_CHUNKS_PER_SOURCE,
    chunks_by_id: dict[str, object] | None = None,
) -> list[SourceEvidencePacket]:
    """Attach chunk-level context to each source-aware representative hit."""

    lookup = chunks_by_id or {}
    enriched_representatives = [
        _enrich_hit_for_citation(hit, lookup) for hit in representative_hits
    ]
    enriched_candidates = [_enrich_hit_for_citation(hit, lookup) for hit in candidate_hits]
    enriched_neighbors = [_enrich_hit_for_citation(hit, lookup) for hit in neighbor_hits]

    candidates_by_source: dict[str, list[RetrievalHit]] = {}
    for hit in sorted(enriched_candidates, key=lambda h: (h.rank, -h.score, h.chunk_id)):
        candidates_by_source.setdefault(hit.source_id, []).append(hit)

    neighbors_by_source: dict[str, list[RetrievalHit]] = {}
    for hit in enriched_neighbors:
        neighbors_by_source.setdefault(hit.source_id, []).append(hit)

    packets: list[SourceEvidencePacket] = []
    for representative in enriched_representatives:
        supporting = [
            hit
            for hit in candidates_by_source.get(representative.source_id, [])
            if hit.chunk_id != representative.chunk_id
        ][:supporting_per_source]
        neighbors = [
            hit
            for hit in neighbors_by_source.get(representative.source_id, [])
            if hit.chunk_id != representative.chunk_id
        ]
        packets.append(
            SourceEvidencePacket(
                source_id=representative.source_id,
                title=representative.title,
                representative_chunk=representative,
                supporting_chunks=_dedupe_hits(supporting),
                neighbor_chunks=_dedupe_hits(neighbors),
            )
        )
    return packets


def run_hybrid_retrieval(
    *,
    case_id: str,
    chunks_path: Path | str = DEFAULT_CHUNKS_PATH,
    output_dir: Path = DEFAULT_REVIEW_RUNS_DIR,
    top_k: int = DEFAULT_TOP_K,
    max_neighbors: int = DEFAULT_NEIGHBOR_COUNT,
    review_mode: ReviewMode = "llm",
    rerank_mode: RerankMode = "off",
    keyword_retriever: KeywordSearchAdapter,
    vector_retriever: VectorSearchAdapter,
    output_format: ResultFormat = "plain",
    case_analyst: CaseAnalystRunner | None = None,
) -> RetrievalTrace:
    """Run hybrid retrieval for an existing review case.

    Combines keyword and vector search adapters, applies metadata boosts based
    on ``ReviewFacts``, fuses with RRF, and expands neighbor chunks.
    Persists all component results and the fused result to the trace.

    Both adapters are required. Runtime callers use Elasticsearch and
    pgvector; tests may inject protocol-compatible fakes.
    """

    total_started = time.perf_counter()
    case, target_trace, traces = _load_case_and_trace(case_id, output_dir)
    if case.review_mode != review_mode:
        raise ValueError(
            f"review case {case_id} was created for {case.review_mode!r} mode, not {review_mode!r}"
        )

    facts = case.review_facts
    multi_agent = review_mode == "multi_agent"
    agent_steps: list[AgentStep] = []
    issue_plan = None
    if multi_agent and target_trace.issue_plan is not None and target_trace.queries:
        issue_plan = target_trace.issue_plan
        agent_steps = [
            step for step in target_trace.agent_steps if step.agent_name == "case_analyst"
        ]
    elif multi_agent:
        analyst_started = time.perf_counter()
        analyst_calls_before = current_telemetry().llm_call_count
        analysis = (
            case_analyst(
                question=case.question,
                material_text=case.material.material_text,
                trace_id=target_trace.trace_id,
            )
            if case_analyst is not None
            else run_case_analyst(
                question=case.question,
                material_text=case.material.material_text,
                trace_id=target_trace.trace_id,
            )
        )
        facts = analysis.facts
        issue_plan = analysis.issue_plan
        analyst_step = AgentStep(
            agent_name="case_analyst",
            status="completed",
            decision=(
                f"extracted facts, planned {len(issue_plan.issues)} issues and "
                f"{len(analysis.queries)} total queries"
            ),
            latency_ms=int((time.perf_counter() - analyst_started) * 1000),
            llm_calls=current_telemetry().llm_call_count - analyst_calls_before,
        )
        agent_steps = [analyst_step]
        case = case.model_copy(update={"review_facts": facts})
        target_trace = target_trace.model_copy(
            update={
                "queries": analysis.queries,
                "issue_plan": issue_plan,
                "agent_steps": agent_steps,
            }
        )
        cases = read_review_cases(review_cases_path(output_dir))
        write_review_cases(
            review_cases_path(output_dir),
            [case if item.review_case_id == case_id else item for item in cases],
        )
        traces = [
            target_trace if item.trace_id == target_trace.trace_id else item for item in traces
        ]
        write_retrieval_traces(retrieval_traces_path(output_dir), traces)
        results = read_review_results(review_results_path(output_dir))
        write_review_results(
            review_results_path(output_dir),
            [
                result.model_copy(update={"review_facts": facts})
                if result.review_case_id == case_id
                else result
                for result in results
            ],
        )
    elif not target_trace.queries:
        raise ValueError(
            f"review case {case_id} has no planned queries; "
            "ensure create_review_case ran fact extraction and query planning."
        )

    initial_queries = list(target_trace.queries)

    chunks = load_corpus(chunks_path)
    chunks_by_id: dict[str, object] = {c.chunk_id: c for c in chunks}
    candidate_top_k = max(top_k, DEFAULT_CANDIDATE_TOP_K)
    rerank_config = load_rerank_config(mode=rerank_mode)
    source_fusion_top_k = max(top_k, rerank_config.window) if rerank_mode != "off" else top_k

    retrieval_started = time.perf_counter()

    # LLM mode retains the original case-wide batch retrieval. Multi-agent
    # mode executes an Evidence Researcher for each Case Analyst issue and
    # then lets deterministic fusion combine their tool outputs.
    keyword_hits_per_query: list[list[RetrievalHit]] = []
    vector_hits_per_query: list[list[RetrievalHit]] = []
    query_types: list[str | None] = []
    issue_research_results: list[IssueResearchResult] = []
    retrieval_queries = [(query.text, query.query_type) for query in target_trace.queries]
    query_types.extend(query_type for _text, query_type in retrieval_queries)
    if multi_agent:
        assert issue_plan is not None

        def research_issue(issue):
            return run_evidence_researcher(
                issue=issue,
                queries=target_trace.queries,
                facts=facts,
                chunks_by_id=chunks_by_id,
                keyword_retriever=keyword_retriever,
                vector_retriever=vector_retriever,
                candidate_top_k=candidate_top_k,
            )

        issue_research_results = [research_issue(issue) for issue in issue_plan.issues]
        merged_keyword_all = merge_hits_by_chunk_id(
            [result.keyword_hits for result in issue_research_results],
            top_k=candidate_top_k,
        )
        merged_vector_all = merge_hits_by_chunk_id(
            [result.vector_hits for result in issue_research_results],
            top_k=candidate_top_k,
        )
        merged_keyword = merged_keyword_all
        merged_vector = merged_vector_all
        issue_hits_by_issue = {
            result.issue_id: result.evidence_hits for result in issue_research_results
        }
    else:
        keyword_hits_per_query = keyword_retriever.search_many(
            retrieval_queries, top_k=candidate_top_k
        )
        vector_hits_per_query = vector_retriever.search_many(
            retrieval_queries, top_k=candidate_top_k
        )
        merged_keyword_all = merge_hits_by_chunk_id(keyword_hits_per_query, top_k=candidate_top_k)
        merged_vector_all = merge_hits_by_chunk_id(vector_hits_per_query, top_k=candidate_top_k)
        initial_query_count = len(initial_queries)
        merged_keyword = merge_hits_by_chunk_id(
            keyword_hits_per_query[:initial_query_count], top_k=candidate_top_k
        )
        merged_vector = merge_hits_by_chunk_id(
            vector_hits_per_query[:initial_query_count], top_k=candidate_top_k
        )
        issue_hits_by_issue = {}

    # Apply metadata boosts to both component results
    boosted_keyword = apply_boosts_to_hits(merged_keyword, chunks_by_id, facts)
    boosted_vector = apply_boosts_to_hits(merged_vector, chunks_by_id, facts)
    boosted_keyword_all = apply_boosts_to_hits(merged_keyword_all, chunks_by_id, facts)
    boosted_vector_all = apply_boosts_to_hits(merged_vector_all, chunks_by_id, facts)

    # RRF produces a broad chunk-level candidate list; source-aware fusion
    # then collapses repeated chunks from the same legal source into a
    # source-diverse final evidence list.
    hybrid_candidates = rrf_fuse(boosted_keyword, boosted_vector, top_k=candidate_top_k)
    hybrid_hits = source_aware_fuse(
        hybrid_candidates,
        top_k=source_fusion_top_k,
        chunks_by_id=chunks_by_id,
    )
    if issue_plan is not None:
        hybrid_hits = select_issue_aware_hits(
            issue_plan,
            issue_hits_by_issue,
            hybrid_hits,
            top_k=source_fusion_top_k,
        )
    rerank_outcome = rerank_hits(
        hybrid_hits,
        question=case.question,
        material_text=case.material.material_text,
        facts=facts,
        queries=target_trace.queries,
        top_k=top_k,
        mode=rerank_mode,
        config=rerank_config,
    )
    hybrid_hits = rerank_outcome.hits
    rerank_info: dict[str, object] = {"initial": rerank_outcome.info}

    # Expand neighbors for top hits
    neighbor_hits = expand_neighbors(hybrid_hits[:5], chunks_by_id, max_neighbors=max_neighbors)

    # Build boost summary for trace
    boosts_summary = compute_boosts_summary(facts, query_types)

    # Issue 7: Evidence self-check
    if needs_llm_self_check(
        question=case.question,
        material_text=case.material.material_text,
        facts=facts,
    ):
        self_check = run_self_check_with_deepseek(
            hybrid_hits,
            facts,
            chunks_by_id,
            question=case.question,
            material_text=case.material.material_text,
        )
        self_check = validate_llm_self_check(self_check, hybrid_hits, facts, chunks_by_id)
    else:
        self_check = run_self_check(hybrid_hits, facts, chunks_by_id)
    second_retrieval_info: dict[str, object] = {}
    final_evidence: list[RetrievalHit] = hybrid_hits
    source_evidence_packets = build_source_evidence_packets(
        representative_hits=final_evidence,
        candidate_hits=hybrid_candidates,
        neighbor_hits=neighbor_hits,
        chunks_by_id=chunks_by_id,
    )
    active_candidates = rrf_fuse_many(
        [hybrid_candidates, *issue_hits_by_issue.values()],
        top_k=candidate_top_k,
    )
    active_neighbor_hits = neighbor_hits

    if self_check.status == "needs_second_retrieval" and self_check.second_retrieval_plan:
        plan = self_check.second_retrieval_plan
        expanded_top_k = max(top_k, plan.increased_top_k)
        expanded_candidate_top_k = max(candidate_top_k, expanded_top_k)
        expanded_source_fusion_top_k = (
            max(expanded_top_k, rerank_config.window) if rerank_mode != "off" else expanded_top_k
        )

        # Run second retrieval with expanded queries
        all_queries = [
            (query.text, query.query_type)
            for query in list(target_trace.queries) + plan.expanded_queries
        ]
        kw2_per_query = keyword_retriever.search_many(all_queries, top_k=expanded_candidate_top_k)
        vec2_per_query = vector_retriever.search_many(all_queries, top_k=expanded_candidate_top_k)

        global_indexes = list(range(len(initial_queries))) + list(
            range(len(target_trace.queries), len(all_queries))
        )
        merged_kw2_all = merge_hits_by_chunk_id(kw2_per_query, top_k=expanded_candidate_top_k)
        merged_vec2_all = merge_hits_by_chunk_id(vec2_per_query, top_k=expanded_candidate_top_k)
        merged_kw2 = merge_hits_by_chunk_id(
            [kw2_per_query[index] for index in global_indexes],
            top_k=expanded_candidate_top_k,
        )
        merged_vec2 = merge_hits_by_chunk_id(
            [vec2_per_query[index] for index in global_indexes],
            top_k=expanded_candidate_top_k,
        )

        # Apply stronger boosts on second retrieval
        boosted_kw2 = apply_boosts_to_hits(merged_kw2, chunks_by_id, facts)
        boosted_vec2 = apply_boosts_to_hits(merged_vec2, chunks_by_id, facts)
        boosted_kw2_all = apply_boosts_to_hits(merged_kw2_all, chunks_by_id, facts)
        boosted_vec2_all = apply_boosts_to_hits(merged_vec2_all, chunks_by_id, facts)

        hybrid2_candidates = rrf_fuse(
            boosted_kw2,
            boosted_vec2,
            top_k=expanded_candidate_top_k,
        )
        hybrid2_hits = source_aware_fuse(
            hybrid2_candidates,
            top_k=expanded_source_fusion_top_k,
            chunks_by_id=chunks_by_id,
        )
        if issue_plan is not None:
            issue_hits_by_issue = _build_issue_hit_pools(
                issue_plan=issue_plan,
                queries=list(target_trace.queries) + plan.expanded_queries,
                keyword_hits_per_query=kw2_per_query,
                vector_hits_per_query=vec2_per_query,
                chunks_by_id=chunks_by_id,
                facts=facts,
                top_k=expanded_candidate_top_k,
            )
            hybrid2_hits = select_issue_aware_hits(
                issue_plan,
                issue_hits_by_issue,
                hybrid2_hits,
                top_k=expanded_source_fusion_top_k,
            )
        rerank2_outcome = rerank_hits(
            hybrid2_hits,
            question=case.question,
            material_text=case.material.material_text,
            facts=facts,
            queries=list(target_trace.queries) + plan.expanded_queries,
            top_k=expanded_top_k,
            mode=rerank_mode,
            config=rerank_config,
        )
        hybrid2_hits = rerank2_outcome.hits
        rerank_info["second_retrieval"] = rerank2_outcome.info
        neighbor2_hits = expand_neighbors(
            hybrid2_hits[:5], chunks_by_id, max_neighbors=max_neighbors
        )

        # Re-evaluate after second retrieval (never triggers another).
        # Always use the rule-based evaluator to guarantee termination:
        # critical_facts_missing are treated as soft warnings and the status
        # is forced to sufficient or insufficient, never needs_second_retrieval.
        self_check = evaluate_after_second_retrieval(
            hybrid2_hits, facts, chunks_by_id, self_check.issues
        )
        self_check = self_check.model_copy(update={"second_retrieval_triggered": True})

        # Use second retrieval results as final evidence
        final_evidence = hybrid2_hits
        source_evidence_packets = build_source_evidence_packets(
            representative_hits=final_evidence,
            candidate_hits=hybrid2_candidates,
            neighbor_hits=neighbor2_hits,
            chunks_by_id=chunks_by_id,
        )
        active_candidates = rrf_fuse_many(
            [hybrid2_candidates, *issue_hits_by_issue.values()],
            top_k=candidate_top_k,
        )
        active_neighbor_hits = neighbor2_hits
        second_retrieval_info = {
            "triggered": True,
            "expanded_queries": [q.model_dump() for q in plan.expanded_queries],
            "increased_top_k": expanded_top_k,
            "stronger_boost": plan.stronger_boost,
            "reason": plan.reason,
            "hybrid_results_count": len(hybrid2_hits),
            "neighbor_chunks_count": len(neighbor2_hits),
        }

        # Update trace with second retrieval results
        updated_trace = target_trace.model_copy(
            update={
                "keyword_results": boosted_kw2_all,
                "vector_results": boosted_vec2_all,
                "hybrid_results": hybrid2_hits,
                "candidate_results": active_candidates,
                "neighbor_chunks": neighbor2_hits,
                "metadata_boosts": boosts_summary,
                "rerank": rerank_info,
                "evidence_self_check": self_check,
                "second_retrieval": second_retrieval_info,
                "final_evidence": final_evidence,
                "source_evidence_packets": source_evidence_packets,
            }
        )
    else:
        updated_trace = target_trace.model_copy(
            update={
                "keyword_results": boosted_keyword_all,
                "vector_results": boosted_vector_all,
                "hybrid_results": hybrid_hits,
                "candidate_results": active_candidates,
                "neighbor_chunks": neighbor_hits,
                "metadata_boosts": boosts_summary,
                "rerank": rerank_info,
                "evidence_self_check": self_check,
                "second_retrieval": second_retrieval_info,
                "final_evidence": final_evidence,
                "source_evidence_packets": source_evidence_packets,
            }
        )

    retrieval_latency_ms = int((time.perf_counter() - retrieval_started) * 1000)

    # Issue 8: Build governed review result
    result_evidence = flatten_source_evidence_packets(source_evidence_packets)
    evidence_dossiers = (
        build_evidence_dossiers(
            issue_plan,
            result_evidence,
            issue_hits_by_issue=issue_hits_by_issue,
            research_results=issue_research_results,
        )
        if issue_plan is not None
        else []
    )
    if multi_agent:
        agent_steps.extend(
            AgentStep(
                agent_name="evidence_researcher",
                status="completed",
                decision=(
                    f"{result.issue_id}: executed {len(result.executed_queries)} queries, "
                    f"returned {len(result.evidence_hits)} issue evidence hits"
                ),
                latency_ms=retrieval_latency_ms,
                llm_calls=0,
            )
            for result in issue_research_results
        )

    reviewer_started = time.perf_counter()
    reviewer_calls_before = current_telemetry().llm_call_count
    review_result = build_review_result_with_deepseek(
        review_result_id=case.latest_result_id or make_id("result"),
        review_case_id=case_id,
        trace_id=target_trace.trace_id,
        facts=facts,
        self_check=self_check,
        evidence_hits=result_evidence,
        chunks_by_id=chunks_by_id,
        question=case.question,
        material_text=case.material.material_text,
        retrieval_queries=updated_trace.queries,
        second_retrieval=updated_trace.second_retrieval,
        source_evidence_packets=source_evidence_packets,
        issue_plan=issue_plan if multi_agent else None,
        evidence_dossiers=evidence_dossiers if multi_agent else None,
        output_format=output_format,
    )
    if multi_agent:
        agent_steps.append(
            AgentStep(
                agent_name="compliance_reviewer",
                status="completed",
                latency_ms=int((time.perf_counter() - reviewer_started) * 1000),
                llm_calls=current_telemetry().llm_call_count - reviewer_calls_before,
            )
        )

    critique_decision = None
    if multi_agent and issue_plan is not None:
        if should_run_evidence_critic(review_result, self_check, issue_plan):
            critic_started = time.perf_counter()
            critic_calls_before = current_telemetry().llm_call_count
            critique_decision = run_evidence_critic(
                result=review_result,
                issue_plan=issue_plan,
                dossiers=evidence_dossiers,
                evidence_hits=result_evidence,
            )
            agent_steps.append(
                AgentStep(
                    agent_name="evidence_critic",
                    status="completed",
                    decision=critique_decision.decision,
                    latency_ms=int((time.perf_counter() - critic_started) * 1000),
                    llm_calls=current_telemetry().llm_call_count - critic_calls_before,
                )
            )
            targeted_requests = critique_decision.targeted_retrieval_requests
            targeted_hits_by_issue: dict[str, list[RetrievalHit]] = {}
            if critique_decision.decision == "research_required":
                targeted_started = time.perf_counter()
                next_query_number = len(updated_trace.queries) + 1
                targeted_queries = [
                    RetrievalQuery(
                        query_id=f"q_{next_query_number + index}",
                        query_type=request.query_type,
                        text=request.query.strip(),
                    )
                    for index, request in enumerate(targeted_requests)
                    if request.query.strip()
                ]
                targeted_queries_by_issue: dict[str, list[str]] = {}
                for request, query in zip(targeted_requests, targeted_queries, strict=True):
                    targeted_queries_by_issue.setdefault(request.issue_id, []).append(
                        query.query_id
                    )
                issue_by_id = {issue.issue_id: issue for issue in issue_plan.issues}
                research_inputs = [
                    issue_by_id[issue_id].model_copy(update={"query_ids": query_ids})
                    for issue_id, query_ids in targeted_queries_by_issue.items()
                ]
                all_research_queries = list(updated_trace.queries) + targeted_queries

                def research_targeted_issue(issue):
                    return run_evidence_researcher(
                        issue=issue,
                        queries=all_research_queries,
                        facts=facts,
                        chunks_by_id=chunks_by_id,
                        keyword_retriever=keyword_retriever,
                        vector_retriever=vector_retriever,
                        candidate_top_k=candidate_top_k,
                    )

                targeted_results = [research_targeted_issue(issue) for issue in research_inputs]
                results_by_issue = {result.issue_id: result for result in issue_research_results}
                for targeted_result in targeted_results:
                    prior_result = results_by_issue[targeted_result.issue_id]
                    combined_candidates = rrf_fuse_many(
                        [
                            prior_result.candidate_hits,
                            targeted_result.candidate_hits,
                        ],
                        top_k=candidate_top_k,
                    )
                    combined_result = IssueResearchResult(
                        issue_id=targeted_result.issue_id,
                        executed_queries=[
                            *prior_result.executed_queries,
                            *targeted_result.executed_queries,
                        ],
                        keyword_hits=merge_hits_by_chunk_id(
                            [
                                prior_result.keyword_hits,
                                targeted_result.keyword_hits,
                            ],
                            top_k=candidate_top_k,
                        ),
                        vector_hits=merge_hits_by_chunk_id(
                            [
                                prior_result.vector_hits,
                                targeted_result.vector_hits,
                            ],
                            top_k=candidate_top_k,
                        ),
                        candidate_hits=combined_candidates,
                        evidence_hits=source_aware_fuse(
                            combined_candidates,
                            top_k=candidate_top_k,
                            chunks_by_id=chunks_by_id,
                        ),
                    )
                    results_by_issue[combined_result.issue_id] = combined_result
                    targeted_hits_by_issue[combined_result.issue_id] = targeted_result.evidence_hits
                issue_research_results = [
                    results_by_issue[issue.issue_id] for issue in issue_plan.issues
                ]
                issue_hits_by_issue = {
                    result.issue_id: result.evidence_hits for result in issue_research_results
                }

                final_evidence = select_issue_aware_hits(
                    issue_plan,
                    issue_hits_by_issue,
                    final_evidence,
                    top_k=max(top_k, len(final_evidence)),
                )
                targeted_neighbors = expand_neighbors(
                    final_evidence[:5], chunks_by_id, max_neighbors=max_neighbors
                )
                active_candidates = rrf_fuse_many(
                    [
                        active_candidates,
                        *(result.candidate_hits for result in targeted_results),
                        *(result.candidate_hits for result in issue_research_results),
                    ],
                    top_k=candidate_top_k,
                )
                active_neighbor_hits = list(active_neighbor_hits) + targeted_neighbors
                source_evidence_packets = build_source_evidence_packets(
                    representative_hits=final_evidence,
                    candidate_hits=active_candidates,
                    neighbor_hits=active_neighbor_hits,
                    chunks_by_id=chunks_by_id,
                )
                result_evidence = flatten_source_evidence_packets(source_evidence_packets)
                evidence_dossiers = build_evidence_dossiers(
                    issue_plan,
                    result_evidence,
                    issue_hits_by_issue=issue_hits_by_issue,
                    research_results=issue_research_results,
                )
                targeted_info = {
                    "triggered": True,
                    "requests": [request.model_dump() for request in targeted_requests],
                    "queries": [query.model_dump() for query in targeted_queries],
                    "result_count": len(final_evidence),
                }
                updated_trace = updated_trace.model_copy(
                    update={
                        "queries": list(updated_trace.queries) + targeted_queries,
                        "hybrid_results": final_evidence,
                        "candidate_results": active_candidates,
                        "neighbor_chunks": targeted_neighbors,
                        "final_evidence": final_evidence,
                        "source_evidence_packets": source_evidence_packets,
                        "second_retrieval": {
                            **updated_trace.second_retrieval,
                            "critic_targeted_retrieval": targeted_info,
                        },
                    }
                )
                agent_steps.extend(
                    AgentStep(
                        agent_name="evidence_researcher",
                        status="completed",
                        decision=(
                            f"{result.issue_id}: executed {len(result.executed_queries)} "
                            "critic-requested queries"
                        ),
                        latency_ms=int((time.perf_counter() - targeted_started) * 1000),
                    )
                    for result in targeted_results
                )
            if critique_decision.decision in {
                "research_required",
                "revision_required",
            }:
                revision_actions = gate_revision_actions(
                    critique_decision,
                    issue_hits_by_issue=issue_hits_by_issue,
                    targeted_hits_by_issue=targeted_hits_by_issue,
                )
                revision_started = time.perf_counter()
                revision_calls_before = current_telemetry().llm_call_count
                try:
                    review_result = revise_review_result_with_deepseek(
                        result=review_result,
                        actions=revision_actions,
                        evidence_hits=result_evidence,
                        chunks_by_id=chunks_by_id,
                        issue_plan=issue_plan,
                        evidence_dossiers=evidence_dossiers,
                    )
                except ReviewWorkflowFailed as exc:
                    agent_steps.append(
                        AgentStep(
                            agent_name="compliance_reviewer",
                            status="failed",
                            decision=f"revision failed: {exc.reason}: {exc.message}",
                            latency_ms=int((time.perf_counter() - revision_started) * 1000),
                            llm_calls=current_telemetry().llm_call_count - revision_calls_before,
                        )
                    )
                    failed_trace = updated_trace.model_copy(
                        update={
                            "issue_plan": issue_plan,
                            "issue_research_results": issue_research_results,
                            "evidence_dossiers": evidence_dossiers,
                            "critique_decision": critique_decision,
                            "agent_steps": agent_steps,
                            "llm_call_count": current_telemetry().llm_call_count,
                            "retry_count": current_telemetry().retry_count,
                        }
                    )
                    write_retrieval_traces(
                        retrieval_traces_path(output_dir),
                        [
                            failed_trace if trace.trace_id == target_trace.trace_id else trace
                            for trace in traces
                        ],
                    )
                    raise
                else:
                    agent_steps.append(
                        AgentStep(
                            agent_name="compliance_reviewer",
                            status="completed",
                            decision="revised once after evidence critique",
                            latency_ms=int((time.perf_counter() - revision_started) * 1000),
                            llm_calls=current_telemetry().llm_call_count - revision_calls_before,
                        )
                    )
        else:
            agent_steps.append(
                AgentStep(
                    agent_name="evidence_critic",
                    status="skipped",
                    decision="simple low-risk case",
                )
            )

    # Persist updated result
    results_path = review_results_path(output_dir)
    if results_path.exists():
        existing_results = read_review_results(results_path)
        updated_results = [
            review_result if r.review_case_id == case_id else r for r in existing_results
        ]
    else:
        updated_results = [review_result]
    write_review_results(results_path, updated_results)

    workflow_latency_ms = int((time.perf_counter() - total_started) * 1000)
    total_latency_ms = (target_trace.total_latency_ms or 0) + workflow_latency_ms
    telemetry = current_telemetry()
    final_trace = updated_trace.model_copy(
        update={
            "latency_ms": total_latency_ms,
            "total_latency_ms": total_latency_ms,
            "retrieval_latency_ms": retrieval_latency_ms,
            "llm_call_count": telemetry.llm_call_count,
            "retry_count": telemetry.retry_count,
            "issue_plan": issue_plan,
            "issue_research_results": issue_research_results,
            "evidence_dossiers": evidence_dossiers,
            "critique_decision": critique_decision,
            "agent_steps": agent_steps,
        }
    )

    # Rewrite traces file after result generation so telemetry includes the
    # full workflow, including final LLM result generation when enabled.
    final_traces = [final_trace if t.trace_id == target_trace.trace_id else t for t in traces]
    write_retrieval_traces(retrieval_traces_path(output_dir), final_traces)

    return final_trace


# ---------------------------------------------------------------------------
# Service mode: real Elasticsearch + pgvector hybrid retrieval
# ---------------------------------------------------------------------------


def run_service_retrieval(
    *,
    case_id: str,
    chunks_path: Path | str = DEFAULT_CHUNKS_PATH,
    output_dir: Path = DEFAULT_REVIEW_RUNS_DIR,
    top_k: int = DEFAULT_TOP_K,
    max_neighbors: int = DEFAULT_NEIGHBOR_COUNT,
    review_mode: ReviewMode = "llm",
    rerank_mode: RerankMode = "off",
    config: object | None = None,
    adapters: object | None = None,
    output_format: ResultFormat = "plain",
    case_analyst: CaseAnalystRunner | None = None,
) -> RetrievalTrace:
    """Run hybrid retrieval backed by real Elasticsearch + pgvector.

    Builds the service adapters from ``ServiceConfig`` (or accepts pre-built
    ``adapters``), then delegates to ``run_hybrid_retrieval`` with both routes
    injected. The corpus is still loaded locally for metadata boosts, neighbor
    expansion, and the governed result builder — only the two retrieval routes
    are served by ES and pgvector.

    Fail-fast: ``require_service_adapters`` inside ``build_service_adapters``
    ensures both routes exist; there is no fallback to local retrieval.
    """

    from law_agent.config import require_service_config
    from law_agent.review.retrieval.service_backends import (
        ServiceAdapters,
        build_service_adapters,
    )

    own_adapters = False
    if adapters is None:
        service_config = config or require_service_config()
        adapters = build_service_adapters(service_config)
        own_adapters = True

    assert isinstance(adapters, ServiceAdapters)
    try:
        return run_hybrid_retrieval(
            case_id=case_id,
            chunks_path=chunks_path,
            output_dir=output_dir,
            top_k=top_k,
            max_neighbors=max_neighbors,
            review_mode=review_mode,
            rerank_mode=rerank_mode,
            keyword_retriever=adapters.keyword,
            vector_retriever=adapters.vector,
            output_format=output_format,
            case_analyst=case_analyst,
        )
    finally:
        if own_adapters:
            adapters.close()
