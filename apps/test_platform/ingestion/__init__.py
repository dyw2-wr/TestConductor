"""Unified frontend/file requirement ingestion before TestDesign v4."""

from .contracts import (
    INGESTION_SCHEMA_VERSION,
    IngestionError,
    IngestionLimits,
    IngestionResult,
    IngestionWarning,
    InputFile,
    SourcePreview,
)
from .service import RequirementIngestor, prepare_request, supported_extensions

__all__ = [
    "INGESTION_SCHEMA_VERSION",
    "IngestionError",
    "IngestionLimits",
    "IngestionResult",
    "IngestionWarning",
    "InputFile",
    "RequirementIngestor",
    "SourcePreview",
    "prepare_request",
    "supported_extensions",
]
