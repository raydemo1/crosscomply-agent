"""Later provisions that name an earlier document in precedence language.

A newer rule often states its own relation to older ones, e.g. "《数据出境安全评估办法》
等相关规定与本规定不一致的，适用本规定". The older document is not repealed, so version
filtering keeps it current, and an Agent reading it cannot tell that part of it gave
way. This module finds such provisions in the corpus itself, by fixed drafting
words plus the earlier document's title in 《》, so new corpora need no hand-kept
mapping. The text is handed to the Agent as it is; deciding what changed is left
to the Agent.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date

from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.retrieval.temporal import filter_hits_as_of

PRECEDENCE_WORDS = re.compile(r"不一致的|废止|停止执行|修改为")
TITLE_MENTION = re.compile(r"《([^》]{2,60})》")
MAX_TEXT_CHARS = 400


def _title(text: str) -> str:
    return text.replace("（试行）", "").replace("(试行)", "").strip()


def index_later_provisions(chunks: Sequence[Chunk]) -> dict[str, list[str]]:
    """source_id → chunk_ids of later citable provisions naming that source in precedence language."""

    sources_by_title: dict[str, list[Chunk]] = defaultdict(list)
    first: dict[str, Chunk] = {}
    for chunk in chunks:
        if chunk.source_id not in first:
            first[chunk.source_id] = chunk
            sources_by_title[_title(chunk.title)].append(chunk)
    index: dict[str, list[str]] = defaultdict(list)
    for chunk in chunks:
        if not chunk.can_cite_clause or not PRECEDENCE_WORDS.search(chunk.text):
            continue
        for mention in set(TITLE_MENTION.findall(chunk.text)):
            for earlier in sources_by_title.get(_title(mention), []):
                if (earlier.source_id != chunk.source_id and earlier.publish_date and chunk.publish_date
                        and earlier.publish_date < chunk.publish_date
                        and chunk.chunk_id not in index[earlier.source_id]):
                    index[earlier.source_id].append(chunk.chunk_id)
    return dict(index)


def later_provisions_as_of(source_ids: Sequence[str], index: Mapping[str, list[str]],
                           chunks_by_id: Mapping[str, Chunk], *, as_of: date) -> dict[str, list[dict[str, str | None]]]:
    """Per source, the provisions in force at ``as_of``; a rule published after the review date is not shown."""

    wanted: dict[str, list[str]] = defaultdict(list)  # one provision can name several earlier sources
    for source_id in dict.fromkeys(source_ids):
        for chunk_id in index.get(source_id, []):
            wanted[chunk_id].append(source_id)
    if not wanted:
        return {}
    hits = filter_hits_as_of([hit_from_chunk(chunks_by_id[c], rank) for rank, c in enumerate(wanted)],
                             chunks_by_id, as_of=as_of)
    out: dict[str, list[dict[str, str | None]]] = defaultdict(list)
    for hit in hits:
        if not hit.can_cite_clause:
            continue
        provision = {"source_id": hit.source_id, "title": hit.title, "article_no": hit.article_no,
                     "publish_date": hit.publish_date, "effective_date": hit.effective_date,
                     "text": hit.text[:MAX_TEXT_CHARS]}
        for source_id in wanted[hit.chunk_id]:
            out[source_id].append(provision)
    return dict(out)
