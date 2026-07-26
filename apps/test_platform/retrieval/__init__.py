"""Controlled, provenance-preserving retrieval for planning inputs."""

from .contracts import (
    HybridEmbedding,
    RetrievalCandidate,
    RetrievalMetadata,
    RetrievalQuery,
    SourceDocument,
    VerifiedRetrieval,
)
from .service import ControlledRetriever, RetrievalIntegrityError

__all__ = [
    "ControlledRetriever",
    "HybridEmbedding",
    "RetrievalCandidate",
    "RetrievalIntegrityError",
    "RetrievalMetadata",
    "RetrievalQuery",
    "SourceDocument",
    "VerifiedRetrieval",
]
