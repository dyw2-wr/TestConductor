"""Policy enforcement and exact-source verification for retrieval results."""

from __future__ import annotations

import hashlib

from .contracts import (
    ExactSourceStore,
    HybridEmbeddingProvider,
    RetrievalBackend,
    RetrievalMetadata,
    RetrievalQuery,
    VerifiedRetrieval,
)


class RetrievalIntegrityError(RuntimeError):
    """A vector candidate disagrees with its authoritative source."""


_ALLOWED_STATUS = {
    "approved_knowledge": frozenset({"approved"}),
    "repair_memory": frozenset({"approved"}),
}


def content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _identity(metadata: RetrievalMetadata) -> tuple[object, ...]:
    return (
        metadata.record_id,
        metadata.object_id,
        metadata.object_type,
        metadata.system_id,
        metadata.site,
        metadata.scope,
        metadata.status,
        metadata.version,
        metadata.content_hash,
        metadata.source_ref,
        metadata.approved,
        metadata.sanitized,
    )


class ControlledRetriever:
    """Discover candidates, then rehydrate and verify exact catalog sources."""

    def __init__(
        self,
        *,
        backend: RetrievalBackend,
        embeddings: HybridEmbeddingProvider,
        sources: ExactSourceStore,
    ) -> None:
        self.backend = backend
        self.embeddings = embeddings
        self.sources = sources

    def retrieve(self, query: RetrievalQuery) -> list[VerifiedRetrieval]:
        allowed = _ALLOWED_STATUS[query.object_type]
        if not set(query.statuses).issubset(allowed):
            raise ValueError("retrieval query requests a status outside policy")
        embedding = self.embeddings.embed_query(query.text)
        candidates = self.backend.hybrid_search(query, embedding)
        verified: list[VerifiedRetrieval] = []
        seen_sources: set[str] = set()
        for candidate in candidates:
            metadata = candidate.metadata
            if metadata.source_ref in seen_sources:
                continue
            self._validate_candidate_scope(query, metadata)
            source = self.sources.load(metadata.source_ref)
            if _identity(source.metadata) != _identity(metadata):
                raise RetrievalIntegrityError(
                    f"retrieval metadata drift: {metadata.source_ref}"
                )
            if content_hash(source.content) != metadata.content_hash:
                raise RetrievalIntegrityError(
                    f"retrieval content hash drift: {metadata.source_ref}"
                )
            if not metadata.approved:
                raise RetrievalIntegrityError(
                    f"retrieval source is not approved: {metadata.source_ref}"
                )
            if metadata.object_type == "repair_memory" and not metadata.sanitized:
                raise RetrievalIntegrityError(
                    f"repair memory is not sanitized: {metadata.source_ref}"
                )
            seen_sources.add(metadata.source_ref)
            verified.append(VerifiedRetrieval(source=source, score=candidate.score))
            if len(verified) >= query.limit:
                break
        return verified

    @staticmethod
    def _validate_candidate_scope(
        query: RetrievalQuery,
        metadata: RetrievalMetadata,
    ) -> None:
        expected = (
            metadata.object_type == query.object_type
            and metadata.system_id == query.system_id
            and metadata.scope in query.scopes
            and metadata.status in query.statuses
            and (not query.versions or metadata.version in query.versions)
            and (
                not query.content_hashes
                or metadata.content_hash in query.content_hashes
            )
            and (not query.site or metadata.site.casefold() == query.site.casefold())
        )
        if not expected:
            raise RetrievalIntegrityError(
                f"retrieval backend returned out-of-scope candidate: {metadata.record_id}"
            )


__all__ = ["ControlledRetriever", "RetrievalIntegrityError", "content_hash"]
