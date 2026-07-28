"""Strict contracts shared by retrieval adapters and trusted source stores.

Vector records are indexes, not sources of truth.  Every selected candidate is
reloaded from its owning catalog and checked against this metadata before use.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


ObjectType = Literal[
    "approved_knowledge",
    "repair_memory",
]


class StrictRetrievalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalMetadata(StrictRetrievalModel):
    record_id: str = Field(min_length=1, max_length=256)
    object_id: str = Field(min_length=1, max_length=256)
    object_type: ObjectType
    system_id: str = Field(min_length=1, max_length=128)
    site: str = Field(default="", max_length=256)
    scope: str = Field(min_length=1, max_length=256)
    status: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1, max_length=512)
    approved: bool = False
    sanitized: bool = False


class RetrievalQuery(StrictRetrievalModel):
    text: str = Field(min_length=1, max_length=32_768)
    object_type: ObjectType
    system_id: str = Field(min_length=1, max_length=128)
    site: str = Field(default="", max_length=256)
    scopes: tuple[str, ...] = Field(min_length=1, max_length=64)
    statuses: tuple[str, ...] = Field(min_length=1, max_length=8)
    versions: tuple[int, ...] = Field(default=(), max_length=64)
    content_hashes: tuple[str, ...] = Field(default=(), max_length=64)
    limit: int = Field(default=8, ge=1, le=100)

    @model_validator(mode="after")
    def validate_scope(self) -> "RetrievalQuery":
        if any(not item.strip() for item in (*self.scopes, *self.statuses)):
            raise ValueError("retrieval scopes/statuses cannot contain blanks")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("retrieval scopes must be unique")
        if any(value < 1 for value in self.versions):
            raise ValueError("retrieval versions must be positive")
        if any(
            not value.startswith("sha256:") or len(value) != 71
            for value in self.content_hashes
        ):
            raise ValueError("retrieval content_hashes must be sha256 identities")
        return self


class HybridEmbedding(StrictRetrievalModel):
    dense: tuple[float, ...] = Field(min_length=1)
    sparse: dict[int, float] = Field(min_length=1)


class RetrievalCandidate(StrictRetrievalModel):
    metadata: RetrievalMetadata
    score: float


class SourceDocument(StrictRetrievalModel):
    metadata: RetrievalMetadata
    content: str = Field(min_length=1, max_length=262_144)


class VerifiedRetrieval(StrictRetrievalModel):
    source: SourceDocument
    score: float


class HybridEmbeddingProvider(Protocol):
    def embed_query(self, text: str) -> HybridEmbedding: ...

    def embed_documents(self, texts: list[str]) -> list[HybridEmbedding]: ...


class RetrievalBackend(Protocol):
    def hybrid_search(
        self,
        query: RetrievalQuery,
        embedding: HybridEmbedding,
    ) -> list[RetrievalCandidate]: ...


class ExactSourceStore(Protocol):
    def load(self, source_ref: str) -> SourceDocument: ...


__all__ = [
    "ExactSourceStore",
    "HybridEmbedding",
    "HybridEmbeddingProvider",
    "ObjectType",
    "RetrievalBackend",
    "RetrievalCandidate",
    "RetrievalMetadata",
    "RetrievalQuery",
    "SourceDocument",
    "VerifiedRetrieval",
]
