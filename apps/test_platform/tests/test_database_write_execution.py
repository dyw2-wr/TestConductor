from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.test_platform.admin import ExecutionPlanArtifactAdmin
from apps.test_platform.database_sql import validate_database_sql
from apps.test_platform.planning.adapters.database import DatabaseCompiler
from apps.test_platform.planning.catalogs import PlanningCatalogSnapshot
from apps.test_platform.planning.contracts import (
    BoundAssertion,
    DatabaseExecution,
    DatabaseOperationPlan,
    ExecutionSource,
    ExecutorKind,
    PlanFlow,
    PlanStage,
)
from apps.test_platform.planning.contracts import TestPlanDraft as PlanDraft
from apps.test_platform.runners.contracts import RunResult, RuntimeContext
from apps.test_platform.runners.database import DatabaseRunner


class DatabaseWriteExecutionTests(SimpleTestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        )
        self.connection.execute(
            "INSERT INTO orders (id, status) VALUES (1, 'pending')"
        )
        self.context = RuntimeContext(
            variables={"order_id": 1, "new_status": "paid"},
            database_schemas={
                "test-db": {
                    "tables": [
                        {
                            "name": "orders",
                            "columns": [
                                {"name": "id"},
                                {"name": "status"},
                            ],
                        }
                    ],
                    "allowed_parameter_refs": ["order_id", "new_status"],
                }
            },
        )

    def tearDown(self):
        self.connection.close()

    def test_validator_accepts_bounded_write_and_rejects_policy_mismatch(self):
        sql = "UPDATE orders SET status=:status WHERE id=:order_id"
        kwargs = {
            "allowed_tables": ["orders"],
            "allowed_columns": {"orders": ["id", "status"]},
            "allowed_parameter_refs": ["order_id", "new_status"],
            "parameters_refs": {
                "status": "new_status",
                "order_id": "order_id",
            },
        }
        self.assertEqual(
            validate_database_sql(sql, execution_policy="write", **kwargs),
            sql,
        )
        with self.assertRaisesRegex(ValueError, "策略"):
            validate_database_sql(sql, execution_policy="read_only", **kwargs)

    def test_runner_executes_approved_write_and_checks_affected_rows(self):
        runner = DatabaseRunner()
        statement = runner._validate_query(
            0,
            {
                "statement_id": "DB-0001",
                "source": {"source_kind": "operation", "source_id": "OP-0001"},
                "operation_ref": "ai.sql.stage-0001.0001",
                "execution_policy": "write",
                "risk_level": "high",
                "parameters_refs": {
                    "status": "new_status",
                    "order_id": "order_id",
                },
                "assertions": [
                    {
                        "expected_result_id": "EXP-0001",
                        "after_operation_id": "OP-0001",
                        "kind": "affected_rows",
                        "operator": "equals",
                        "expected": 1,
                    }
                ],
                "sql": "UPDATE orders SET status=:status WHERE id=:order_id",
                "sql_origin": "ai_generated",
                "knowledge_scope_id": None,
            },
            self.context,
            {"EXP-0001"},
            connection_ref="test-db",
            schema_version="database-execution-plan.v6",
        )
        result = RunResult.new(
            run_id="run-write-test",
            executor_kind="database",
            flow_id="FLOW-0001",
            stage_id="STAGE-0001",
        )
        runner._run_query(
            result,
            0,
            statement,
            self.connection,
            self.context,
            1000,
            [False],
        )

        self.assertEqual(result.steps[0].status.value, "passed")
        self.assertEqual(result.steps[0].details["affected_rows"], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM orders WHERE id = 1"
            ).fetchone()[0],
            "paid",
        )

    def test_approval_preview_prioritizes_sql_warning_and_port_call(self):
        self.assertNotIn(
            "execution_artifact_preview",
            ExecutionPlanArtifactAdmin.readonly_fields,
        )
        self.assertEqual(ExecutionPlanArtifactAdmin.audit_payload_specs, ())
        database_stage = {
            "execution": {
                "kind": "database",
                "operations": [
                    {
                        "operation_ref": "ai.sql.stage-0001.0001",
                        "execution_policy": "write",
                        "sql_origin": "ai_generated",
                        "sql": "DELETE FROM orders WHERE id=:order_id",
                        "parameters_refs": {"order_id": "order_id"},
                        "assertions": [],
                    }
                ],
            }
        }
        database_preview = str(
            ExecutionPlanArtifactAdmin._stage_test_content(database_stage)
        )
        database_code = str(
            ExecutionPlanArtifactAdmin._stage_key_code(database_stage)
        )
        self.assertIn("高风险", database_preview)
        self.assertIn("DELETE FROM orders", database_preview)
        self.assertIn("db.execute", database_code)

        port_code = str(
            ExecutionPlanArtifactAdmin._stage_key_code(
                {
                    "execution": {
                        "kind": "tcp_port",
                        "probes": [
                            {
                                "host_ref": "gateway.test",
                                "port": 443,
                                "timeout_seconds": 3,
                                "assertions": [],
                            }
                        ],
                    }
                }
            )
        )
        self.assertIn("tcp_connect", port_code)
        self.assertIn("port=443", port_code)

    def test_compiler_materializes_warning_and_exact_sql(self):
        operation = DatabaseOperationPlan(
            operation_run_id="DB-0001",
            source=ExecutionSource(
                source_kind="operation",
                source_id="OP-0001",
            ),
            operation_ref="ai.sql.stage-0001.0001",
            action="更新订单状态",
            execution_policy="write",
            sql="UPDATE orders SET status=:status WHERE id=:order_id",
            parameters_refs={
                "status": "new_status",
                "order_id": "order_id",
            },
            sql_origin="ai_generated",
            assertions=[
                BoundAssertion(
                    expected_result_id="EXP-0001",
                    after_operation_id="OP-0001",
                    observable_ref="ai.sql.stage-0001.0001.check",
                    kind="affected_rows",
                    statement="应更新一行",
                    operator="equals",
                    expected=1,
                )
            ],
        )
        stage = PlanStage(
            stage_id="STAGE-0001",
            order=1,
            executor_kind=ExecutorKind.DATABASE,
            operation_ids=["OP-0001"],
            expected_result_ids=["EXP-0001"],
            execution=DatabaseExecution(
                connection_profile_ref="test-db",
                operations=[operation],
            ),
        )
        flow = PlanFlow(
            flow_id="FLOW-0001",
            name="数据库写入",
            scenario_id="SCN-0001",
            techniques=["database"],
            requirement_ids=["REQ-0001"],
            stages=[stage],
        )
        digest = "sha256:" + "1" * 64
        plan = PlanDraft(
            plan_id="PLAN-0001",
            version=1,
            design_id="DESIGN-0001",
            design_version=1,
            design_content_hash=digest,
            design_input_content_hash=digest,
            catalog_id="CATALOG-0001",
            catalog_content_hash=digest,
            target_system_id="orders",
            target_environment="test",
            flows=[flow],
        )
        catalog = PlanningCatalogSnapshot.build(
            catalog_id="CATALOG-0001",
            system_id="orders",
            environment="test",
            available_executors=["database"],
            database_schema={
                "connection_profile_ref": "test-db",
                "dialect": "sqlite",
                "tables": [
                    {
                        "name": "orders",
                        "columns": [
                            {"name": "id", "data_type": "integer"},
                            {"name": "status", "data_type": "text"},
                        ],
                    }
                ],
                "allowed_parameter_refs": ["order_id", "new_status"],
            },
        )
        plan = plan.model_copy(
            update={"catalog_content_hash": catalog.content_hash}
        )
        bundle = SimpleNamespace(
            design=SimpleNamespace(
                design_id="DESIGN-0001",
                version=1,
                target=SimpleNamespace(system_id="orders", environment="test"),
            ),
            review=SimpleNamespace(
                design_content_hash=digest,
                input_content_hash=digest,
            ),
        )

        with TemporaryDirectory() as temp_dir:
            artifact = DatabaseCompiler().compile(
                bundle,
                plan,
                flow,
                stage,
                catalog,
                Path(temp_dir),
            )
            stage_root = (
                Path(temp_dir)
                / plan.plan_id
                / "v1"
                / flow.flow_id
                / stage.stage_id
            )
            payload = json.loads(
                (stage_root / "execution.json").read_text(encoding="utf-8")
            )

        self.assertEqual(
            artifact.artifact_schema_version,
            "database-execution-plan.v6",
        )
        self.assertTrue(payload["contains_writes"])
        self.assertEqual(payload["statements"][0]["risk_level"], "high")
        self.assertIn("高风险", payload["warnings"][0])
        self.assertIn("UPDATE orders", payload["statements"][0]["sql"])
        self.assertFalse((stage_root / "review-statements.sql").exists())
