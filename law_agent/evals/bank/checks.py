"""Deterministic consistency checks of the scenario bank.

Checks only what a program can verify: intake fields, sources and articles
exist, citability and validity at the review date, rubric completeness,
review records and the locked test split. Whether a label is legally right
stays a human review decision.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from pydantic import ValidationError

from law_agent.data.schemas import Chunk
from law_agent.evals.bank.schema import Provision, Scenario
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.retrieval.temporal import filter_hits_as_of


class CorpusIndex:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self.by_source: dict[str, list[Chunk]] = defaultdict(list)
        for chunk in chunks:
            self.by_source[chunk.source_id].append(chunk)

    def article(self, provision: Provision) -> list[Chunk]:
        chunks = self.by_source.get(provision.source_id, [])
        if provision.article_no is None:
            return chunks
        return [chunk for chunk in chunks if chunk.article_no == provision.article_no]

    def citable_at(self, chunks: list[Chunk], scenario: Scenario) -> list[Chunk]:
        hits = filter_hits_as_of(
            [hit_from_chunk(chunk, rank) for rank, chunk in enumerate(chunks)],
            self.by_id, as_of=scenario.as_of_date,
        )
        return [self.by_id[hit.chunk_id] for hit in hits if hit.can_cite_clause]


def completeness_errors(scenario: Scenario) -> list[tuple[str, str]]:
    """Field-level gaps that block a scenario from review; no corpus needed."""

    errors: list[tuple[str, str]] = []
    if set(scenario.intake) - IntakePayload.model_fields.keys():
        errors.append(("intake", "申报信息包含未定义字段"))
    else:
        try:
            IntakePayload.model_validate(scenario.intake)
        except ValidationError as exc:
            errors.extend((f"intake.{error['loc'][0]}", "申报值类型或选项无效")
                          for error in exc.errors(include_input=False))
    if "followup_answers" in scenario.intake:
        errors.append(("intake", "评测场景不得包含原案补充回答"))
    rubric = scenario.agent_rubric
    if rubric is not None:
        for field, values in (
            ("selection_reason", [scenario.selection_reason]),
            ("reference_basis", scenario.reference_basis),
            ("rubric.allowed_judgments", rubric.allowed_judgments),
            ("rubric.forbidden_judgments", rubric.forbidden_judgments),
        ):
            if not values or not all(value.strip() for value in values):
                errors.append((field, "请填写此项"))
    names = [material.filename for material in scenario.materials]
    if len(set(names)) != len(names):
        errors.append(("materials", "材料文件名重复"))
    return errors


def corpus_errors(scenario: Scenario, corpus: CorpusIndex) -> list[str]:
    errors: list[str] = []
    where = scenario.scenario_id
    labels = scenario.retrieval
    groups = (
        (("must_cite", labels.must_cite), ("must_retrieve", labels.must_retrieve),
         ("optional", labels.optional), ("must_not_cite", labels.must_not_cite))
        if labels else ()
    )
    for group, items in groups:
        for item in items:
            label = f"{where}: {group} {item.source_id} {item.article_no or '(整篇)'}"
            chunks = corpus.article(item)
            if not chunks:
                errors.append(f"{label} 在语料中不存在")
                continue
            citable = corpus.citable_at(chunks, scenario)
            if group == "must_cite" and not citable:
                errors.append(f"{label} 在 {scenario.as_of_date} 不可作为条款引用")
            if group == "must_retrieve" and citable:
                errors.append(f"{label} 可作为条款引用，应放入 must_cite")
            if group == "must_not_cite" and item.reason == "version_superseded" and citable:
                errors.append(f"{label} 标为已被取代，但在 {scenario.as_of_date} 仍可引用")
    rubric = scenario.agent_rubric
    referenced = [*scenario.answer_leak_sources]
    if rubric:
        referenced += [*rubric.must_cover_sources, *rubric.forbidden_clause_sources]
    if scenario.controlled_web:
        referenced += scenario.controlled_web.held_out_source_ids
    errors.extend(f"{where}: 来源 {source_id} 不在语料中"
                  for source_id in dict.fromkeys(referenced) if source_id not in corpus.by_source)
    return errors


def check_bank(scenarios: list[Scenario], corpus: CorpusIndex | None, personas: set[str]) -> list[str]:
    from law_agent.evals.review import review_errors

    errors = [f"{key}: scenario_id 重复"
              for key, count in Counter(s.scenario_id for s in scenarios).items() if count > 1]
    for scenario in scenarios:
        if scenario.persona_id and scenario.persona_id not in personas:
            errors.append(f"{scenario.scenario_id}: 未知 persona {scenario.persona_id}")
        errors.extend(f"{scenario.scenario_id}: {field} {message}"
                      for field, message in completeness_errors(scenario))
        if corpus is not None:
            errors.extend(corpus_errors(scenario, corpus))
    errors.extend(review_errors(scenarios))
    return errors


def coverage(scenarios: list[Scenario]) -> dict[str, Counter]:
    points: Counter = Counter()
    sources: Counter = Counter()
    splits: Counter = Counter()
    for scenario in scenarios:
        splits[f"{scenario.split}/{scenario.label_status}"] += 1
        points.update(scenario.judgment_points)
        if scenario.retrieval:
            labels = scenario.retrieval
            sources.update({item.source_id for item in [*labels.must_cite, *labels.must_retrieve]})
    return {"splits": splits, "judgment_points": points, "sources": sources}
