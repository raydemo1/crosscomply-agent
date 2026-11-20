"""Deterministic consistency checks of the scenario bank against the corpus.

Run ``python -m law_agent.review.evalset.bank.checks`` after any corpus or
label change. Checks only what a program can verify (sources and articles
exist, citability and validity at the review date, coverage); whether a label
is legally right stays a human review decision.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from law_agent.data.schemas import Chunk
from law_agent.review.evalset.bank.loader import BANK_DIR, load_personas, load_scenarios
from law_agent.review.evalset.bank.schema import Provision, Scenario
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
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


def check_scenario(scenario: Scenario, corpus: CorpusIndex, personas: set[str]) -> list[str]:
    errors: list[str] = []
    where = scenario.scenario_id
    if scenario.persona_id not in personas:
        errors.append(f"{where}: 未知 persona {scenario.persona_id}")
    unknown = set(scenario.intake) - IntakePayload.model_fields.keys()
    if unknown:
        errors.append(f"{where}: intake 含未知字段 {sorted(unknown)}")
    else:
        IntakePayload.model_validate(scenario.intake)
    names = [m.filename for m in scenario.materials]
    if len(set(names)) != len(names):
        errors.append(f"{where}: 材料文件名重复")

    labels = scenario.retrieval
    for group, items in (("must_cite", labels.must_cite), ("must_retrieve", labels.must_retrieve),
                         ("optional", labels.optional), ("must_not_cite", labels.must_not_cite)):
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
    for source_id in scenario.answer_leak_sources:
        if source_id not in corpus.by_source:
            errors.append(f"{where}: answer_leak_sources {source_id} 不存在")
    return errors


def coverage(scenarios: list[Scenario]) -> dict[str, Counter]:
    points: Counter = Counter()
    sources: Counter = Counter()
    for scenario in scenarios:
        points.update(scenario.judgment_points)
        labels = scenario.retrieval
        sources.update({item.source_id for item in [*labels.must_cite, *labels.must_retrieve]})
    return {"judgment_points": points, "sources": sources}


def run_checks(bank_dir: Path = BANK_DIR, chunks_path: Path = DEFAULT_CHUNKS_PATH) -> list[str]:
    personas = set(load_personas(bank_dir))
    scenarios = load_scenarios(bank_dir)
    ids = Counter(s.scenario_id for s in scenarios)
    errors = [f"{key}: scenario_id 重复" for key, count in ids.items() if count > 1]
    corpus = CorpusIndex(load_corpus(chunks_path))
    for scenario in scenarios:
        errors.extend(check_scenario(scenario, corpus, personas))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS_PATH)
    args = parser.parse_args(argv)
    errors = run_checks(chunks_path=args.chunks)
    report = coverage(load_scenarios())
    print(f"scenarios={sum(1 for _ in load_scenarios())}")
    for name, counter in report.items():
        print(f"[{name}] " + ", ".join(f"{key}={value}" for key, value in sorted(counter.items())))
    for error in errors:
        print("ERROR", error)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
