"""Lazy PyMilvus adapter for dense+sparse hybrid search.

Importing this module does not require a running Milvus instance.  The SDK is
loaded only when a client must be constructed or a search request is executed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import math
import re
from typing import Any, Iterable

from .contracts import (
    HybridEmbedding,
    RetrievalCandidate,
    RetrievalMetadata,
    RetrievalQuery,
    SourceDocument,
)


COLLECTION_SCHEMA_VERSION = "controlled-retrieval.v1"
OUTPUT_FIELDS = [
    "record_id",
    "object_id",
    "object_type",
    "system_id",
    "site",
    "scope",
    "status",
    "version",
    "content_hash",
    "source_ref",
    "approved",
    "sanitized",
]
COLLECTION_FIELDS = frozenset(
    OUTPUT_FIELDS
    + ["schema_version", "content", "dense_vector", "sparse_vector"]
)


def _quoted(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _in(field: str, values: Iterable[str]) -> str:
    return f"{field} in [{','.join(_quoted(item) for item in values)}]"


def build_filter(query: RetrievalQuery) -> str:
    clauses = [
        f"object_type == {_quoted(query.object_type)}",
        f"system_id == {_quoted(query.system_id)}",
        _in("scope", query.scopes),
        _in("status", query.statuses),
        "approved == true",
    ]
    if query.site:
        clauses.append(f"site == {_quoted(query.site)}")
    if query.versions:
        clauses.append(
            "version in [" + ",".join(str(value) for value in query.versions) + "]"
        )
    if query.content_hashes:
        clauses.append(_in("content_hash", query.content_hashes))
    if query.object_type == "repair_memory":
        clauses.append("sanitized == true")
    return " and ".join(clauses)


@dataclass(frozen=True)
class MilvusConfig:
    uri: str = "http://localhost:19530"
    token: str = ""
    database: str = "default"
    collection: str = "test_conductor_knowledge_v1"
    dense_weight: float = 0.65
    sparse_weight: float = 0.35


class MilvusHybridBackend:
    def __init__(self, config: MilvusConfig, *, client: Any | None = None) -> None:
        self.config = config
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:
                raise RuntimeError(
                    "Milvus retrieval is enabled but pymilvus is not installed"
                ) from exc
            kwargs: dict[str, Any] = {
                "uri": self.config.uri,
                "db_name": self.config.database,
            }
            if self.config.token:
                kwargs["token"] = self.config.token
            self._client = MilvusClient(**kwargs)
        return self._client

    def hybrid_search(
        self,
        query: RetrievalQuery,
        embedding: HybridEmbedding,
    ) -> list[RetrievalCandidate]:
        try:
            from pymilvus import AnnSearchRequest, WeightedRanker
        except ImportError as exc:
            raise RuntimeError(
                "Milvus retrieval is enabled but pymilvus is not installed"
            ) from exc
        expression = build_filter(query)
        request_limit = min(100, max(query.limit * 3, query.limit))
        requests = [
            AnnSearchRequest(
                data=[list(embedding.dense)],
                anns_field="dense_vector",
                param={"metric_type": "COSINE"},
                limit=request_limit,
                expr=expression,
            ),
            AnnSearchRequest(
                data=[dict(embedding.sparse)],
                anns_field="sparse_vector",
                param={"metric_type": "IP"},
                limit=request_limit,
                expr=expression,
            ),
        ]
        result = self.client.hybrid_search(
            collection_name=self.config.collection,
            reqs=requests,
            ranker=WeightedRanker(
                self.config.dense_weight,
                self.config.sparse_weight,
            ),
            limit=query.limit,
            output_fields=OUTPUT_FIELDS,
        )
        hits = result[0] if result else []
        return [self._candidate(hit) for hit in hits]

    @staticmethod
    def _candidate(hit: Any) -> RetrievalCandidate:
        entity = getattr(hit, "entity", None)
        if entity is None and isinstance(hit, dict):
            entity = hit.get("entity") or hit
        if not isinstance(entity, dict):
            entity = {field: entity.get(field) for field in OUTPUT_FIELDS}
        score = getattr(hit, "score", None)
        if score is None:
            score = getattr(hit, "distance", None)
        if score is None and isinstance(hit, dict):
            score = hit.get("score", hit.get("distance", 0.0))
        return RetrievalCandidate(
            metadata=RetrievalMetadata.model_validate(
                {field: entity.get(field) for field in OUTPUT_FIELDS}
            ),
            score=float(score or 0.0),
        )

    def upsert(
        self,
        documents: list[SourceDocument],
        embeddings: list[HybridEmbedding],
        *,
        chunk_texts: list[str] | None = None,
    ) -> None:
        if len(documents) != len(embeddings):
            raise ValueError("documents and embeddings must have the same length")
        texts = chunk_texts or [item.content for item in documents]
        if len(texts) != len(documents):
            raise ValueError("chunk_texts and documents must have the same length")
        rows: list[dict[str, Any]] = []
        for document, embedding, text in zip(documents, embeddings, texts):
            if len(text.encode("utf-8")) > 60_000:
                raise ValueError("Milvus index chunks must be at most 60000 UTF-8 bytes")
            row = document.metadata.model_dump(mode="json")
            row.update(
                {
                    "content": text,
                    "schema_version": COLLECTION_SCHEMA_VERSION,
                    "dense_vector": list(embedding.dense),
                    "sparse_vector": dict(embedding.sparse),
                }
            )
            rows.append(row)
        if rows:
            self.client.upsert(collection_name=self.config.collection, data=rows)


class BGEM3EmbeddingProvider:
    """Lazy dense+sparse embeddings using the PyMilvus BGE-M3 integration."""

    def __init__(self, *, device: str = "cpu", use_fp16: bool = False) -> None:
        self.device = device
        self.use_fp16 = use_fp16
        self._model: Any | None = None

    @property
    def model(self) -> Any:
        if self._model is None:
            try:
                from pymilvus.model.hybrid import BGEM3EmbeddingFunction
            except ImportError as exc:
                raise RuntimeError(
                    'BGE-M3 requires the optional dependency "pymilvus[model]"'
                ) from exc
            self._model = BGEM3EmbeddingFunction(
                use_fp16=self.use_fp16,
                device=self.device,
            )
        return self._model

    @property
    def dense_dimension(self) -> int:
        return int(self.model.dim["dense"])

    @staticmethod
    def _sparse_row(value: Any, index: int) -> dict[int, float]:
        row = value.getrow(index) if hasattr(value, "getrow") else value[index]
        if isinstance(row, dict):
            return {int(key): float(score) for key, score in row.items() if score}
        if hasattr(row, "tocoo"):
            coordinate = row.tocoo()
            return {
                int(column): float(score)
                for column, score in zip(coordinate.col, coordinate.data)
                if score
            }
        return {
            int(key): float(score)
            for key, score in enumerate(row)
            if float(score) != 0.0
        }

    def embed_documents(self, texts: list[str]) -> list[HybridEmbedding]:
        if not texts:
            return []
        output = self.model(texts)
        dense = output["dense"]
        sparse = output["sparse"]
        return [
            HybridEmbedding(
                dense=tuple(float(value) for value in dense[index]),
                sparse=self._sparse_row(sparse, index),
            )
            for index in range(len(texts))
        ]

    def embed_query(self, text: str) -> HybridEmbedding:
        return self.embed_documents([text])[0]


class HashingEmbeddingProvider:
    """Dependency-free lexical embeddings for local demos and CI."""

    def __init__(self, *, dense_dimension: int = 256) -> None:
        if dense_dimension < 32 or dense_dimension > 4096:
            raise ValueError("hashing dense dimension must be between 32 and 4096")
        self._dense_dimension = dense_dimension

    @property
    def dense_dimension(self) -> int:
        return self._dense_dimension

    @staticmethod
    def _tokens(text: str) -> list[str]:
        value = str(text or "").lower()
        tokens = re.findall(r"[a-z0-9_]+", value)
        for run in re.findall(r"[\u3400-\u9fff]+", value):
            tokens.extend(run if len(run) == 1 else (run[i : i + 2] for i in range(len(run) - 1)))
        return tokens or ["__empty__"]

    def _embed(self, text: str) -> HybridEmbedding:
        dense = [0.0] * self.dense_dimension
        sparse: dict[int, float] = {}
        for token, count in Counter(self._tokens(text)).items():
            digest = sha256(token.encode("utf-8")).digest()
            dense_index = int.from_bytes(digest[:4], "big") % self.dense_dimension
            weight = 1.0 + math.log(float(count))
            dense[dense_index] += (-weight if digest[4] & 1 else weight)
            sparse_index = int.from_bytes(digest[5:9], "big") % 1_000_003
            sparse[sparse_index] = sparse.get(sparse_index, 0.0) + weight
        norm = math.sqrt(sum(value * value for value in dense)) or 1.0
        return HybridEmbedding(
            dense=tuple(value / norm for value in dense),
            sparse=sparse,
        )

    def embed_documents(self, texts: list[str]) -> list[HybridEmbedding]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> HybridEmbedding:
        return self._embed(text)


def build_embedding_provider(name: str, *, device: str = "cpu"):
    provider = str(name or "hashing").strip().lower()
    if provider == "hashing":
        return HashingEmbeddingProvider()
    if provider in {"bge-m3", "bge_m3", "bgem3"}:
        return BGEM3EmbeddingProvider(device=device)
    raise ValueError("embedding provider must be 'hashing' or 'bge-m3'")


def ensure_collection(client: Any, config: MilvusConfig, *, dense_dimension: int) -> None:
    """Create the controlled collection and both ANN indexes when absent."""

    if client.has_collection(collection_name=config.collection):
        description = client.describe_collection(collection_name=config.collection)
        fields = {
            str(item.get("name") or ""): item
            for item in description.get("fields") or []
            if isinstance(item, dict)
        }
        dense = fields.get("dense_vector") or {}
        if (
            set(fields) != COLLECTION_FIELDS
            or int((dense.get("params") or {}).get("dim") or 0) != dense_dimension
            or not bool((fields.get("record_id") or {}).get("is_primary"))
            or not bool((fields.get("system_id") or {}).get("is_partition_key"))
        ):
            raise RuntimeError(
                f"Milvus collection schema mismatch: {config.collection}"
            )
        return
    try:
        from pymilvus import DataType
    except ImportError as exc:
        raise RuntimeError("pymilvus is required to create the collection") from exc
    schema = client.create_schema(enable_dynamic_field=False)
    schema.add_field("record_id", DataType.VARCHAR, is_primary=True, max_length=256)
    schema.add_field(
        "system_id",
        DataType.VARCHAR,
        max_length=128,
        is_partition_key=True,
    )
    for name, length in (
        ("object_id", 256),
        ("object_type", 32),
        ("site", 256),
        ("scope", 256),
        ("status", 32),
        ("content_hash", 71),
        ("source_ref", 512),
        ("schema_version", 64),
    ):
        schema.add_field(name, DataType.VARCHAR, max_length=length)
    schema.add_field("version", DataType.INT64)
    schema.add_field("approved", DataType.BOOL)
    schema.add_field("sanitized", DataType.BOOL)
    schema.add_field("content", DataType.VARCHAR, max_length=65535)
    schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=dense_dimension)
    schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
    indexes = client.prepare_index_params()
    indexes.add_index(
        field_name="dense_vector",
        index_type="AUTOINDEX",
        metric_type="COSINE",
    )
    indexes.add_index(
        field_name="sparse_vector",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="IP",
    )
    client.create_collection(
        collection_name=config.collection,
        schema=schema,
        index_params=indexes,
        num_partitions=64,
    )


__all__ = [
    "COLLECTION_SCHEMA_VERSION",
    "BGEM3EmbeddingProvider",
    "MilvusConfig",
    "MilvusHybridBackend",
    "build_filter",
    "ensure_collection",
]
