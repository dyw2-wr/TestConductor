from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3


def procedure_payload(
    *,
    procedure_id: str = "account.login",
    version: int = 1,
    site: str = "account.example.test",
) -> dict:
    identity = {
        "page_id": "login",
        "title": "Login",
        "url_prefix": f"https://{site}/login",
    }
    return {
        "schema_version": "ProcedureV1",
        "procedure_id": procedure_id,
        "version": version,
        "status": "stable",
        "site": site,
        "description": "Login with the supplied account",
        "backend_scope": ["playwright"],
        "parameters": [
            {
                "name": "account",
                "source": "input_data",
                "source_key": "account_id",
                "required": True,
                "secret": False,
            }
        ],
        "precondition": identity,
        "segments": [
            {
                "segment_id": "login",
                "page_identity": identity,
                "items": [
                    {"action": "input", "target": "Account", "value": "${account}"},
                    {"action": "click", "target": "Login"},
                ],
                "completion_checks": [{"type": "visible", "target": "Welcome"}],
                "transition": {"kind": "none", "next_segment_id": ""},
            }
        ],
        "postcondition": {
            "page_identity": identity,
            "checks": [{"type": "visible", "target": "Welcome"}],
        },
        "provenance": {
            "source_kind": "selected_action_plan",
            "action_plan_paths": ["records/login.json"],
            "selected_steps": [0],
            "source_case_sha256s": [hashlib.sha256(b"case").hexdigest()],
            "extracted_at": "2026-01-01T00:00:00+00:00",
            "all_actions_successful": True,
            "temporary_ref_count": 0,
            "ambiguous_resolution_count": 0,
        },
        "validation": {
            "clean_replay_successes": 2,
            "parameter_signatures": [
                hashlib.sha256(b"a").hexdigest(),
                hashlib.sha256(b"b").hexdigest(),
            ],
            "backends": ["playwright"],
            "last_validated_at": "2026-01-01T00:00:00+00:00",
        },
    }


def procedure_fingerprint(payload: dict) -> str:
    core = {
        key: value
        for key, value in payload.items()
        if key not in {"status", "validation"}
    }
    provenance = dict(core["provenance"])
    provenance.pop("extracted_at", None)
    core["provenance"] = provenance
    return hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_asset_database(path: Path, payloads: list[dict] | None = None) -> dict:
    payloads = payloads or [procedure_payload()]
    site = payloads[0]["site"]
    rows = [
        {
            "payload": payload,
            "fingerprint": procedure_fingerprint(payload),
        }
        for payload in payloads
    ]
    rows.sort(key=lambda item: item["payload"]["procedure_id"])
    library_hash = hashlib.sha256(
        json.dumps(
            [
                {
                    "procedure_id": item["payload"]["procedure_id"],
                    "version": item["payload"]["version"],
                    "fingerprint": item["fingerprint"],
                }
                for item in rows
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE asset_library (
                schema_version TEXT NOT NULL,
                library_id TEXT NOT NULL,
                site TEXT NOT NULL,
                library_hash TEXT NOT NULL,
                published_at TEXT NOT NULL
            );
            CREATE TABLE procedures (
                procedure_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                description TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                PRIMARY KEY(procedure_id, version)
            );
            """
        )
        connection.execute(
            "INSERT INTO asset_library VALUES (?, ?, ?, ?, ?)",
            (
                "ProcedureAssetLibraryV1",
                f"site.{site}",
                site,
                library_hash,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.executemany(
            "INSERT INTO procedures VALUES (?, ?, 'published', ?, ?, ?, ?)",
            [
                (
                    item["payload"]["procedure_id"],
                    item["payload"]["version"],
                    item["payload"]["description"],
                    json.dumps(
                        item["payload"]["parameters"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        item["payload"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    item["fingerprint"],
                )
                for item in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "library_id": f"site.{site}",
        "library_hash": library_hash,
        "site": site,
        "rows": rows,
    }
