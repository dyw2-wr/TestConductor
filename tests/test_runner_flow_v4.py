from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from apps.test_platform.intent.builder import DefaultDesignBuilder
from apps.test_platform.intent.contracts import (
    DesignSelections,
    RequirementInput,
    ReviewDecision,
    TargetSelection,
    TestDesignRequest,
    TestTechnique,
)
from apps.test_platform.intent.knowledge import InMemoryApprovedKnowledgeResolver
from apps.test_platform.intent.prompt_builder import DefaultDesignPromptBuilder
from apps.test_platform.intent.service import TestDesignPipeline
from apps.test_platform.planning.catalogs import (
    ProcedureCapabilityProfile,
    ProcedureOperation,
    CleanupAction,
    DataBinding,
    DatabaseObservable,
    DatabaseOperation,
    HttpOperation,
    LoadStage,
    PerformanceObservable,
    PerformanceProfile,
    PlanningCatalogSnapshot,
)
from apps.test_platform.planning.contracts import (
    ApprovedTestPlanBundle,
    BoundAssertion,
    ProcedureExecution,
    ProcedurePlanRow,
    CleanupDataBinding,
    CleanupDataBindingSelection,
    CleanupSelection,
    DataBindingSelection,
    DatabaseExecution,
    DatabaseOperationPlan,
    ExecutionSource,
    ExecutorArtifactBundle,
    ExecutorArtifactRef,
    HttpExecution,
    HttpRequestPlan,
    ExpectedResultSelection,
    OperationSelection,
    PerformanceExecution,
    PlanCleanup,
    PlanCandidate,
    PlanDataGuaranteeResolution,
    PlanFlow,
    PlanFlowCandidate,
    PlanReview,
    PlanReviewDecision,
    PlanStage,
    PlanStageCandidate,
    PlanStatus,
    PlanValidationReport,
    TestPlanDraft,
    SetupStageResolutionCandidate,
    compute_artifact_set_hash,
    compute_plan_review_content_hash,
    compute_plan_validation_content_hash,
)
from apps.test_platform.planning.artifact_paths import generated_files_root
from apps.test_platform.planning.compiler import TestPlanCompiler
from apps.test_platform.runners import (
    ProcedureRunner,
    DatabaseRunner,
    ExecutionCoordinator,
    HttpRunner,
    PerformanceRunner,
    RunnerRegistry,
)
from apps.test_platform.runners.base import (
    prepare_flow_cleanup,
    run_prepared_flow_cleanup,
)
from apps.test_platform.runners.contracts import (
    CleanupResult,
    RunStatus,
    RunnerError,
    RuntimeContext,
)


_HASH_A = "sha256:" + "1" * 64
_HASH_B = "sha256:" + "2" * 64


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _catalog(kinds: set[str], *, cleanup: bool = False) -> PlanningCatalogSnapshot:
    http_operations = []
    database_operations = []
    performance_profiles = []
    procedure_profiles = []
    data_bindings = []
    cleanup_actions = []
    if "http_api" in kinds:
        http_operations.append(
            HttpOperation(
                operation_ref="http.stage",
                description="Run an HTTP stage",
                base_url_ref="api.main",
                method="GET",
                path="/stage",
                state_effect="read_only",
            )
        )
    if "database" in kinds:
        database_operations.append(
            DatabaseOperation(
                operation_ref="db.account",
                description="Read an account row",
                connection_profile_ref="db.main",
            )
        )
    if "performance" in kinds:
        performance_profiles.append(
            PerformanceProfile(
                profile_ref="perf.smoke",
                description="Small performance profile",
                driver_ref="driver.fake",
                state_effect="read_only",
                max_duration_seconds=60,
                max_virtual_users=10,
                observables=[
                    PerformanceObservable(
                        observable_ref="perf.latency",
                        description="Latency in milliseconds",
                        metric="latency_ms",
                        unit="ms",
                    )
                ],
            )
        )
    if "procedure_playwright" in kinds:
        procedure_profiles.append(
            ProcedureCapabilityProfile(
                profile_ref="procedure.login",
                site="account.example.test",
                library_id="site.account.example.test",
                library_hash="sha256:" + "a" * 64,
                description="Login page capability",
                operations=[
                    ProcedureOperation(
                        operation_ref="procedure.login.open",
                        page_ref="page.login",
                        action="Open login page",
                        state_effect="read_only",
                        procedure_id="account.open-login",
                        procedure_version=1,
                        procedure_fingerprint="sha256:" + "1" * 64,
                    )
                ],
            )
        )
    if cleanup:
        data_bindings.append(
            DataBinding(
                binding_ref="binding.cleanup.account",
                description="Account cleanup input",
                executor_kind="http_api",
                operation_ref="cleanup.account.restore",
                input_refs={"body_ref": "runtime.account_id"},
            )
        )
        cleanup_actions.append(
            CleanupAction(
                action_ref="cleanup.account.restore",
                description="Restore the test account",
                handler_kind="http_api",
                policy="restore_state",
                target="test account",
                always_run=True,
                evidence_required=True,
                required_data_slots=["body_ref"],
            )
        )
    return PlanningCatalogSnapshot.build(
        catalog_id="catalog.runner.v4",
        system_id="system-under-test",
        environment="test",
        available_executors=sorted(kinds),
        http_operations=http_operations,
        database_operations=database_operations,
        performance_profiles=performance_profiles,
        procedure_profiles=procedure_profiles,
        data_bindings=data_bindings,
        cleanup_actions=cleanup_actions,
    )


def _stage(flow_id: str, order: int, kind: str) -> PlanStage:
    stage_id = f"{flow_id}-STAGE-{order:04d}"
    operation_id = f"OP-{order:04d}"
    expected_id = f"EXPECTED-{order:04d}"
    source = ExecutionSource(source_kind="operation", source_id=operation_id)
    if kind == "http_api":
        assertion = BoundAssertion(
            expected_result_id=expected_id,
            after_operation_id=operation_id,
            observable_ref="http.status",
            kind="status",
            statement="The response status is 200",
            operator="equals",
            expected=200,
        )
        execution = HttpExecution(
            base_url_ref="api.main",
            requests=[
                HttpRequestPlan(
                    request_id=f"REQUEST-{order:04d}",
                    source=source,
                    operation_ref="http.stage",
                    action="Run HTTP stage",
                    method="GET",
                    path=f"/stage-{order}",
                    assertions=[assertion],
                )
            ],
        )
    elif kind == "procedure_playwright":
        execution = ProcedureExecution(
            capability_profile_ref="procedure.login",
            capability_site="account.example.test",
            library_id="site.account.example.test",
            library_hash="sha256:" + "a" * 64,
            procedure_refs=["account.open-login@v1"],
            rows=[
                ProcedurePlanRow(
                    row_id=f"ROW-{order:04d}",
                    source=source,
                    operation_ref="procedure.login.open",
                    action="Open login page",
                    procedure_id="account.open-login",
                    procedure_version=1,
                    procedure_fingerprint="sha256:" + "1" * 64,
                )
            ],
        )
    elif kind == "database":
        execution = DatabaseExecution(
            connection_profile_ref="db.main",
            operations=[
                DatabaseOperationPlan(
                    operation_run_id=f"QUERY-{order:04d}",
                    source=source,
                    operation_ref="db.account",
                    action="Read account",
                )
            ],
        )
    elif kind == "performance":
        execution = PerformanceExecution(
            profile_ref="perf.smoke",
            driver_ref="driver.fake",
            sources=[source],
            stages=[LoadStage(duration_seconds=1, virtual_users=1)],
        )
    else:  # pragma: no cover - test helper guard
        raise AssertionError(kind)
    return PlanStage(
        stage_id=stage_id,
        order=order,
        executor_kind=kind,
        operation_ids=[operation_id],
        expected_result_ids=[expected_id] if kind == "http_api" else [],
        execution=execution,
    )


def _payload(stage: PlanStage) -> dict:
    base = {
        "executor_kind": stage.executor_kind.value,
        "flow_id": stage.stage_id.rsplit("-STAGE-", 1)[0],
        "stage_id": stage.stage_id,
    }
    if stage.executor_kind.value == "http_api":
        return {
            **base,
            "schema_version": "http-execution-plan.v4",
            "base_url_ref": "api.main",
            "requests": [
                {
                    "request_id": f"REQUEST-{stage.order:04d}",
                    "method": "GET",
                    "path": f"/stage-{stage.order}",
                    "assertions": [
                        {
                            "expected_result_id": stage.expected_result_ids[0],
                            "kind": "status",
                            "operator": "equals",
                            "expected": 200,
                        }
                    ],
                }
            ],
        }
    if stage.executor_kind.value == "database":
        return {
            **base,
            "schema_version": "database-execution-plan.v4",
            "connection_profile_ref": "db.main",
            "read_only": True,
            "queries": [],
        }
    if stage.executor_kind.value == "performance":
        return {
            **base,
            "schema_version": "performance-execution-plan.v4",
            "driver_ref": "driver.fake",
            "load_profile_ref": "perf.smoke",
            "stages": [{"duration_seconds": 1, "virtual_users": 1}],
            "input_refs": {},
            "thresholds": [],
        }
    return {**base, "schema_version": "procedure-stage-bundle.v4"}


def _write_artifact(
    directory: Path,
    *,
    executor_kind: str,
    artifact_schema_version: str,
    flow_id: str,
    stage_id: str,
    payload: dict,
    plan_id: str = "PLAN-RUNNER-V4",
    plan_version: int = 1,
    plan_content_hash: str = _HASH_A,
    design_id: str = "DESIGN-RUNNER-V4",
    design_version: int = 1,
    design_content_hash: str = _HASH_A,
    design_input_content_hash: str = _HASH_B,
    catalog_id: str = "catalog.runner.v4",
    catalog_content_hash: str = _HASH_B,
) -> ExecutorArtifactBundle:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": artifact_schema_version,
        "executor_kind": executor_kind,
        "flow_id": flow_id,
        "stage_id": stage_id,
        "design_id": design_id,
        "design_version": design_version,
        "plan_id": plan_id,
        "plan_version": plan_version,
        **payload,
    }
    for index, request in enumerate(payload.get("requests", []), start=1):
        request.setdefault(
            "source",
            {"source_kind": "operation", "source_id": f"OP-{index:04d}"},
        )
        request.setdefault("operation_ref", f"operation.{index:04d}")
        request.setdefault("body_ref", None)
        request.setdefault("headers_ref", None)
        request.setdefault("query", {})
    for index, query in enumerate(payload.get("queries", []), start=1):
        if not any(key in query for key in ("sql", "query", "statement")):
            query.setdefault(
                "source",
                {"source_kind": "operation", "source_id": f"OP-DB-{index:04d}"},
            )
            query.setdefault("operation_ref", f"operation.db.{index:04d}")
    if executor_kind == "performance":
        payload.setdefault(
            "sources",
            [{"source_kind": "operation", "source_id": "OP-PERFORMANCE-0001"}],
        )
        for threshold in payload.get("thresholds", []):
            threshold.setdefault("after_operation_id", "OP-PERFORMANCE-0001")
            threshold.setdefault("unit", None)
            threshold.setdefault("percentile", None)
    if executor_kind == "procedure_playwright":
        import pandas as pd

        payload_path = directory / "case.xlsx"
        pd.DataFrame(
            [
                {
                    "Test Case ID": flow_id,
                    "Test Case Name": "Runner flow",
                    "Test Step ID": "ROW-0001",
                    "UR": "REQ-0001",
                    "Action": "Open login page",
                    "Input Data": "",
                    "Check": "",
                }
            ]
        ).to_excel(payload_path, sheet_name="Case", index=False)
        payload_kind = "workbook"
    else:
        payload_path = directory / "execution.json"
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        payload_kind = "payload"
    payload_hash = _sha256(payload_path)
    artifact_id = f"ARTIFACT-{flow_id}-{stage_id}"
    expected_results = [
        str(assertion["expected_result_id"])
        for collection in (
            payload.get("requests", []),
            payload.get("queries", []),
        )
        for item in collection
        for assertion in item.get("assertions", [])
        if assertion.get("expected_result_id")
    ]
    expected_results.extend(
        str(item["expected_result_id"])
        for item in payload.get("thresholds", [])
        if item.get("expected_result_id")
    )
    manifest = {
        "schema_version": "executor-artifact-manifest.v4",
        "artifact_schema_version": artifact_schema_version,
        "artifact_id": artifact_id,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "plan_content_hash": plan_content_hash,
        "design_id": design_id,
        "design_version": design_version,
        "design_content_hash": design_content_hash,
        "design_input_content_hash": design_input_content_hash,
        "catalog_id": catalog_id,
        "catalog_content_hash": catalog_content_hash,
        "executor_kind": executor_kind,
        "flow_id": flow_id,
        "stage_id": stage_id,
        "compiled_artifact_hashes": {payload_path.name: payload_hash},
        "traceability": {"expected_results": expected_results},
    }
    if executor_kind == "procedure_playwright":
        manifest.update(
            {
                "payload_format": "xlsx",
                "workbook_schema": "WorkbookV2",
                "entry_point_ref": "entry.login",
                "variable_refs": [],
            }
        )
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ExecutorArtifactBundle(
        artifact_id=artifact_id,
        artifact_schema_version=artifact_schema_version,
        executor_kind=executor_kind,
        flow_id=flow_id,
        stage_id=stage_id,
        design_id=design_id,
        design_version=design_version,
        design_content_hash=design_content_hash,
        design_input_content_hash=design_input_content_hash,
        plan_id=plan_id,
        plan_version=plan_version,
        plan_content_hash=plan_content_hash,
        catalog_id=catalog_id,
        catalog_content_hash=catalog_content_hash,
        manifest_path_ref="manifest.json",
        artifact_refs=[
            ExecutorArtifactRef(
                kind=payload_kind,
                path_ref=payload_path.name,
                sha256=payload_hash,
            ),
            ExecutorArtifactRef(
                kind="manifest",
                path_ref="manifest.json",
                sha256=_sha256(manifest_path),
            ),
        ],
    )


def _approved_bundle(
    root: Path,
    kinds: list[str],
    *,
    cleanup: bool = False,
    data_guarantee: bool = False,
) -> ApprovedTestPlanBundle:
    catalog = _catalog(set(kinds), cleanup=cleanup)
    plan_id = "PLAN-RUNNER-V4"
    flow_id = f"{plan_id}-FLOW-0001"
    stages = [_stage(flow_id, index, kind) for index, kind in enumerate(kinds, start=1)]
    plan_cleanup = None
    if cleanup:
        plan_cleanup = PlanCleanup(
            cleanup_goal_id="CLEANUP-0001",
            action_ref="cleanup.account.restore",
            handler_kind="http_api",
            policy="restore_state",
            target="test account",
            always_run=True,
            evidence_required=True,
            data_bindings=[
                CleanupDataBinding(
                    slot="body_ref",
                    data_id="DATA-ACCOUNT",
                    binding_ref="binding.cleanup.account",
                    variable_ref="runtime.account_id",
                )
            ],
        )
    plan = TestPlanDraft(
        plan_id=plan_id,
        version=1,
        status=PlanStatus.APPROVED,
        design_id="DESIGN-RUNNER-V4",
        design_version=1,
        design_content_hash=_HASH_A,
        design_input_content_hash=_HASH_B,
        catalog_id=catalog.catalog_id,
        catalog_content_hash=catalog.content_hash,
        target_system_id="system-under-test",
        target_environment="test",
        flows=[
            PlanFlow(
                flow_id=flow_id,
                name="Runner flow",
                scenario_id="SCENARIO-0001",
                techniques=[TestTechnique.POSITIVE],
                requirement_ids=["REQ-0001"],
                stages=stages,
                required_state_resolutions=(
                    [
                        PlanDataGuaranteeResolution(
                            required_state_id="REQUIRED-STATE-0001",
                            text="fixture account is ready",
                            data_id="DATA-ACCOUNT",
                        )
                    ]
                    if data_guarantee
                    else []
                ),
                cleanup=plan_cleanup,
            )
        ],
    )
    plan_hash = plan.content_hash()
    artifacts = []
    schema_by_kind = {
        "http_api": "http-execution-plan.v4",
        "database": "database-execution-plan.v4",
        "performance": "performance-execution-plan.v4",
        "procedure_playwright": "procedure-stage-bundle.v4",
    }
    for stage in stages:
        artifacts.append(
            _write_artifact(
                generated_files_root(root, stage.executor_kind)
                / plan.plan_id
                / "v1"
                / flow_id
                / stage.stage_id,
                executor_kind=stage.executor_kind.value,
                artifact_schema_version=schema_by_kind[stage.executor_kind.value],
                flow_id=flow_id,
                stage_id=stage.stage_id,
                payload=_payload(stage),
                plan_content_hash=plan_hash,
                catalog_content_hash=catalog.content_hash,
            )
        )
    validation_payload = {
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "plan_content_hash": plan_hash,
        "passed": True,
        "findings": [],
    }
    validation_payload["validation_content_hash"] = (
        compute_plan_validation_content_hash(validation_payload)
    )
    validation = PlanValidationReport(**validation_payload)
    review_payload = {
        "review_id": "REVIEW-RUNNER-V4",
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "decision": PlanReviewDecision.APPROVED,
        "comments": "Approved for runner tests",
        "reviewed_at": "2026-07-19T00:00:00Z",
        "plan_content_hash": plan_hash,
        "validation_content_hash": validation.validation_content_hash,
        "artifact_set_hash": compute_artifact_set_hash(artifacts),
    }
    review_payload["review_content_hash"] = compute_plan_review_content_hash(
        review_payload
    )
    review = PlanReview(**review_payload)
    return ApprovedTestPlanBundle(
        plan=plan,
        validation=validation,
        review=review,
        catalog_snapshot=catalog,
        compiled_artifacts=artifacts,
    )


class _SequenceTransport:
    def __init__(self, statuses: list[int]):
        self.statuses = list(statuses)
        self.calls: list[str] = []

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        status = self.statuses.pop(0)
        return {"status_code": status, "headers": {}, "json": {"ok": status == 200}}


class _RecordingDesignGateway:
    def __init__(self, payload: dict):
        self.payload = payload

    def generate(self, messages, output_schema):
        return self.payload


def _approved_e2e_design():
    request = TestDesignRequest(
        request_id="REQ-E2E-LOCK",
        requirements=[
            RequirementInput(
                requirement_id="REQ-LOCK",
                content=(
                    "Ensure the account starts unlocked, lock it through the API, "
                    "verify the database state, then restore it."
                ),
            )
        ],
        target=TargetSelection(system_id="account-service", environment="test"),
        selections=DesignSelections(
            techniques=["positive"],
            allowed_channels=["api", "database"],
            knowledge_scope_ids=[],
        ),
    )
    candidate = {
        "title": "Account lock state",
        "objective": {"text": "Verify account locking and restoration"},
        "in_scope": [{"text": "API mutation and database verification"}],
        "out_of_scope": [],
        "scenarios": [
            {
                "title": "Lock one account",
                "techniques": ["positive"],
                "requirement_ids": ["REQ-LOCK"],
                "required_states": [{"text": "The account is unlocked"}],
                "operations": [
                    {"text": "Lock the account", "channel_hint": "api"}
                ],
                "expected_results": [
                    {
                        "text": "The database locked flag is 1",
                        "after_operation_index": 1,
                        "channel_hint": "database",
                        "operator": "equals",
                        "expected": 1,
                    }
                ],
                "data_requirements": [
                    {"text": "A restorable account", "constraints": []}
                ],
                "state_impact": {
                    "impact": "changes_state",
                    "rationale": {"text": "The lock endpoint changes account state"},
                    "cleanup_goal": {
                        "text": "Restore the account to unlocked",
                        "subject_data_indexes": [1],
                    },
                },
            }
        ],
        "open_questions": [],
    }
    pipeline = TestDesignPipeline(
        DefaultDesignBuilder(
            DefaultDesignPromptBuilder(),
            _RecordingDesignGateway(candidate),
        ),
        knowledge_resolver=InMemoryApprovedKnowledgeResolver([]),
    )
    generated = pipeline.generate(request)
    approved, review = pipeline.review(
        generated,
        decision=ReviewDecision.APPROVED,
        comments="Design approved for runner E2E",
    )
    return pipeline.build_approved_bundle(generated, approved, review)


def _e2e_catalog() -> PlanningCatalogSnapshot:
    return PlanningCatalogSnapshot.build(
        catalog_id="catalog.runner.e2e.v4",
        system_id="account-service",
        environment="test",
        available_executors=["http_api", "database"],
        http_operations=[
            HttpOperation(
                operation_ref="http.account.ensure-unlocked",
                description="Ensure the fixture starts unlocked",
                base_url_ref="api.main",
                method="POST",
                path="/setup/unlock",
                state_effect="changes_state",
            ),
            HttpOperation(
                operation_ref="http.account.lock",
                description="Lock one account",
                base_url_ref="api.main",
                method="POST",
                path="/accounts/{account}/lock",
                state_effect="changes_state",
                allowed_binding_refs=["binding.http.account"],
            ),
        ],
        database_operations=[
            DatabaseOperation(
                operation_ref="db.account.lock-state",
                description="Read the account lock flag",
                connection_profile_ref="db.main",
                allowed_binding_refs=["binding.db.account"],
                observables=[
                    DatabaseObservable(
                        observable_ref="observable.db.locked",
                        description="Locked column",
                        kind="column",
                        column="locked",
                    )
                ],
            )
        ],
        data_bindings=[
            DataBinding(
                binding_ref="binding.http.account",
                description="Account path parameter",
                executor_kind="http_api",
                operation_ref="http.account.lock",
                input_refs={"path.account": "account"},
            ),
            DataBinding(
                binding_ref="binding.db.account",
                description="Account query parameter",
                executor_kind="database",
                operation_ref="db.account.lock-state",
                input_refs={"param.account": "account"},
            ),
            DataBinding(
                binding_ref="binding.cleanup.account",
                description="Account cleanup parameter",
                executor_kind="database",
                operation_ref="cleanup.account.restore",
                input_refs={"account_id": "account"},
            ),
        ],
        cleanup_actions=[
            CleanupAction(
                action_ref="cleanup.account.restore",
                description="Restore the account lock flag",
                handler_kind="database",
                policy="restore_state",
                target="account lock state",
                always_run=True,
                evidence_required=True,
                required_data_slots=["account_id"],
            )
        ],
    )


def _compile_e2e_handoff(root: Path) -> ApprovedTestPlanBundle:
    design = _approved_e2e_design()
    scenario = design.design.scenarios[0]
    operation_id = scenario.operations[0].operation_id
    expected_id = scenario.expected_results[0].expected_result_id
    data_id = scenario.data_requirements[0].data_id
    required_state_id = scenario.required_states[0].required_state_id
    cleanup_goal_id = scenario.state_impact.cleanup_goal.cleanup_goal_id
    candidate = PlanCandidate(
        flows=[
            PlanFlowCandidate(
                scenario_id=scenario.scenario_id,
                stages=[
                    PlanStageCandidate(executor_kind="http_api"),
                    PlanStageCandidate(
                        executor_kind="http_api",
                        operations=[
                            OperationSelection(
                                operation_id=operation_id,
                                catalog_ref="http.account.lock",
                            )
                        ],
                        data_bindings=[
                            DataBindingSelection(
                                data_id=data_id,
                                consumer_id=operation_id,
                                binding_ref="binding.http.account",
                            )
                        ],
                    ),
                    PlanStageCandidate(
                        executor_kind="database",
                        expected_results=[
                            ExpectedResultSelection(
                                expected_result_id=expected_id,
                                catalog_ref="db.account.lock-state",
                                observable_ref="observable.db.locked",
                            )
                        ],
                        data_bindings=[
                            DataBindingSelection(
                                data_id=data_id,
                                consumer_id=expected_id,
                                binding_ref="binding.db.account",
                            )
                        ],
                    ),
                ],
                required_state_resolutions=[
                    SetupStageResolutionCandidate(
                        required_state_id=required_state_id,
                        stage_index=1,
                        catalog_ref="http.account.ensure-unlocked",
                    )
                ],
                cleanup=CleanupSelection(
                    cleanup_goal_id=cleanup_goal_id,
                    action_ref="cleanup.account.restore",
                    data_bindings=[
                        CleanupDataBindingSelection(
                            slot="account_id",
                            data_id=data_id,
                            binding_ref="binding.cleanup.account",
                        )
                    ],
                ),
            )
        ]
    )
    catalog = _e2e_catalog()
    compiler = TestPlanCompiler()
    plan = compiler.build_draft(
        design,
        candidate,
        catalog,
        plan_id="PLAN-RUNNER-E2E-V4",
    )
    compiled = compiler.compile(design, plan, catalog, root)
    if not compiled.validation.passed:  # pragma: no cover - helper failure detail
        raise AssertionError(compiled.validation.findings)
    approved, review = compiler.review(
        compiled,
        decision=PlanReviewDecision.APPROVED,
        comments="Plan and compiled artifacts approved",
    )
    return compiler.build_approved_bundle(compiled, approved, review, catalog)


class _SqliteStateTransport:
    def __init__(self, database_path: Path, *, fail_first: bool = False):
        self.database_path = database_path
        self.fail_first = fail_first
        self.calls: list[str] = []

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("setup transport failure")
        connection = sqlite3.connect(self.database_path)
        try:
            if url.endswith("/setup/unlock"):
                connection.execute("UPDATE accounts SET locked = 0")
            elif url.endswith("/accounts/ACCOUNT-1/lock"):
                connection.execute(
                    "UPDATE accounts SET locked = 1 WHERE account = 'ACCOUNT-1'"
                )
            else:  # pragma: no cover - helper guard
                raise AssertionError(url)
            connection.commit()
        finally:
            connection.close()
        return {"status_code": 200, "headers": {}, "json": {"ok": True}}
def _cleanup(*, variable_ref: str = "runtime.account_id"):
    return SimpleNamespace(
        cleanup_goal_id="CLEANUP-1",
        action_ref="cleanup.account.restore",
        handler_kind="http_api",
        always_run=True,
        evidence_required=True,
        data_bindings=[
            SimpleNamespace(
                slot="account_id",
                data_id="DATA-ACCOUNT",
                binding_ref="binding.cleanup.account",
                variable_ref=variable_ref,
            )
        ],
    )


class RunnerFlowV4Tests(unittest.TestCase):
    def test_flow_cleanup_resolves_variable_ref_and_passes_explicit_parameters(self):
        calls: list[dict] = []

        def cleanup_hook(*, account_id):
            calls.append({"account_id": account_id})
            return CleanupResult(success=True, details={"restored": True})

        with tempfile.TemporaryDirectory() as output:
            context = RuntimeContext(
                variables={
                    "runtime": {"account_id": "A-1"},
                    # data_id is traceability only and must not be used as the value.
                    "DATA-ACCOUNT": "WRONG-VALUE",
                },
                cleanup_hooks={"cleanup.account.restore": cleanup_hook},
                evidence_dir=Path(output),
            )
            prepared = prepare_flow_cleanup(_cleanup(), context)
            self.assertIsNotNone(prepared)
            step, evidence, errors = run_prepared_flow_cleanup(
                prepared,
                context,
                run_id="run-flow-1",
            )

        self.assertEqual(calls, [{"account_id": "A-1"}])
        self.assertEqual(step.status, RunStatus.PASSED)
        self.assertTrue(evidence)
        self.assertEqual(errors, [])

    def test_flow_cleanup_blocks_when_variable_ref_is_missing(self):
        context = RuntimeContext(
            variables={"DATA-ACCOUNT": "must-not-be-used"},
            cleanup_hooks={
                "cleanup.account.restore": lambda **parameters: CleanupResult(success=True)
            },
            evidence_dir=Path("unused"),
        )

        with self.assertRaisesRegex(RunnerError, "cleanup variable_ref"):
            prepare_flow_cleanup(_cleanup(variable_ref="runtime.missing"), context)

    def test_flow_cleanup_rejects_zero_parameter_hook_contract(self):
        cleanup = _cleanup()
        cleanup.data_bindings = []
        context = RuntimeContext(
            cleanup_hooks={
                "cleanup.account.restore": lambda **kwargs: CleanupResult(success=True)
            },
            evidence_dir=Path("unused"),
        )

        with self.assertRaisesRegex(RunnerError, "禁止零参数 cleanup hook"):
            prepare_flow_cleanup(cleanup, context)

    def test_http_stages_run_sequentially_and_manifest_binds_artifact_set(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            bundle = _approved_bundle(root, ["http_api", "http_api"])
            transport = _SequenceTransport([200, 200])
            summary = ExecutionCoordinator(
                RunnerRegistry(http=HttpRunner(transport=transport))
            ).execute(
                bundle,
                root,
                RuntimeContext(
                    base_urls={"api.main": "https://example.test"},
                    evidence_dir=root / "evidence",
                ),
                run_id="RUN-SEQUENTIAL-V4",
            )

            manifest = json.loads(
                (root / "evidence" / summary.manifest_path).read_text(encoding="utf-8")
            )

        self.assertEqual(summary.status, RunStatus.PASSED)
        self.assertEqual(
            transport.calls,
            ["https://example.test/stage-1", "https://example.test/stage-2"],
        )
        self.assertEqual([item.status for item in summary.stages], [RunStatus.PASSED] * 2)
        self.assertEqual(
            [item.stage_id for item in summary.stages],
            [item.stage_id for item in bundle.plan.flows[0].stages],
        )
        self.assertEqual(manifest["schema_version"], "run-manifest.v4")
        self.assertEqual(
            manifest["validation_content_hash"],
            bundle.validation.validation_content_hash,
        )
        self.assertEqual(manifest["review_content_hash"], bundle.review.review_content_hash)
        self.assertEqual(manifest["artifact_set_hash"], bundle.review.artifact_set_hash)
        self.assertTrue(all("artifact_set_hash" not in item for item in manifest["artifacts"]))
        self.assertTrue(all("case_id" not in item for item in manifest["stages"]))

    def test_http_runner_rejects_non_v4_alias_and_missing_request_id(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            missing_id_payload = {
                "schema_version": "http-execution-plan.v4",
                "executor_kind": "http_api",
                "flow_id": "FLOW-HTTP-MISSING-ID",
                "stage_id": "STAGE-HTTP-MISSING-ID",
                "base_url_ref": "api.main",
                "requests": [{"method": "GET", "path": "/health", "assertions": []}],
            }
            missing_id_artifact = _write_artifact(
                root / "http-missing-id",
                executor_kind="http_api",
                artifact_schema_version="http-execution-plan.v4",
                flow_id="FLOW-HTTP-MISSING-ID",
                stage_id="STAGE-HTTP-MISSING-ID",
                payload=missing_id_payload,
            )
            alias_payload = {
                "schema_version": "http-execution-plan.v4",
                "executor_kind": "http_api",
                "flow_id": "FLOW-HTTP-ALIAS",
                "stage_id": "STAGE-HTTP-ALIAS",
                "base_url_ref": "api.main",
                "requests": [
                    {
                        "request_id": "REQUEST-ALIAS",
                        "method": "GET",
                        "path": "/health",
                        "assertions": [
                            {
                                "expected_result_id": "EXPECTED-ALIAS",
                                "kind": "status_code",
                                "operator": "equals",
                                "expected": 200,
                            }
                        ],
                    }
                ],
            }
            alias_artifact = _write_artifact(
                root / "http-alias",
                executor_kind="http_api",
                artifact_schema_version="http-execution-plan.v4",
                flow_id="FLOW-HTTP-ALIAS",
                stage_id="STAGE-HTTP-ALIAS",
                payload=alias_payload,
            )
            transport = _SequenceTransport([200])
            runner = HttpRunner(transport=transport)
            context = RuntimeContext(base_urls={"api.main": "https://example.test"})
            missing_id = runner.run(
                root / "http-missing-id", missing_id_artifact, context
            )
            alias = runner.run(root / "http-alias", alias_artifact, context)

        self.assertEqual(missing_id.status, RunStatus.BLOCKED)
        self.assertEqual(alias.status, RunStatus.BLOCKED)
        self.assertTrue(any("request_id" in item for item in missing_id.errors))
        self.assertTrue(any("不支持的 kind" in item for item in alias.errors))
        self.assertEqual(transport.calls, [])

    def test_failed_stage_blocks_later_stage_and_flow_cleanup_runs_once(self):
        cleanup_calls: list[str] = []

        def restore(*, body_ref):
            cleanup_calls.append(body_ref)
            return CleanupResult(success=True, details={"restored": True})

        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            bundle = _approved_bundle(root, ["http_api", "http_api"], cleanup=True)
            transport = _SequenceTransport([500])
            summary = ExecutionCoordinator(
                RunnerRegistry(http=HttpRunner(transport=transport))
            ).execute(
                bundle,
                root,
                RuntimeContext(
                    variables={"runtime": {"account_id": "ACCOUNT-1"}},
                    base_urls={"api.main": "https://example.test"},
                    cleanup_hooks={"cleanup.account.restore": restore},
                    evidence_dir=root / "evidence",
                ),
                run_id="RUN-FAIL-CLEANUP-V4",
            )

        self.assertEqual(transport.calls, ["https://example.test/stage-1"])
        self.assertEqual(cleanup_calls, ["ACCOUNT-1"])
        self.assertEqual(
            [item.status for item in summary.stages],
            [RunStatus.FAILED, RunStatus.BLOCKED],
        )
        self.assertIn("UPSTREAM_STAGE_NOT_PASSED", summary.stages[1].errors[0])
        self.assertEqual(summary.flows[0].cleanup.status, RunStatus.PASSED)
        self.assertEqual(summary.status, RunStatus.FAILED)

    def test_ui_procedure_runner_is_registered_by_default(self):
        registry = RunnerRegistry()
        self.assertIsInstance(registry.get("procedure_playwright"), ProcedureRunner)
        with self.assertRaisesRegex(RunnerError, "EXECUTOR_UNKNOWN"):
            registry.get("misspelled_executor")

    def test_procedure_flow_still_checks_all_artifact_hashes_before_deferral(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            bundle = _approved_bundle(root, ["http_api", "procedure_playwright"])
            first = bundle.compiled_artifacts[0]
            payload_path = (
                generated_files_root(root, first.executor_kind)
                / bundle.plan.plan_id
                / "v1"
                / first.flow_id
                / first.stage_id
                / "execution.json"
            )
            payload_path.write_text("tampered\n", encoding="utf-8")
            transport = _SequenceTransport([200])
            summary = ExecutionCoordinator(
                RunnerRegistry(http=HttpRunner(transport=transport))
            ).execute(
                bundle,
                root,
                RuntimeContext(base_urls={"api.main": "https://example.test"}),
                run_id="RUN-PROCEDURE-HASH-V4",
            )

        self.assertEqual(transport.calls, [])
        self.assertEqual([item.status for item in summary.stages], [RunStatus.BLOCKED] * 2)
        self.assertTrue(any("ARTIFACT_HASH_MISMATCH" in item for item in summary.errors))
        self.assertTrue(
            all("FLOW_PREFLIGHT_FAILED" in item.errors[0] for item in summary.stages)
        )

    def test_database_runner_consumes_v4_stage_and_rejects_literal_sql(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            database_path = root / "accounts.sqlite"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO accounts(id) VALUES ('ACCOUNT-1')")
            connection.commit()
            connection.close()
            payload = {
                "schema_version": "database-execution-plan.v4",
                "executor_kind": "database",
                "flow_id": "FLOW-DB",
                "stage_id": "STAGE-DB",
                "connection_profile_ref": "db.main",
                "read_only": True,
                "queries": [
                    {
                        "query_id": "QUERY-1",
                        "query_ref": "db.account",
                        "parameters_refs": {"id": "account_id"},
                        "assertions": [
                            {
                                "expected_result_id": "EXPECTED-DB",
                                "kind": "row_count",
                                "operator": "equals",
                                "expected": 1,
                            }
                        ],
                    }
                ],
            }
            artifact = _write_artifact(
                root / "db-stage",
                executor_kind="database",
                artifact_schema_version="database-execution-plan.v4",
                flow_id="FLOW-DB",
                stage_id="STAGE-DB",
                payload=payload,
            )
            context = RuntimeContext(
                variables={"account_id": "ACCOUNT-1"},
                query_catalog={
                    "db.account": {
                        "read_only": True,
                        "sql": "SELECT id FROM accounts WHERE id = :id",
                    }
                },
                database_connections={"db.main": database_path},
                evidence_dir=root / "evidence",
            )
            passed = DatabaseRunner().run(root / "db-stage", artifact, context)

            unsafe_payload = dict(payload)
            unsafe_payload["flow_id"] = "FLOW-DB-UNSAFE"
            unsafe_payload["stage_id"] = "STAGE-DB-UNSAFE"
            unsafe_payload["queries"] = [
                {"query_id": "QUERY-1", "sql": "DELETE FROM accounts", "assertions": []}
            ]
            unsafe_artifact = _write_artifact(
                root / "unsafe-db-stage",
                executor_kind="database",
                artifact_schema_version="database-execution-plan.v4",
                flow_id="FLOW-DB-UNSAFE",
                stage_id="STAGE-DB-UNSAFE",
                payload=unsafe_payload,
            )
            blocked = DatabaseRunner().run(root / "unsafe-db-stage", unsafe_artifact, context)

        self.assertEqual(passed.status, RunStatus.PASSED)
        self.assertEqual((passed.flow_id, passed.stage_id), ("FLOW-DB", "STAGE-DB"))
        self.assertEqual(blocked.status, RunStatus.BLOCKED)
        self.assertTrue(any("QUERY_NOT_READ_ONLY" in item for item in blocked.errors))

    def test_database_runner_executes_approved_v5_ai_sql_and_enforces_schema(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            database_path = root / "accounts.sqlite"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE accounts (id TEXT PRIMARY KEY, locked INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO accounts VALUES ('ACCOUNT-1', 1)")
            connection.commit()
            connection.close()
            payload = {
                "connection_profile_ref": "db.main",
                "read_only": True,
                "queries": [
                    {
                        "query_id": "AI-QUERY-1",
                        "source": {
                            "source_kind": "expected_result",
                            "source_id": "EXPECTED-DB",
                        },
                        "operation_ref": "ai.sql.lock-state",
                        "sql": "SELECT locked FROM accounts WHERE id = :id",
                        "sql_origin": "ai_generated",
                        "knowledge_scope_id": None,
                        "parameters_refs": {"id": "runtime.account_id"},
                        "assertions": [
                            {
                                "expected_result_id": "EXPECTED-DB",
                                "kind": "column",
                                "column": "locked",
                                "operator": "equals",
                                "expected": 1,
                            }
                        ],
                    }
                ],
            }
            artifact = _write_artifact(
                root / "db-v5",
                executor_kind="database",
                artifact_schema_version="database-execution-plan.v5",
                flow_id="FLOW-DB-V5",
                stage_id="STAGE-DB-V5",
                payload=payload,
            )
            context = RuntimeContext(
                variables={"runtime": {"account_id": "ACCOUNT-1"}},
                database_connections={"db.main": database_path},
                database_schemas={
                    "db.main": {
                        "dialect": "sqlite",
                        "tables": [
                            {
                                "name": "accounts",
                                "columns": [
                                    {"name": "id"},
                                    {"name": "locked"},
                                ],
                            }
                        ],
                        "allowed_parameter_refs": ["runtime.account_id"],
                    }
                },
                evidence_dir=root / "evidence",
            )
            passed = DatabaseRunner().run(root / "db-v5", artifact, context)

            unsafe_payload = {
                **payload,
                "queries": [
                    {
                        **payload["queries"][0],
                        "sql": "SELECT secret_value FROM accounts WHERE id = :id",
                    }
                ],
            }
            unsafe_artifact = _write_artifact(
                root / "db-v5-unsafe",
                executor_kind="database",
                artifact_schema_version="database-execution-plan.v5",
                flow_id="FLOW-DB-V5-UNSAFE",
                stage_id="STAGE-DB-V5-UNSAFE",
                payload=unsafe_payload,
            )
            blocked = DatabaseRunner().run(
                root / "db-v5-unsafe", unsafe_artifact, context
            )

        self.assertEqual(passed.status, RunStatus.PASSED)
        self.assertEqual(blocked.status, RunStatus.BLOCKED)
        self.assertTrue(any("未登记字段" in item for item in blocked.errors))

    def test_database_runner_rejects_alias_and_missing_operator(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            database_path = root / "accounts.sqlite"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY)")
            connection.commit()
            connection.close()
            base_query = {
                "query_id": "QUERY-1",
                "query_ref": "db.account",
                "parameters_refs": {},
            }
            alias_payload = {
                "schema_version": "database-execution-plan.v4",
                "executor_kind": "database",
                "flow_id": "FLOW-DB-ALIAS",
                "stage_id": "STAGE-DB-ALIAS",
                "connection_profile_ref": "db.main",
                "read_only": True,
                "queries": [
                    {
                        **base_query,
                        "assertions": [
                            {
                                "expected_result_id": "EXPECTED-DB-ALIAS",
                                "kind": "count",
                                "operator": "equals",
                                "expected": 0,
                            }
                        ],
                    }
                ],
            }
            missing_operator_payload = {
                **alias_payload,
                "flow_id": "FLOW-DB-MISSING-OPERATOR",
                "stage_id": "STAGE-DB-MISSING-OPERATOR",
                "queries": [
                    {
                        **base_query,
                        "assertions": [
                            {
                                "expected_result_id": "EXPECTED-DB-MISSING",
                                "kind": "row_count",
                                "expected": 0,
                            }
                        ],
                    }
                ],
            }
            alias_artifact = _write_artifact(
                root / "db-alias",
                executor_kind="database",
                artifact_schema_version="database-execution-plan.v4",
                flow_id="FLOW-DB-ALIAS",
                stage_id="STAGE-DB-ALIAS",
                payload=alias_payload,
            )
            missing_operator_artifact = _write_artifact(
                root / "db-missing-operator",
                executor_kind="database",
                artifact_schema_version="database-execution-plan.v4",
                flow_id="FLOW-DB-MISSING-OPERATOR",
                stage_id="STAGE-DB-MISSING-OPERATOR",
                payload=missing_operator_payload,
            )
            context = RuntimeContext(
                query_catalog={"db.account": {"read_only": True, "sql": "SELECT id FROM accounts"}},
                database_connections={"db.main": database_path},
            )
            alias = DatabaseRunner().run(root / "db-alias", alias_artifact, context)
            missing_operator = DatabaseRunner().run(
                root / "db-missing-operator",
                missing_operator_artifact,
                context,
            )

        self.assertEqual(alias.status, RunStatus.BLOCKED)
        self.assertEqual(missing_operator.status, RunStatus.BLOCKED)
        self.assertTrue(any("不支持 kind: count" in item for item in alias.errors))
        self.assertTrue(any("operator 不受支持" in item for item in missing_operator.errors))

    def test_performance_runner_consumes_v4_stage(self):
        driver_payloads: list[dict] = []

        def driver(plan, context):
            driver_payloads.append(dict(plan))
            return {"metrics": {"latency_ms": {"value": 120, "unit": "ms"}}}

        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            payload = {
                "schema_version": "performance-execution-plan.v4",
                "executor_kind": "performance",
                "flow_id": "FLOW-PERF",
                "stage_id": "STAGE-PERF",
                "driver_ref": "driver.fake",
                "load_profile_ref": "perf.smoke",
                "stages": [{"duration_seconds": 1, "virtual_users": 1}],
                "input_refs": {},
                "thresholds": [
                    {
                        "threshold_id": "THRESHOLD-1",
                        "expected_result_id": "EXPECTED-PERF",
                        "metric": "latency_ms",
                        "operator": "lte",
                        "value": 200,
                        "unit": "ms",
                    }
                ],
            }
            artifact = _write_artifact(
                root / "perf-stage",
                executor_kind="performance",
                artifact_schema_version="performance-execution-plan.v4",
                flow_id="FLOW-PERF",
                stage_id="STAGE-PERF",
                payload=payload,
            )
            result = PerformanceRunner().run(
                root / "perf-stage",
                artifact,
                RuntimeContext(
                    performance_mode="live",
                    performance_profiles={"perf.smoke": {"name": "smoke"}},
                    performance_drivers={
                        "driver.fake": driver
                    },
                    evidence_dir=root / "evidence",
                ),
            )

        self.assertEqual(result.status, RunStatus.PASSED)
        self.assertEqual((result.flow_id, result.stage_id), ("FLOW-PERF", "STAGE-PERF"))
        self.assertEqual(
            driver_payloads[0]["thresholds"][0]["after_operation_id"],
            "OP-PERFORMANCE-0001",
        )

    def test_performance_runner_rejects_alias_and_missing_threshold_id(self):
        driver_calls: list[dict] = []

        def driver(plan, context):
            driver_calls.append(dict(plan))
            return {"metrics": {"latency_ms": 1}}

        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            base_payload = {
                "schema_version": "performance-execution-plan.v4",
                "executor_kind": "performance",
                "driver_ref": "driver.fake",
                "load_profile_ref": "perf.smoke",
                "stages": [{"duration_seconds": 1, "virtual_users": 1}],
                "input_refs": {},
            }
            alias_payload = {
                **base_payload,
                "flow_id": "FLOW-PERF-ALIAS",
                "stage_id": "STAGE-PERF-ALIAS",
                "thresholds": [
                    {
                        "threshold_id": "THRESHOLD-ALIAS",
                        "expected_result_id": "EXPECTED-PERF-ALIAS",
                        "metric": "latency_ms",
                        "operator": "<=",
                        "value": 10,
                    }
                ],
            }
            missing_id_payload = {
                **base_payload,
                "flow_id": "FLOW-PERF-MISSING-ID",
                "stage_id": "STAGE-PERF-MISSING-ID",
                "thresholds": [
                    {
                        "expected_result_id": "EXPECTED-PERF-MISSING-ID",
                        "metric": "latency_ms",
                        "operator": "lte",
                        "value": 10,
                    }
                ],
            }
            alias_artifact = _write_artifact(
                root / "perf-alias",
                executor_kind="performance",
                artifact_schema_version="performance-execution-plan.v4",
                flow_id="FLOW-PERF-ALIAS",
                stage_id="STAGE-PERF-ALIAS",
                payload=alias_payload,
            )
            missing_id_artifact = _write_artifact(
                root / "perf-missing-id",
                executor_kind="performance",
                artifact_schema_version="performance-execution-plan.v4",
                flow_id="FLOW-PERF-MISSING-ID",
                stage_id="STAGE-PERF-MISSING-ID",
                payload=missing_id_payload,
            )
            context = RuntimeContext(
                performance_mode="live",
                performance_profiles={"perf.smoke": {}},
                performance_drivers={"driver.fake": driver},
            )
            alias = PerformanceRunner().run(root / "perf-alias", alias_artifact, context)
            missing_id = PerformanceRunner().run(
                root / "perf-missing-id", missing_id_artifact, context
            )

        self.assertEqual(alias.status, RunStatus.BLOCKED)
        self.assertEqual(missing_id.status, RunStatus.BLOCKED)
        self.assertTrue(any("THRESHOLD_INVALID" in item for item in alias.errors))
        self.assertTrue(any("threshold_id" in item for item in missing_id.errors))
        self.assertEqual(driver_calls, [])

    def test_compiler_to_coordinator_setup_http_db_cleanup_e2e(self):
        cleanup_calls: list[str] = []

        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            database_path = root / "state.sqlite"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE accounts (account TEXT PRIMARY KEY, locked INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO accounts VALUES ('ACCOUNT-1', 1)")
            connection.commit()
            connection.close()
            handoff = _compile_e2e_handoff(root)
            flow = handoff.plan.flows[0]
            setup_artifact = handoff.compiled_artifacts[0]
            setup_manifest = json.loads(
                (
                    generated_files_root(root, setup_artifact.executor_kind)
                    / handoff.plan.plan_id
                    / "v1"
                    / setup_artifact.flow_id
                    / setup_artifact.stage_id
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            transport = _SqliteStateTransport(database_path)

            def restore(*, account_id):
                cleanup_calls.append(account_id)
                cleanup_connection = sqlite3.connect(database_path)
                cleanup_connection.execute(
                    "UPDATE accounts SET locked = 0 WHERE account = ?",
                    (account_id,),
                )
                cleanup_connection.commit()
                cleanup_connection.close()
                return CleanupResult(success=True)

            summary = ExecutionCoordinator(
                RunnerRegistry(http=HttpRunner(transport=transport))
            ).execute(
                handoff,
                root,
                RuntimeContext(
                    variables={"account": "ACCOUNT-1"},
                    base_urls={"api.main": "https://example.test"},
                    query_catalog={
                        "db.account.lock-state": {
                            "read_only": True,
                            "sql": "SELECT locked FROM accounts WHERE account = :account",
                        }
                    },
                    database_connections={"db.main": database_path},
                    cleanup_hooks={"cleanup.account.restore": restore},
                    evidence_dir=root / "evidence",
                ),
                run_id="RUN-COMPILER-E2E-V4",
            )
            check = sqlite3.connect(database_path)
            locked = check.execute(
                "SELECT locked FROM accounts WHERE account = 'ACCOUNT-1'"
            ).fetchone()[0]
            check.close()

        self.assertNotIn("lifecycle", setup_manifest)
        self.assertEqual(
            setup_manifest["traceability"]["steps"][0]["source"]["source_kind"],
            "required_state",
        )
        self.assertEqual(
            transport.calls,
            [
                "https://example.test/setup/unlock",
                "https://example.test/accounts/ACCOUNT-1/lock",
            ],
        )
        self.assertEqual([item.status for item in summary.stages], [RunStatus.PASSED] * 3)
        self.assertEqual([item.stage_id for item in summary.stages], [s.stage_id for s in flow.stages])
        self.assertEqual(cleanup_calls, ["ACCOUNT-1"])
        self.assertEqual(summary.flows[0].cleanup.status, RunStatus.PASSED)
        self.assertEqual(locked, 0)

    def test_failed_compiled_setup_blocks_later_stages_and_still_cleans_once(self):
        cleanup_calls: list[str] = []

        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            database_path = root / "state.sqlite"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE accounts (account TEXT PRIMARY KEY, locked INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO accounts VALUES ('ACCOUNT-1', 1)")
            connection.commit()
            connection.close()
            handoff = _compile_e2e_handoff(root)
            transport = _SqliteStateTransport(database_path, fail_first=True)

            def restore(*, account_id):
                cleanup_calls.append(account_id)
                cleanup_connection = sqlite3.connect(database_path)
                cleanup_connection.execute(
                    "UPDATE accounts SET locked = 0 WHERE account = ?",
                    (account_id,),
                )
                cleanup_connection.commit()
                cleanup_connection.close()
                return CleanupResult(success=True)

            summary = ExecutionCoordinator(
                RunnerRegistry(http=HttpRunner(transport=transport))
            ).execute(
                handoff,
                root,
                RuntimeContext(
                    variables={"account": "ACCOUNT-1"},
                    base_urls={"api.main": "https://example.test"},
                    query_catalog={
                        "db.account.lock-state": {
                            "read_only": True,
                            "sql": "SELECT locked FROM accounts WHERE account = :account",
                        }
                    },
                    database_connections={"db.main": database_path},
                    cleanup_hooks={"cleanup.account.restore": restore},
                    evidence_dir=root / "evidence",
                ),
                run_id="RUN-SETUP-FAIL-V4",
            )
            check = sqlite3.connect(database_path)
            locked = check.execute(
                "SELECT locked FROM accounts WHERE account = 'ACCOUNT-1'"
            ).fetchone()[0]
            check.close()

        self.assertEqual(transport.calls, ["https://example.test/setup/unlock"])
        self.assertNotEqual(summary.stages[0].status, RunStatus.PASSED)
        self.assertEqual(
            [item.status for item in summary.stages[1:]],
            [RunStatus.BLOCKED, RunStatus.BLOCKED],
        )
        self.assertTrue(
            all("UPSTREAM_STAGE_NOT_PASSED" in item.errors[0] for item in summary.stages[1:])
        )
        self.assertEqual(cleanup_calls, ["ACCOUNT-1"])
        self.assertEqual(summary.flows[0].cleanup.status, RunStatus.PASSED)
        self.assertEqual(locked, 0)


if __name__ == "__main__":
    unittest.main()
