"""Internal one-time corpus migration; not a daily user-facing command."""

from __future__ import annotations

import argparse
from pathlib import Path

from law_agent.config import load_service_config, require_service_config
from law_agent.kb.bootstrap import initialize_legacy_corpus
from law_agent.kb.service import processing_signature
from law_agent.review.retrieval.corpus import load_corpus
from law_agent.review.retrieval.service_backends import index_corpus_to_services


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="data/corpus/legal_docs_20260702")
    parser.add_argument("--rebuild-services", action="store_true")
    args = parser.parse_args()
    root = Path(args.corpus)
    config = load_service_config()
    signature = processing_signature(
        embedding_model=config.embedding.model,
        embedding_dimension=config.embedding.dimension,
    )
    result = initialize_legacy_corpus(root, signature=signature)
    print(f"Initialized {result['sources']} sources and {result['chunks']} chunks; moved {result['raw_moves']} raw files.")
    if args.rebuild_services:
        service_config = require_service_config()
        summary = index_corpus_to_services(service_config, load_corpus(root / "chunks.jsonl"))
        print(f"Rebuilt services: ES={summary['elasticsearch_docs']} PG={summary['pgvector_rows']}")
    else:
        print("Run this script once more with --rebuild-services before starting the review API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
