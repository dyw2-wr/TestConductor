"""已批准知识的只读解析边界。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .contracts import ApprovedKnowledge
from apps.test_platform.retrieval import (
    ControlledRetriever,
    RetrievalMetadata,
    RetrievalQuery,
    SourceDocument,
)


class ApprovedKnowledgeResolver(Protocol):
    """只返回经过平台审批并锁定 hash 的知识文档。"""

    def resolve(
        self,
        scope_ids: list[str],
        *,
        query_text: str = "",
    ) -> list[ApprovedKnowledge]: ...


class InMemoryApprovedKnowledgeResolver:
    """第一版和测试使用的显式目录；不把请求上传内容自动升级为知识。"""

    def __init__(self, documents: list[ApprovedKnowledge] | None = None):
        documents = documents or []
        self._by_scope = {item.scope_id: item for item in documents}
        if len(self._by_scope) != len(documents):
            raise ValueError("approved knowledge scope_id 必须唯一")

    def resolve(
        self,
        scope_ids: list[str],
        *,
        query_text: str = "",
    ) -> list[ApprovedKnowledge]:
        del query_text
        missing = [scope_id for scope_id in scope_ids if scope_id not in self._by_scope]
        if missing:
            raise ValueError(f"知识范围未批准或不存在: {missing}")
        # Re-validate on every read so accidental mutation after approval cannot
        # silently change the content sent to the model.
        return [
            ApprovedKnowledge.model_validate(
                self._by_scope[scope_id].model_dump(mode="json")
            )
            for scope_id in scope_ids
        ]


class ApprovedKnowledgeSourceStore:
    """Exact, hash-valid source catalog retained outside Milvus."""

    def __init__(self, *, system_id: str, documents: list[ApprovedKnowledge]):
        self.system_id = str(system_id or "").strip()
        if not self.system_id:
            raise ValueError("approved knowledge catalog requires system_id")
        self._documents: dict[str, ApprovedKnowledge] = {}
        self._sources: dict[str, SourceDocument] = {}
        scopes: set[str] = set()
        for raw in documents:
            document = ApprovedKnowledge.model_validate(raw.model_dump(mode="json"))
            if document.scope_id in scopes:
                raise ValueError("approved knowledge scope_id must be unique")
            scopes.add(document.scope_id)
            source_ref = (
                f"approved-knowledge://{self.system_id}/"
                f"{document.knowledge_id}/v{document.version}"
            )
            metadata = RetrievalMetadata(
                record_id=f"{self.system_id}:{document.knowledge_id}:v{document.version}",
                object_id=document.knowledge_id,
                object_type="approved_knowledge",
                system_id=self.system_id,
                scope=document.scope_id,
                status="approved",
                version=document.version,
                content_hash=document.content_hash,
                source_ref=source_ref,
                approved=True,
                sanitized=False,
            )
            self._documents[source_ref] = document
            self._sources[source_ref] = SourceDocument(
                metadata=metadata,
                content=document.content,
            )

    @classmethod
    def from_json(cls, path: str | Path) -> "ApprovedKnowledgeSourceStore":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("approved knowledge catalog is unreadable") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "system_id",
            "documents",
        }:
            raise ValueError("approved knowledge catalog shape is invalid")
        if payload["schema_version"] != "approved-knowledge-catalog.v1":
            raise ValueError("approved knowledge catalog version is unsupported")
        documents = payload.get("documents")
        if not isinstance(documents, list):
            raise ValueError("approved knowledge catalog documents must be a list")
        return cls(
            system_id=str(payload.get("system_id") or ""),
            documents=[ApprovedKnowledge.model_validate(item) for item in documents],
        )

    def load(self, source_ref: str) -> SourceDocument:
        try:
            source = self._sources[source_ref]
        except KeyError as exc:
            raise ValueError(f"approved knowledge source not found: {source_ref}") from exc
        return SourceDocument.model_validate(source.model_dump(mode="json"))

    def approved_document(self, source_ref: str) -> ApprovedKnowledge:
        try:
            document = self._documents[source_ref]
        except KeyError as exc:
            raise ValueError(f"approved knowledge source not found: {source_ref}") from exc
        return ApprovedKnowledge.model_validate(document.model_dump(mode="json"))

    def sources(self) -> list[SourceDocument]:
        return [self.load(source_ref) for source_ref in sorted(self._sources)]


class MilvusApprovedKnowledgeResolver:
    """Use Milvus for discovery, then return only exact approved catalog data."""

    def __init__(
        self,
        *,
        retriever: ControlledRetriever,
        sources: ApprovedKnowledgeSourceStore,
    ) -> None:
        self.retriever = retriever
        self.sources = sources

    def resolve(
        self,
        scope_ids: list[str],
        *,
        query_text: str = "",
    ) -> list[ApprovedKnowledge]:
        if not scope_ids:
            return []
        allowed_sources = [
            item
            for item in self.sources.sources()
            if item.metadata.scope in scope_ids
        ]
        unavailable_scopes = [
            scope
            for scope in scope_ids
            if not any(item.metadata.scope == scope for item in allowed_sources)
        ]
        if unavailable_scopes:
            raise ValueError(
                f"知识范围未批准、不可检索或不存在: {unavailable_scopes}"
            )
        results = self.retriever.retrieve(
            RetrievalQuery(
                text=query_text.strip() or " ".join(scope_ids),
                object_type="approved_knowledge",
                system_id=self.sources.system_id,
                scopes=tuple(scope_ids),
                statuses=("approved",),
                versions=tuple(
                    sorted({item.metadata.version for item in allowed_sources})
                ),
                content_hashes=tuple(
                    sorted({item.metadata.content_hash for item in allowed_sources})
                ),
                limit=min(100, max(len(scope_ids) * 4, len(scope_ids))),
            )
        )
        by_scope: dict[str, ApprovedKnowledge] = {}
        for result in results:
            scope = result.source.metadata.scope
            if scope not in by_scope:
                by_scope[scope] = self.sources.approved_document(
                    result.source.metadata.source_ref
                )
        missing = [scope for scope in scope_ids if scope not in by_scope]
        if missing:
            raise ValueError(f"知识范围未批准、不可检索或不存在: {missing}")
        return [by_scope[scope] for scope in scope_ids]


__all__ = [
    "ApprovedKnowledgeResolver",
    "ApprovedKnowledgeSourceStore",
    "InMemoryApprovedKnowledgeResolver",
    "MilvusApprovedKnowledgeResolver",
]
