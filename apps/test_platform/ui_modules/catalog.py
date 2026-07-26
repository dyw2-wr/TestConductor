"""Read the portable Procedure asset library published by auto_ui_test."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping
from urllib.parse import quote


ASSET_LIBRARY_SCHEMA_VERSION = "ProcedureAssetLibraryV1"
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class UiModuleCatalogError(RuntimeError):
    """The selected Procedure asset library is missing or inconsistent."""


@dataclass(frozen=True)
class UiModuleDefinition:
    procedure_id: str
    version: int
    site: str
    description: str
    parameters: tuple[dict[str, Any], ...]
    precondition: dict[str, Any]
    postcondition: dict[str, Any]
    fingerprint: str

    @property
    def ref(self) -> str:
        return f"{self.procedure_id}@v{self.version}"

    @property
    def input_parameters(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            item
            for item in self.parameters
            if item.get("required") is True and item.get("source") == "input_data"
        )

    @property
    def required_parameters(self) -> tuple[dict[str, Any], ...]:
        return tuple(item for item in self.parameters if item.get("required") is True)


class UiModuleCatalog:
    def __init__(
        self,
        modules: tuple[UiModuleDefinition, ...],
        *,
        library_id: str,
        library_hash: str,
        site: str,
        payloads: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
    ) -> None:
        self.modules = modules
        self.library_id = library_id
        self.library_hash = library_hash
        self.site = site
        self._payloads = {
            identity: json.loads(json.dumps(payload, ensure_ascii=False))
            for identity, payload in (payloads or {}).items()
        }

    @staticmethod
    def _procedure_fingerprint(payload: Mapping[str, Any]) -> str:
        core = {
            key: value
            for key, value in payload.items()
            if key not in {"status", "validation"}
        }
        provenance = dict(core.get("provenance") or {})
        provenance.pop("extracted_at", None)
        core["provenance"] = provenance
        encoded = json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_asset_database(cls, database: str | Path) -> "UiModuleCatalog":
        path = Path(database).resolve()
        if not path.is_file():
            raise UiModuleCatalogError("沉淀资产库不存在")
        uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            metadata_rows = connection.execute(
                """
                SELECT schema_version, library_id, site, library_hash
                FROM asset_library
                """
            ).fetchall()
            rows = connection.execute(
                """
                SELECT procedure_id, version, status, description,
                       parameters_json, payload_json, fingerprint
                FROM procedures
                ORDER BY procedure_id, version
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise UiModuleCatalogError("沉淀资产库格式无效") from exc
        finally:
            if "connection" in locals():
                connection.close()
        if len(metadata_rows) != 1:
            raise UiModuleCatalogError("沉淀资产库元数据必须有且仅有一条")
        metadata = metadata_rows[0]
        schema_version = str(metadata["schema_version"] or "")
        library_id = str(metadata["library_id"] or "").strip()
        site = str(metadata["site"] or "").strip()
        library_hash = str(metadata["library_hash"] or "").strip().lower()
        if (
            schema_version != ASSET_LIBRARY_SCHEMA_VERSION
            or not library_id
            or not site
            or not _FINGERPRINT.fullmatch(library_hash)
        ):
            raise UiModuleCatalogError("沉淀资产库元数据无效")

        modules: list[UiModuleDefinition] = []
        payloads: dict[tuple[str, int], dict[str, Any]] = {}
        identities: set[tuple[str, int]] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                parameters = json.loads(row["parameters_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise UiModuleCatalogError("沉淀资产内容不是有效 JSON") from exc
            procedure_id = str(row["procedure_id"] or "").strip()
            version = row["version"]
            fingerprint = str(row["fingerprint"] or "").strip().lower()
            identity = (procedure_id, version)
            if (
                not isinstance(payload, Mapping)
                or not isinstance(parameters, list)
                or not procedure_id
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
                or identity in identities
                or row["status"] != "published"
                or payload.get("schema_version") != "ProcedureV1"
                or payload.get("status") != "stable"
                or payload.get("procedure_id") != procedure_id
                or payload.get("version") != version
                or str(payload.get("site") or "").strip() != site
                or payload.get("parameters") != parameters
                or not _FINGERPRINT.fullmatch(fingerprint)
                or cls._procedure_fingerprint(payload) != fingerprint
            ):
                raise UiModuleCatalogError("沉淀资产身份或指纹无效")
            clean_parameters: list[dict[str, Any]] = []
            names: set[str] = set()
            for item in parameters:
                if not isinstance(item, Mapping) or set(item) != {
                    "name",
                    "source",
                    "source_key",
                    "required",
                    "secret",
                }:
                    raise UiModuleCatalogError("沉淀参数定义无效")
                clean = dict(item)
                name = str(clean["name"] or "").strip()
                source = str(clean["source"] or "").strip()
                if (
                    not name
                    or name in names
                    or source not in {"input_data", "profile", "remember", "secret"}
                    or not str(clean["source_key"] or "").strip()
                    or not isinstance(clean["required"], bool)
                    or not isinstance(clean["secret"], bool)
                ):
                    raise UiModuleCatalogError("沉淀参数定义无效")
                names.add(name)
                clean_parameters.append(clean)
            description = str(row["description"] or "").strip()
            if not description or description != str(payload.get("description") or "").strip():
                raise UiModuleCatalogError("沉淀描述无效")
            precondition = payload.get("precondition")
            postcondition = payload.get("postcondition")
            if not isinstance(precondition, Mapping) or not isinstance(postcondition, Mapping):
                raise UiModuleCatalogError("沉淀前后条件无效")
            identities.add(identity)
            payloads[identity] = dict(payload)
            modules.append(
                UiModuleDefinition(
                    procedure_id=procedure_id,
                    version=version,
                    site=site,
                    description=description,
                    parameters=tuple(clean_parameters),
                    precondition=dict(precondition),
                    postcondition=dict(postcondition),
                    fingerprint=fingerprint,
                )
            )
        calculated_hash = hashlib.sha256(
            json.dumps(
                [
                    {
                        "procedure_id": item.procedure_id,
                        "version": item.version,
                        "fingerprint": item.fingerprint,
                    }
                    for item in modules
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if calculated_hash != library_hash:
            raise UiModuleCatalogError("沉淀资产库摘要不一致")
        if not modules:
            raise UiModuleCatalogError("沉淀资产库没有可用 Procedure")
        return cls(
            tuple(modules),
            library_id=library_id,
            library_hash=library_hash,
            site=site,
            payloads=payloads,
        )

    def get(
        self,
        procedure_id: str,
        version: int,
        *,
        fingerprint: str | None = None,
    ) -> UiModuleDefinition:
        matches = [
            item
            for item in self.modules
            if item.procedure_id == procedure_id and item.version == version
        ]
        if len(matches) != 1:
            raise UiModuleCatalogError("沉淀资产不存在")
        result = matches[0]
        if fingerprint is not None and result.fingerprint != fingerprint:
            raise UiModuleCatalogError("沉淀资产指纹不一致")
        return result

    def payload(
        self,
        procedure_id: str,
        version: int,
        *,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        self.get(procedure_id, version, fingerprint=fingerprint)
        payload = self._payloads.get((procedure_id, version))
        if payload is None:
            raise UiModuleCatalogError("沉淀资产内容不可用")
        return json.loads(json.dumps(payload, ensure_ascii=False))


__all__ = [
    "ASSET_LIBRARY_SCHEMA_VERSION",
    "UiModuleCatalog",
    "UiModuleCatalogError",
    "UiModuleDefinition",
]
