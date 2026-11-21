"""Article-level evidence recall, shared by the Q&A and review-report Agents.

Both Agents research with the same tools (search, navigation, article reads);
only how they write the final output differs. So one scorer answers two
questions for any run, against the scenario's retrieval labels:

* retrieved — the labeled provision is among everything the Agent gathered;
* cited — the final output cites it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal

from law_agent.data.schemas import StrictModel
from law_agent.evals.bank.schema import Provision, RetrievalLabels


class EvidenceLabel(StrictModel):
    kind: Literal["must_cite", "must_retrieve"]
    key: str
    retrieved: bool
    cited: bool


class EvidenceScore(StrictModel):
    labels: list[EvidenceLabel]
    excluded_citations: list[str]


def _matches(item: dict[str, Any], provision: Provision) -> bool:
    return item.get("source_id") == provision.source_id and (
        provision.article_no is None or item.get("article_no") == provision.article_no
    )


def score_evidence(labels: RetrievalLabels, *, evidence: Sequence[dict[str, Any]],
                   citations: Sequence[dict[str, Any]]) -> EvidenceScore:
    """``evidence`` and ``citations`` are dicts with ``source_id`` / ``article_no`` (citations may carry
    ``can_cite_clause``); an excluded provision only counts when cited as a clause."""

    return EvidenceScore(
        labels=[EvidenceLabel(kind=kind, key=p.key, retrieved=any(_matches(e, p) for e in evidence),
                              cited=any(_matches(c, p) for c in citations))
                for kind in ("must_cite", "must_retrieve") for p in getattr(labels, kind)],
        excluded_citations=[p.key for p in labels.must_not_cite
                            if any(_matches(c, p) and c.get("can_cite_clause") is not False for c in citations)],
    )


def summarize_evidence(scores: Iterable[EvidenceScore]) -> dict[str, Any]:
    """Pooled (micro) rates: each labeled provision counts once."""

    scores = list(scores)
    out: dict[str, Any] = {"scored_runs": len(scores)}
    for kind in ("must_cite", "must_retrieve"):
        items = [label for score in scores for label in score.labels if label.kind == kind]
        out[f"{kind}_n"] = len(items)
        for level in ("retrieved", "cited"):
            out[f"{kind}_{level}"] = (sum(getattr(i, level) for i in items) / len(items)) if items else None
    out["excluded_citation_runs"] = sum(bool(score.excluded_citations) for score in scores)
    return out
