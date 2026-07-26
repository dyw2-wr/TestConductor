"""Index exact approved knowledge without changing its source-of-truth catalog."""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.test_platform.intent.knowledge import ApprovedKnowledgeSourceStore
from apps.test_platform.retrieval.milvus import (
    MilvusConfig,
    MilvusHybridBackend,
    build_embedding_provider,
    ensure_collection,
)


class Command(BaseCommand):
    help = "Synchronize the approved knowledge catalog into controlled Milvus retrieval."

    def add_arguments(self, parser):
        parser.add_argument("--catalog", type=Path)

    def handle(self, *args, **options):
        configured = str(settings.TEST_PLATFORM_APPROVED_KNOWLEDGE_CATALOG or "").strip()
        path = options.get("catalog") or (Path(configured) if configured else None)
        if path is None:
            raise CommandError("请通过 --catalog 或环境变量指定已审批知识目录")
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        sources = ApprovedKnowledgeSourceStore.from_json(path)
        config = MilvusConfig(
            uri=settings.TEST_PLATFORM_MILVUS_URI,
            token=settings.TEST_PLATFORM_MILVUS_TOKEN,
            database=settings.TEST_PLATFORM_MILVUS_DATABASE,
            collection=settings.TEST_PLATFORM_MILVUS_COLLECTION,
            dense_weight=settings.TEST_PLATFORM_MILVUS_DENSE_WEIGHT,
            sparse_weight=settings.TEST_PLATFORM_MILVUS_SPARSE_WEIGHT,
        )
        provider_name = settings.TEST_PLATFORM_EMBEDDING_PROVIDER
        self.stdout.write(f"[1/4] loading embedding provider={provider_name}")
        embeddings = build_embedding_provider(
            provider_name,
            device=settings.TEST_PLATFORM_EMBEDDING_DEVICE,
        )
        backend = MilvusHybridBackend(config)
        self.stdout.write(
            f"[2/4] ensuring collection={config.collection} dimension={embeddings.dense_dimension}"
        )
        ensure_collection(
            backend.client,
            config,
            dense_dimension=embeddings.dense_dimension,
        )
        documents = sources.sources()
        self.stdout.write(f"[3/4] embedding documents={len(documents)}")
        vectors = embeddings.embed_documents([item.content for item in documents])
        self.stdout.write(f"[4/4] upserting documents={len(documents)}")
        backend.upsert(documents, vectors)
        self.stdout.write(
            self.style.SUCCESS(
                f"indexed={len(documents)} collection={config.collection}"
            )
        )
