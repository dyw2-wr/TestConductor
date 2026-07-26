"""Evaluate approved-knowledge retrieval against a versioned JSON dataset."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.test_platform.intent.knowledge import ApprovedKnowledgeSourceStore
from apps.test_platform.retrieval import ControlledRetriever, RetrievalQuery
from apps.test_platform.retrieval.evaluation import evaluate_rankings
from apps.test_platform.retrieval.milvus import BGEM3EmbeddingProvider, MilvusConfig, MilvusHybridBackend


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    if dataset.get("schema_version") != "controlled-retrieval-eval.v1":
        raise ValueError("unsupported evaluation dataset")
    sources = ApprovedKnowledgeSourceStore.from_json(args.catalog)
    config = MilvusConfig(
        uri=os.getenv("TEST_PLATFORM_MILVUS_URI", "http://localhost:19530"),
        token=os.getenv("TEST_PLATFORM_MILVUS_TOKEN", ""),
        database=os.getenv("TEST_PLATFORM_MILVUS_DATABASE", "default"),
        collection=os.getenv("TEST_PLATFORM_MILVUS_COLLECTION", "test_conductor_knowledge_v1"),
    )
    retriever = ControlledRetriever(
        backend=MilvusHybridBackend(config),
        embeddings=BGEM3EmbeddingProvider(device=os.getenv("TEST_PLATFORM_EMBEDDING_DEVICE", "cpu")),
        sources=sources,
    )
    rankings = {}
    for case in dataset["cases"]:
        allowed = [
            item
            for item in sources.sources()
            if item.metadata.scope in set(case["scopes"])
        ]
        results = retriever.retrieve(RetrievalQuery(
            text=case["query"],
            object_type="approved_knowledge",
            system_id=sources.system_id,
            scopes=tuple(case["scopes"]),
            statuses=("approved",),
            versions=tuple(sorted({item.metadata.version for item in allowed})),
            content_hashes=tuple(
                sorted({item.metadata.content_hash for item in allowed})
            ),
            limit=int(case.get("limit", 8)),
        ))
        rankings[case["case_id"]] = [item.source.metadata.source_ref for item in results]
    metrics = evaluate_rankings(dataset["cases"], rankings)
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    thresholds = dataset.get("thresholds") or {}
    passed = (
        float(metrics["recall_at_k"]) >= float(thresholds.get("min_recall_at_k", 0))
        and float(metrics["mrr"]) >= float(thresholds.get("min_mrr", 0))
        and int(metrics["forbidden_hits"]) <= int(thresholds.get("max_forbidden_hits", 0))
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
