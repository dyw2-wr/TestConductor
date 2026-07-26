"""Security and provenance gates for Milvus-backed controlled retrieval."""

from __future__ import annotations

import unittest

from apps.test_platform.intent.contracts import ApprovedKnowledge
from apps.test_platform.intent.knowledge import (
    ApprovedKnowledgeSourceStore,
    MilvusApprovedKnowledgeResolver,
)
from apps.test_platform.retrieval import (
    ControlledRetriever,
    HybridEmbedding,
    RetrievalCandidate,
    RetrievalMetadata,
    RetrievalQuery,
)
from apps.test_platform.retrieval.milvus import (
    HashingEmbeddingProvider,
    MilvusConfig,
    build_embedding_provider,
    build_filter,
    ensure_collection,
)
from apps.test_platform.retrieval.service import RetrievalIntegrityError
from apps.test_platform.retrieval.evaluation import evaluate_rankings


def approved(scope: str, identity: str, content: str) -> ApprovedKnowledge:
    from apps.test_platform.retrieval.service import content_hash

    return ApprovedKnowledge(
        scope_id=scope,
        knowledge_id=identity,
        version=2,
        approval_id=f"approval-{identity}",
        approved_at="2026-07-23T00:00:00+08:00",
        content=content,
        content_hash=content_hash(content),
    )


class FakeEmbeddings:
    def embed_query(self, text: str) -> HybridEmbedding:
        self.query = text
        return HybridEmbedding(dense=(1.0, 0.0), sparse={1: 1.0})


class FakeBackend:
    def __init__(self, metadata: list[RetrievalMetadata]):
        self.metadata = metadata

    def hybrid_search(self, query, embedding):
        self.query = query
        self.embedding = embedding
        return [
            RetrievalCandidate(metadata=item, score=1.0 - index / 10)
            for index, item in enumerate(self.metadata)
        ]


class ControlledRetrievalTests(unittest.TestCase):
    def test_existing_collection_schema_mismatch_fails_closed(self):
        class Client:
            def has_collection(self, **kwargs):
                return True

            def describe_collection(self, **kwargs):
                return {"fields": [{"name": "record_id", "is_primary": True}]}

        with self.assertRaisesRegex(RuntimeError, "schema mismatch"):
            ensure_collection(Client(), MilvusConfig(collection="wrong"), dense_dimension=4)

    def test_evaluation_metrics_are_reproducible(self):
        metrics = evaluate_rankings(
            [
                {
                    "case_id": "one",
                    "expected_source_refs": ["expected"],
                    "forbidden_source_refs": ["leak"],
                }
            ],
            {"one": ["noise", "expected"]},
        )
        self.assertEqual(metrics["recall_at_k"], 1.0)
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertEqual(metrics["forbidden_hits"], 0)

    def setUp(self):
        self.documents = [
            approved("billing", "billing-rules", "Refunds require an approval."),
            approved("account", "account-rules", "Locked users need manual review."),
        ]
        self.sources = ApprovedKnowledgeSourceStore(
            system_id="account-system",
            documents=self.documents,
        )

    def retriever(self, metadata=None):
        backend = FakeBackend(metadata or [item.metadata for item in self.sources.sources()])
        return ControlledRetriever(
            backend=backend,
            embeddings=FakeEmbeddings(),
            sources=self.sources,
        ), backend

    def test_resolver_preserves_requested_scope_order_after_hybrid_search(self):
        retriever, backend = self.retriever()
        resolver = MilvusApprovedKnowledgeResolver(
            retriever=retriever,
            sources=self.sources,
        )
        resolved = resolver.resolve(
            ["account", "billing"],
            query_text="How should a locked account refund be reviewed?",
        )
        self.assertEqual([item.scope_id for item in resolved], ["account", "billing"])
        self.assertEqual(backend.query.statuses, ("approved",))
        self.assertEqual(backend.query.system_id, "account-system")
        self.assertEqual(set(backend.query.versions), {2})
        self.assertEqual(len(backend.query.content_hashes), 2)

    def test_out_of_scope_candidate_is_rejected_before_source_use(self):
        metadata = self.sources.sources()[0].metadata.model_copy(
            update={"system_id": "other-system"}
        )
        retriever, _ = self.retriever([metadata])
        with self.assertRaisesRegex(RetrievalIntegrityError, "out-of-scope"):
            retriever.retrieve(
                RetrievalQuery(
                    text="refund",
                    object_type="approved_knowledge",
                    system_id="account-system",
                    scopes=("billing",),
                    statuses=("approved",),
                )
            )

    def test_unapproved_status_cannot_be_requested(self):
        retriever, _ = self.retriever()
        with self.assertRaisesRegex(ValueError, "outside policy"):
            retriever.retrieve(
                RetrievalQuery(
                    text="refund",
                    object_type="approved_knowledge",
                    system_id="account-system",
                    scopes=("billing",),
                    statuses=("draft",),
                )
            )

    def test_milvus_filter_contains_all_governance_fields(self):
        expression = build_filter(
            RetrievalQuery(
                text="open dashboard",
                object_type="procedure",
                system_id="portal",
                site="example.test",
                scopes=("portal-reviewed",),
                statuses=("published",),
                versions=(1,),
                content_hashes=("sha256:" + "a" * 64,),
            )
        )
        for field in (
            "object_type",
            "system_id",
            "site",
            "scope",
            "status",
            "version",
            "content_hash",
            "approved",
        ):
            self.assertIn(field, expression)

    def test_hashing_embeddings_are_fast_stable_and_hybrid(self):
        provider = HashingEmbeddingProvider(dense_dimension=64)
        first = provider.embed_query("冷链温度超限后阻止出库")
        second = provider.embed_query("冷链温度超限后阻止出库")
        different = provider.embed_query("查询订单状态")
        self.assertEqual(first, second)
        self.assertEqual(len(first.dense), 64)
        self.assertTrue(first.sparse)
        self.assertNotEqual(first.dense, different.dense)
        self.assertIsInstance(build_embedding_provider("hashing"), HashingEmbeddingProvider)


if __name__ == "__main__":
    unittest.main()
