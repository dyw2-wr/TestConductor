"""Compile a UI Procedure stage into a review workbook and execution manifest."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle

from ..catalogs import PlanningCatalogSnapshot
from ..contracts import (
    ProcedureExecution,
    ExecutorArtifactBundle,
    ExecutorArtifactRef,
    PlanFlow,
    PlanStage,
    TestPlanDraft,
)
from .json_common import _manifest, canonical_json, sha256_bytes


class ProcedureStageCompiler:
    artifact_schema_version = "procedure-stage-bundle.v4"

    def compile(
        self,
        bundle: ApprovedTestDesignBundle,
        plan: TestPlanDraft,
        flow: PlanFlow,
        stage: PlanStage,
        catalog: PlanningCatalogSnapshot,
        output_root: str | Path,
    ) -> ExecutorArtifactBundle:
        execution = stage.execution
        if not isinstance(execution, ProcedureExecution):
            raise ValueError("ProcedureStageCompiler 只接受 ProcedureExecution")
        destination = (
            Path(output_root)
            / plan.plan_id
            / f"v{plan.version}"
            / flow.flow_id
            / stage.stage_id
        )
        destination.mkdir(parents=True, exist_ok=True)
        workbook_path = destination / "case.xlsx"
        manifest_path = destination / "manifest.json"
        rows = [
            {
                "Test Case ID": flow.flow_id,
                "Test Case Name": flow.name,
                "Test Step ID": row.row_id,
                "UR": ", ".join(flow.requirement_ids),
                "Action": row.action,
                # Procedure's reader accepts explicit key=value assignments. Keep
                # runtime placeholders intact; the future external bridge resolves
                # them from its injected variable source after this artifact is handed off.
                "Input Data": row.input_data or "",
                "Check": row.checkpoint or "",
            }
            for row in execution.rows
        ]
        columns = [
            "Test Case ID",
            "Test Case Name",
            "Test Step ID",
            "UR",
            "Action",
            "Input Data",
            "Check",
        ]
        pd.DataFrame(rows, columns=columns).to_excel(
            workbook_path,
            sheet_name="Case",
            index=False,
        )
        workbook_hash = sha256_bytes(workbook_path.read_bytes())
        artifact_id = f"ARTIFACT-{flow.flow_id}-{stage.stage_id}"
        manifest = _manifest(
            bundle,
            plan,
            flow,
            stage,
            catalog,
            artifact_id=artifact_id,
            artifact_schema_version=self.artifact_schema_version,
            artifact_refs=[{"kind": "workbook", "path_ref": "case.xlsx"}],
            compiled_artifact_hashes={"case.xlsx": workbook_hash},
        )
        manifest.update(
            {
                "workbook_schema": "WorkbookV2",
                "payload_format": "xlsx",
                "capability_profile_ref": execution.capability_profile_ref,
                "capability_site": execution.capability_site,
                "library_id": execution.library_id,
                "library_hash": execution.library_hash,
                "procedure_refs": list(execution.procedure_refs),
                "procedure_calls": [
                    {
                        "row_id": row.row_id,
                        "operation_ref": row.operation_ref,
                        "procedure_id": row.procedure_id,
                        "procedure_version": row.procedure_version,
                        "procedure_fingerprint": row.procedure_fingerprint,
                        "data_bindings": [
                            item.model_dump(mode="json")
                            for item in row.data_bindings
                        ],
                        "assertions": [
                            item.model_dump(mode="json")
                            for item in row.assertions
                        ],
                    }
                    for row in execution.rows
                ],
            }
        )
        manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        manifest_hash = sha256_bytes(manifest_path.read_bytes())
        return ExecutorArtifactBundle(
            artifact_id=artifact_id,
            artifact_schema_version=self.artifact_schema_version,
            executor_kind=stage.executor_kind,
            flow_id=flow.flow_id,
            stage_id=stage.stage_id,
            design_id=bundle.design.design_id,
            design_version=bundle.design.version,
            design_content_hash=bundle.review.design_content_hash,
            design_input_content_hash=bundle.review.input_content_hash,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            plan_content_hash=plan.content_hash(),
            catalog_id=catalog.catalog_id,
            catalog_content_hash=catalog.content_hash,
            manifest_path_ref="manifest.json",
            artifact_refs=[
                ExecutorArtifactRef(
                    kind="workbook", path_ref="case.xlsx", sha256=workbook_hash
                ),
                ExecutorArtifactRef(
                    kind="manifest", path_ref="manifest.json", sha256=manifest_hash
                ),
            ],
        )


__all__ = ["ProcedureStageCompiler"]
