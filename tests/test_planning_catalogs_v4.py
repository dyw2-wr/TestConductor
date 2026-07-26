"""PlanningCatalog v4 的安全、hash 和 cleanup binding 门禁。"""

from __future__ import annotations

import hashlib
import json
import unittest

from pydantic import ValidationError

from apps.test_platform.planning.catalogs import (
    ProcedureOperation,
    CleanupAction,
    DataBinding,
    DatabaseOperation,
    PerformanceObservable,
    PerformanceProfile,
    PlanningCatalogSnapshot,
    compute_catalog_content_hash,
)
from tests.test_planning_flow_v4 import _catalog


class PlanningCatalogV4Tests(unittest.TestCase):
    def test_optional_schema_additions_do_not_invalidate_legacy_hash(self):
        payload = _catalog().model_dump(mode="json")
        for profile in payload.get("procedure_profiles", []):
            profile.pop("navigation_profile", None)
            profile.pop("navigation_snapshot_hash", None)
            profile.pop("pages", None)
            profile.pop("relations", None)
            for operation in profile.get("operations", []):
                operation.pop("exit_page_ref", None)
        payload.pop("database_schema", None)
        payload.pop("content_hash", None)
        legacy_hash = "sha256:" + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        payload["content_hash"] = legacy_hash

        snapshot = PlanningCatalogSnapshot.model_validate(payload)

        self.assertEqual(snapshot.content_hash, legacy_hash)

    def test_snapshot_is_canonical_target_scoped_and_v4(self):
        snapshot = _catalog()
        self.assertEqual(snapshot.schema_version, "planning-catalog.v4")
        self.assertRegex(snapshot.content_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            snapshot.content_hash,
            "sha256:bdda8175432dd2cd20f7b64a030e767645ba2b84a754775a5a11d58d452ee0fb",
        )
        self.assertEqual(snapshot.content_hash, compute_catalog_content_hash(snapshot))
        self.assertTrue(snapshot.matches_target("account-web", "staging"))
        self.assertFalse(snapshot.matches_target("account-web", "production"))
        self.assertEqual(
            snapshot.get_cleanup_action("cleanup.account.unlock").required_data_slots,
            ["account_id"],
        )
        self.assertEqual(
            snapshot.get_data_binding("binding.cleanup.account").input_refs,
            {"account_id": "account"},
        )

    def test_hash_mismatch_is_rejected(self):
        payload = _catalog().model_dump(mode="json")
        payload["environment"] = "production"
        with self.assertRaisesRegex(ValidationError, "content_hash does not match"):
            PlanningCatalogSnapshot.model_validate(payload)

    def test_cleanup_slots_require_catalog_binding_and_python_parameter_name(self):
        payload = _catalog().model_dump(mode="json", exclude={"content_hash"})
        payload["data_bindings"] = [
            item
            for item in payload["data_bindings"]
            if item["operation_ref"] != "cleanup.account.unlock"
        ]
        with self.assertRaisesRegex(ValidationError, "have no catalog DataBinding"):
            PlanningCatalogSnapshot.build(**payload)

        with self.assertRaisesRegex(ValidationError, "Python"):
            CleanupAction(
                action_ref="cleanup.invalid",
                description="Invalid slot",
                handler_kind="http_api",
                policy="restore_state",
                always_run=True,
                evidence_required=True,
                required_data_slots=["account.id"],
            )

    def test_cleanup_binding_uses_hook_slots_not_executor_input_prefix(self):
        binding = DataBinding(
            binding_ref="binding.cleanup.valid",
            description="Cleanup hook parameter",
            executor_kind="database",
            operation_ref="cleanup.valid",
            input_refs={"account_id": "account"},
        )
        snapshot = PlanningCatalogSnapshot.build(
            catalog_id="catalog.cleanup.v4",
            system_id="account-web",
            environment="staging",
            available_executors=["database"],
            database_operations=[
                DatabaseOperation(
                    operation_ref="db.health",
                    description="Read health",
                    connection_profile_ref="runtime.db",
                )
            ],
            data_bindings=[binding],
            cleanup_actions=[
                CleanupAction(
                    action_ref="cleanup.valid",
                    description="Restore account",
                    handler_kind="database",
                    policy="restore_state",
                    always_run=True,
                    evidence_required=True,
                    required_data_slots=["account_id"],
                )
            ],
        )
        self.assertEqual(
            snapshot.get_data_binding("binding.cleanup.valid").input_refs,
            {"account_id": "account"},
        )

    def test_regular_executor_bindings_keep_protocol_specific_slot_rules(self):
        with self.assertRaisesRegex(ValidationError, "database input slot"):
            PlanningCatalogSnapshot.build(
                catalog_id="catalog.bad-binding.v4",
                system_id="account-web",
                environment="staging",
                available_executors=["database"],
                database_operations=[
                    DatabaseOperation(
                        operation_ref="db.health",
                        description="Read health",
                        connection_profile_ref="runtime.db",
                        allowed_binding_refs=["binding.bad"],
                    )
                ],
                data_bindings=[
                    DataBinding(
                        binding_ref="binding.bad",
                        description="Bad database slot",
                        executor_kind="database",
                        operation_ref="db.health",
                        input_refs={"account_id": "account"},
                    )
                ],
            )

    def test_catalog_rejects_raw_sql_locator_secret_and_absolute_path(self):
        with self.assertRaisesRegex(ValidationError, "raw SQL"):
            DatabaseOperation(
                operation_ref="db.bad",
                description="SELECT * FROM users",
                connection_profile_ref="runtime.db",
            )
        with self.assertRaisesRegex(ValidationError, "browser locator"):
            ProcedureOperation(
                operation_ref="procedure.bad",
                page_ref="page.login",
                action='locator("#submit").click()',
                state_effect="read_only",
                procedure_id="account.bad",
                procedure_version=1,
                procedure_fingerprint="sha256:" + "1" * 64,
            )
        with self.assertRaisesRegex(ValidationError, "secret value"):
            CleanupAction(
                action_ref="cleanup.bad",
                description="password=actual-secret",
                handler_kind="http_api",
                policy="restore_state",
                always_run=True,
                evidence_required=True,
                required_data_slots=["account_id"],
            )
        with self.assertRaisesRegex(ValidationError, "absolute filesystem path"):
            PerformanceProfile(
                profile_ref="perf.bad",
                description="Bad profile",
                driver_ref=r"C:\\tools\\k6.exe",
                state_effect="read_only",
                max_duration_seconds=60,
                max_virtual_users=10,
                observables=[
                    PerformanceObservable(
                        observable_ref="observable.perf.bad",
                        description="Metric",
                        metric="latency_ms",
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()
