from __future__ import annotations

from datetime import datetime
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pydantic import BaseModel, ConfigDict

from apps.test_platform.admin import (
    ApprovedKnowledgeEntryForm,
    TestPlanExecutionInputForm,
    TestResourceProfileForm,
    TestWorkflowForm,
    _artifact_generation_lock,
    _json,
)
from apps.test_platform.intent.contracts import ApprovedTestDesignBundle, DesignSelections
from apps.test_platform.artifact_store import import_approved_test_plan
from apps.test_platform.models import (
    ApprovedKnowledgeEntry,
    ExecutionPlanArtifact,
    TestExecutionRun,
    TestIntentImport,
    TestPlanArtifact,
    TestResourceProfile,
    TestWorkflow,
)
from apps.test_platform.service_factory import (
    OpenAICompatibleModelGateway,
    get_runtime_context,
    get_workflow,
)
from tests.test_planning_flow_v4 import _approved_bundle


class TestWorkflowAdminTests(TestCase):
    def test_resource_form_explains_every_resource_and_downloads_real_examples(self):
        listing = self.client.get(
            reverse("admin:test_platform_testresourceprofile_changelist")
        )
        self.assertContains(listing, "新增测试资源总配置")
        self.assertContains(listing, "一体化资源")
        self.assertNotContains(listing, "新增 UI 资源")
        self.assertNotContains(listing, "新增接口资源")

        page = self.client.get(
            reverse("admin:test_platform_testresourceprofile_add")
        )

        self.assertEqual(page.status_code, 200)
        for text in (
            "配置名称和被测系统",
            "每个网站一个 SQLite 文件",
            "local.workflow.login@v2",
            "不包含导航、录制过程、Repair 经验或调用历史",
            "OpenAPI 或大致描述 method、path、参数",
            "DDL、数据字典、表结构文档或简单文字",
            "性能要求、历史方案、现有配置或简单文字",
            "一个主机名/IP 和一个端口号",
            "格式：",
            "作用：",
            "本配置包含的测试能力（可多选）",
            "接口资料说明（可选）",
            "数据库资料说明（可选）",
            "性能资料说明（可选）",
        ):
            self.assertContains(page, text)

        examples = {
            "openapi": b"openapi: 3.0.3",
            "database": b'"schema_version": "database-access-policy.v1"',
            "performance": b'"schema_version": "performance-profile-set.v1"',
        }
        for example_name, marker in examples.items():
            response = self.client.get(
                reverse(
                    "admin:test_platform_testresourceprofile_example",
                    args=[example_name],
                )
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment;", response["Content-Disposition"])
            self.assertIn(marker, b"".join(response.streaming_content))
            response.close()

        knowledge_page = self.client.get(
            reverse("admin:test_platform_approvedknowledgeentry_add")
        )
        self.assertContains(knowledge_page, "上传常见文档")
        self.assertContains(knowledge_page, "不要求固定模板")
        self.assertContains(knowledge_page, "不会扩大测试资源权限")

    def test_workflow_form_has_four_required_inputs_and_mirrors_channels(self):
        self.assertTrue(admin.site.is_registered(TestIntentImport))
        profile = TestResourceProfile(
            name="Local TCP",
            port_host="127.0.0.1",
            port_number=9000,
        )
        profile.full_clean()
        profile.save()
        form = TestWorkflowForm(
            data={
                "requirement_text": "TCP 服务端口应可连接",
                "resource_profile": profile.pk,
                "allowed_channels": ["port"],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("required_channels", form.fields)
        self.assertIn("knowledge_scope_ids", form.fields)
        for removed in (
            "title",
            "request_id",
            "ui_coverage",
            "api_coverage",
            "database_coverage",
            "performance_coverage",
            "port_coverage",
        ):
            self.assertNotIn(removed, form.fields)
        workflow = form.save()
        self.assertEqual(workflow.allowed_channels, ["port"])
        self.assertEqual(workflow.coverage_by_category, {})
        self.assertEqual(workflow.knowledge_scope_ids, [])
        self.assertEqual(workflow.title, "TCP 服务端口应可连接")

    @override_settings(
        TEST_PLATFORM_MILVUS_ENABLED=True,
        TEST_PLATFORM_APPROVED_KNOWLEDGE_CATALOG=(
            Path(__file__).resolve().parents[3]
            / "examples"
            / "test_resources"
            / "approved_knowledge_catalog.json"
        ),
    )
    def test_workflow_form_exposes_business_knowledge_without_internal_metadata(self):
        profile = TestResourceProfile.objects.create(
            name="Demo TCP",
            system_id="demo-system",
            port_host="127.0.0.1",
            port_number=9000,
        )
        form = TestWorkflowForm(
            data={
                "title": "账户安全测试",
                "requirement_text": "验证账户锁定规则",
                "resource_profile": profile.pk,
                "allowed_channels": ["port"],
                "knowledge_scope_ids": ["account-security"],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        labels = [label for _, label in form.fields["knowledge_scope_ids"].choices]
        self.assertIn("account-lock-policy", labels)
        self.assertFalse(any("连续登录失败5次" in label for label in labels))
        self.assertFalse(any("account-security" in label or "v1" in label for label in labels))
        workflow = form.save()
        self.assertEqual(workflow.knowledge_scope_ids, ["account-security"])

    def test_admin_imports_publishes_and_exposes_business_knowledge_without_milvus(self):
        add_url = reverse("admin:test_platform_approvedknowledgeentry_add")
        add_page = self.client.get(add_url)
        self.assertContains(add_page, "导入文件（可选）")
        self.assertNotContains(add_page, "审批信息")
        response = self.client.post(
            add_url,
            {
                "system_id": "account-system",
                "title": "",
                "content": "",
                "source_file": SimpleUploadedFile(
                    "account-policy.md",
                    "连续登录失败 5 次后锁定账户。".encode("utf-8"),
                    content_type="text/markdown",
                ),
                "_save": "保存",
            },
        )
        entry = ApprovedKnowledgeEntry.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(entry.title, "account-policy")
        self.assertIn("锁定账户", entry.content)
        self.assertEqual(entry.status, ApprovedKnowledgeEntry.Status.DRAFT)

        response = self.client.post(
            reverse(
                "admin:test_platform_approvedknowledgeentry_change",
                args=[entry.pk],
            ),
            {
                "system_id": entry.system_id,
                "title": entry.title,
                "content": entry.content,
                "_approve_knowledge": "1",
            },
        )
        entry.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(entry.status, ApprovedKnowledgeEntry.Status.APPROVED)
        self.assertTrue(entry.approval_id)
        self.assertTrue(entry.content_hash.startswith("sha256:"))
        detail = self.client.get(
            reverse(
                "admin:test_platform_approvedknowledgeentry_change",
                args=[entry.pk],
            )
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "已发布")

        profile = TestResourceProfile.objects.create(
            name="Account TCP",
            system_id="account-system",
            port_host="127.0.0.1",
            port_number=9000,
        )
        form = TestWorkflowForm(
            data={
                "requirement_text": "验证账户锁定",
                "resource_profile": profile.pk,
                "allowed_channels": ["port"],
                "knowledge_scope_ids": [entry.scope_id],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        labels = [label for _, label in form.fields["knowledge_scope_ids"].choices]
        self.assertEqual(labels, ["account-policy"])
        self.assertFalse(any("锁定账户" in label for label in labels))

    def test_knowledge_rejects_secrets_and_cross_system_selection(self):
        secret_form = ApprovedKnowledgeEntryForm(
            data={
                "system_id": "payment-system",
                "title": "错误的支付接入说明",
                "content": "api_token=live-production-token-value",
            }
        )
        self.assertFalse(secret_form.is_valid())
        self.assertIn("不能包含密码", str(secret_form.errors))

        entry = ApprovedKnowledgeEntry.objects.create(
            system_id="logistics-system",
            title="冷链配送温度规则",
            content="冷链商品运输温度必须保持在 2 至 8 摄氏度。",
        )
        entry.approve()
        profile = TestResourceProfile.objects.create(
            name="Payment port",
            system_id="payment-system",
            port_host="127.0.0.1",
            port_number=9443,
        )
        workflow_form = TestWorkflowForm(
            data={
                "requirement_text": "验证支付回调端口",
                "resource_profile": profile.pk,
                "allowed_channels": ["port"],
                "knowledge_scope_ids": [entry.scope_id],
            }
        )
        self.assertFalse(workflow_form.is_valid())
        self.assertIn("不属于当前被测系统", str(workflow_form.errors))

    def test_admin_home_exposes_only_the_business_steps(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertNotIn("各模块填写示例", content)
        self.assertNotIn("data-example-dialog", content)
        labels = [
            "测试资源配置",
            "业务知识库",
            "导入测试意图",
            "审批测试计划",
            "审批执行计划",
            "执行历史",
        ]
        positions = [content.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("认证和授权", content)
        self.assertNotIn("Django 管理", content)
        self.assertIn("TestConductor", content)
        self.assertNotIn("修改密码", content)
        self.assertNotIn("退出", content)
        self.assertNotIn('id="nav-sidebar"', content)
        self.assertNotIn("reviewer", content)
        self.assertNotIn("账户与权限", content)
        for model in (
            ApprovedKnowledgeEntry,
            TestIntentImport,
            TestPlanArtifact,
            ExecutionPlanArtifact,
            TestExecutionRun,
        ):
            self.assertTrue(admin.site.is_registered(model))

    def test_module_example_downloads_are_real_and_plan_example_is_valid(self):
        downloads = (
            (
                reverse("admin:test_platform_testresourceprofile_example", args=["openapi"]),
                b"openapi: 3.0.3",
            ),
            (
                reverse("admin:test_platform_testresourceprofile_example", args=["database"]),
                b"database-access-policy.v1",
            ),
            (
                reverse("admin:test_platform_testresourceprofile_example", args=["performance"]),
                b"performance-profile-set.v1",
            ),
            (
                reverse("admin:test_platform_approvedknowledgeentry_example"),
                "账户锁定规则".encode("utf-8"),
            ),
            (
                reverse("admin:test_platform_testintentimport_example"),
                "账户登录安全测试需求".encode("utf-8"),
            ),
        )
        for url, marker in downloads:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment;", response["Content-Disposition"])
            self.assertIn(marker, b"".join(response.streaming_content))
            response.close()

        plan_response = self.client.get(
            reverse("admin:test_platform_testplanartifact_import_example")
        )
        payload = json.loads(b"".join(plan_response.streaming_content).decode("utf-8"))
        plan_response.close()
        bundle = ApprovedTestDesignBundle.model_validate(payload)
        self.assertEqual(bundle.design.target.system_id, "account-web")
        self.assertEqual(bundle.design.target.environment, "staging")
        profile = TestResourceProfile.objects.create(
            name="示例计划匹配资源",
            system_id="account-web",
            environment="staging",
            ui_procedure_database="account-assets.sqlite",
            database_query_file="queries.json",
            database_connection_ref="account-readonly",
        )
        artifact = import_approved_test_plan(payload, profile)
        self.assertEqual(artifact.title, "登录错误锁定")
        self.assertEqual(artifact.status, TestPlanArtifact.Status.APPROVED)

    def test_execution_plan_list_labels_filters_and_searches_two_sources(self):
        profile = TestResourceProfile.objects.create(
            name="来源筛选资源",
            port_host="127.0.0.1",
            port_number=9000,
        )
        intent = TestWorkflow.objects.create(
            title="来源意图甲",
            requirement_text="检查来源检索",
            resource_profile=profile,
        )
        generated_plan = TestPlanArtifact.objects.create(
            source_intent=intent,
            resource_profile=profile,
            title="意图生成测试计划",
            design_id="DESIGN-SOURCE-GENERATED",
            version=1,
            content_hash="sha256:" + "1" * 64,
            generation_result={
                "input_snapshot": {
                    "approved_knowledge": [
                        {"content": "同一账号连续失败五次后必须锁定"}
                    ]
                }
            },
        )
        imported_plan = TestPlanArtifact.objects.create(
            source_kind=TestPlanArtifact.SourceKind.IMPORTED,
            resource_profile=profile,
            title="手动导入测试计划",
            design_id="DESIGN-SOURCE-IMPORTED",
            version=1,
            content_hash="sha256:" + "2" * 64,
        )
        generated_execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=generated_plan,
            resource_profile=profile,
            title="意图生成执行计划",
            plan_id="PLAN-SOURCE-GENERATED",
            version=1,
            content_hash="sha256:" + "3" * 64,
            artifact_root_ref="source/generated",
        )
        imported_execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=imported_plan,
            resource_profile=profile,
            title="手动导入执行计划",
            plan_id="PLAN-SOURCE-IMPORTED",
            version=1,
            content_hash="sha256:" + "4" * 64,
            artifact_root_ref="source/imported",
        )
        url = reverse("admin:test_platform_executionplanartifact_changelist")

        listing = self.client.get(url)
        self.assertContains(listing, "手动导入测试计划")
        self.assertContains(listing, "测试意图生成")

        imported_only = self.client.get(
            url,
            {"plan_source": TestPlanArtifact.SourceKind.IMPORTED},
        )
        self.assertContains(imported_only, imported_execution.title)
        self.assertNotContains(imported_only, generated_execution.title)

        intent_search = self.client.get(url, {"q": intent.title})
        self.assertContains(intent_search, generated_execution.title)
        self.assertNotContains(intent_search, imported_execution.title)

        generated_detail = self.client.get(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[generated_execution.pk],
            )
        )
        self.assertContains(generated_detail, "根据测试意图生成")
        self.assertContains(generated_detail, intent.title)
        self.assertContains(generated_detail, "同一账号连续失败五次后必须锁定")

        imported_detail = self.client.get(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[imported_execution.pk],
            )
        )
        self.assertContains(imported_detail, "手动导入已审批测试计划")
        self.assertContains(imported_detail, "无关联测试意图")
        self.assertContains(imported_detail, "导入文件未包含业务知识库使用记录")

    def test_intent_uses_category_entries_and_single_record_commands(self):
        profile = TestResourceProfile.objects.create(
            name="Local TCP",
            port_host="127.0.0.1",
            port_number=9000,
        )
        workflow = TestWorkflow.objects.create(
            title="端口连通性",
            requirement_text="服务端口应可连接",
            resource_profile=profile,
            allowed_channels=["port"],
            coverage_by_category={"port": ["positive"]},
        )

        listing = self.client.get(
            reverse("admin:test_platform_testintentimport_changelist")
        )
        self.assertEqual(listing.status_code, 200)
        html = listing.content.decode("utf-8")
        self.assertNotIn('name="action"', html)
        self.assertIn("新建测试意图", html)
        self.assertNotIn("新建 UI 测试", html)
        self.assertNotIn("新建接口测试", html)

        detail = self.client.get(
            reverse(
                "admin:test_platform_testintentimport_change",
                args=[workflow.pk],
            )
        )
        self.assertEqual(detail.status_code, 200)
        html = detail.content.decode("utf-8")
        self.assertIn("生成测试计划", html)
        self.assertIn('value="保存"', html)
        self.assertIn("删除", html)
        self.assertNotIn("增加另一个 测试资源配置", html)
        self.assertNotIn("更改选中的测试资源配置", html)
        self.assertNotIn("保存并继续编辑", html)
        self.assertNotIn("保存并增加另一个", html)

    def test_single_record_command_dispatches_only_the_submitted_handler(self):
        workflow = TestWorkflow.objects.create(title="dispatch command")
        model_admin = admin.site._registry[TestIntentImport]
        request = RequestFactory().post("/", {"_generate_test_plan": "1"})
        expected = HttpResponse("queued", status=202)

        with patch.object(
            model_admin,
            "run_generate_test_plan",
            return_value=expected,
        ) as handler:
            response = model_admin.response_change(request, workflow)

        self.assertIs(response, expected)
        handler.assert_called_once_with(request, workflow)

    def test_generating_pages_show_persisted_progress_and_auto_refresh(self):
        profile = TestResourceProfile.objects.create(
            name="Progress profile",
            port_host="127.0.0.1",
            port_number=9000,
        )
        workflow = TestWorkflow.objects.create(
            title="正在生成测试计划",
            requirement_text="验证端口",
            resource_profile=profile,
            allowed_channels=["port"],
            status=TestWorkflow.Status.DESIGN_GENERATING,
            generation_progress={
                "phase": "calling_model",
                "message": "正在调用模型",
                "percent": 45,
            },
        )

        detail = self.client.get(
            reverse(
                "admin:test_platform_testintentimport_change",
                args=[workflow.pk],
            )
        )
        self.assertContains(detail, "正在调用模型")
        self.assertContains(detail, 'aria-valuenow="45"')
        self.assertContains(detail, "test_platform/generation_poll.js")

        source = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Progress source",
            test_categories=["port"],
            design_id="DESIGN-PROGRESS",
            version=1,
            content_hash="sha256:" + "1" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=source,
            resource_profile=profile,
            title="正在生成执行计划",
            test_categories=["port"],
            plan_id="PLAN-PROGRESS",
            version=1,
            content_hash="",
            status=ExecutionPlanArtifact.Status.GENERATING,
            generation_progress={
                "phase": "resolving_resources",
                "message": "正在解析并校验测试资源",
                "percent": 22,
            },
        )

        execution_detail = self.client.get(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[execution.pk],
            )
        )
        self.assertContains(execution_detail, "正在解析并校验测试资源")
        self.assertContains(execution_detail, 'aria-valuenow="22"')
        self.assertContains(execution_detail, "test_platform/generation_poll.js")

    def test_workflow_does_not_invent_coverage_without_user_fields(self):
        profile = TestResourceProfile.objects.create(
            name="API and port",
            api_openapi_file="openapi.yaml",
            api_base_url="http://127.0.0.1:9000",
            port_host="127.0.0.1",
            port_number=9000,
        )
        form = TestWorkflowForm(
            data={
                "requirement_text": "接口重复提交应幂等，端口应可连接",
                "resource_profile": profile.pk,
                "allowed_channels": ["api", "port"],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        workflow = form.save()
        self.assertEqual(workflow.coverage_by_category, {})
        self.assertNotIn("api_coverage", form.fields)
        self.assertNotIn("port_coverage", form.fields)
        selections = DesignSelections(
            techniques=["业务定义的端口边界验证"],
            techniques_by_channel={"port": ["业务定义的端口边界验证"]},
            allowed_channels=["port"],
            required_channels=["port"],
        )
        self.assertEqual(selections.techniques, ["业务定义的端口边界验证"])

    def test_compound_categories_are_inherited_by_plan_and_execution_history(self):
        profile = TestResourceProfile.objects.create(
            name="API and port",
            system_id="demo",
            environment="staging",
            api_openapi_file="openapi.yaml",
            api_base_url="http://127.0.0.1:9000",
            port_host="127.0.0.1",
            port_number=9000,
        )
        workflow = TestWorkflow.objects.create(
            title="复合测试",
            requirement_text="接口返回 200，且端口可连接",
            resource_profile=profile,
            allowed_channels=["api", "port"],
            coverage_by_category={"api": ["positive"], "port": ["positive"]},
        )
        plan = TestPlanArtifact.objects.create(
            source_intent=workflow,
            resource_profile=profile,
            title="复合测试",
            test_categories=["api", "port"],
            design_id="DESIGN-COMPOUND",
            version=1,
            content_hash="sha256:" + "2" * 64,
            generation_result={
                "design": {
                    "title": "复合测试",
                    "objective": {"text": "验证接口和端口"},
                    "selections": {"allowed_channels": ["api", "port"]},
                    "scenarios": [],
                }
            },
        )
        execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=plan,
            resource_profile=profile,
            title="复合测试",
            test_categories=["api", "port"],
            plan_id="PLAN-COMPOUND",
            version=1,
            content_hash="sha256:" + "3" * 64,
            compilation_result={
                "plan": {
                    "plan_id": "PLAN-COMPOUND",
                    "version": 1,
                    "target_system_id": "demo",
                    "target_environment": "staging",
                    "flows": [
                        {
                            "flow_id": "FLOW-0001",
                            "name": "组合流程",
                            "requirement_ids": ["REQ-1"],
                            "stages": [
                                {"stage_id": "STAGE-0001", "executor_kind": "http_api"},
                                {"stage_id": "STAGE-0002", "executor_kind": "tcp_port"},
                            ],
                        }
                    ],
                    "open_questions": [],
                },
                "artifacts": [],
            },
            artifact_root_ref="compound",
        )
        plan_html = self.client.get(
            reverse("admin:test_platform_testplanartifact_change", args=[plan.pk])
        ).content.decode("utf-8")
        self.assertIn("复合测试", plan_html)
        self.assertIn("测试分类", plan_html)
        execution_html = self.client.get(
            reverse(
                "admin:test_platform_executionplanartifact_change", args=[execution.pk]
            )
        ).content.decode("utf-8")
        self.assertIn("执行目标", execution_html)
        self.assertIn("接口测试", execution_html)
        self.assertIn("TCP 端口", execution_html)

        run = TestExecutionRun.objects.create(
            run_id="RUN-COMPOUND-001",
            status=TestExecutionRun.Status.FAILED,
            report_status=TestExecutionRun.ReportStatus.AVAILABLE,
            started_at="2026-07-21T00:00:00Z",
            storage_root_ref="compound",
            execution_plan=execution,
            result_summary={
                "categories": {
                    "api": {"total": 1, "passed": 1},
                    "port": {"total": 1, "failed": 1},
                }
            },
        )
        run_html = self.client.get(
            reverse("admin:test_platform_testexecutionrun_change", args=[run.pk])
        ).content.decode("utf-8")
        self.assertIn("各分类执行结果", run_html)
        self.assertIn("成功 1", run_html)
        self.assertIn("失败 1", run_html)

    def test_stage_lists_are_driven_by_persisted_artifacts_not_status(self):
        profile = TestResourceProfile.objects.create(
            name="Artifact chain",
            port_host="127.0.0.1",
            port_number=9000,
        )
        source_intent = TestWorkflow.objects.create(
            title="Source intent",
            resource_profile=profile,
            status=TestWorkflow.Status.DRAFT,
        )
        pending_plan = TestPlanArtifact.objects.create(
            source_intent=source_intent,
            resource_profile=profile,
            title="Pending plan",
            design_id="DESIGN-1",
            version=1,
            content_hash="sha256:" + "1" * 64,
            generation_result={"design": {"design_id": "DESIGN-1"}},
        )
        approved_without_execution = TestPlanArtifact.objects.create(
            resource_profile=profile,
            source_kind=TestPlanArtifact.SourceKind.IMPORTED,
            title="Imported approved plan",
            design_id="DESIGN-2",
            version=1,
            content_hash="sha256:" + "2" * 64,
            approved_bundle={"design": {"design_id": "DESIGN-2"}},
            status=TestPlanArtifact.Status.APPROVED,
        )
        approved_with_execution = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Approved source plan",
            design_id="DESIGN-3",
            version=1,
            content_hash="sha256:" + "3" * 64,
            approved_bundle={"design": {"design_id": "DESIGN-3"}},
            status=TestPlanArtifact.Status.APPROVED,
        )
        execution_review = ExecutionPlanArtifact.objects.create(
            source_test_plan=approved_with_execution,
            resource_profile=profile,
            title="Execution review",
            plan_id="PLAN-1",
            version=1,
            content_hash="sha256:" + "4" * 64,
            compilation_result={
                "plan": {"plan_id": "PLAN-1"},
                "validation": {"passed": True},
                "artifacts": [{"kind": "procedure_playwright"}],
            },
            artifact_root_ref="test-plans/TP-1/execution-v1",
        )
        execution_approved = ExecutionPlanArtifact.objects.create(
            source_test_plan=approved_with_execution,
            resource_profile=profile,
            title="Execution approved",
            plan_id="PLAN-2",
            version=1,
            content_hash="sha256:" + "5" * 64,
            runtime_config_hash="sha256:" + "6" * 64,
            approved_bundle={"plan": {"plan_id": "PLAN-2"}},
            artifact_root_ref="test-plans/TP-2/execution-v1",
            status=ExecutionPlanArtifact.Status.APPROVED,
        )

        intent_ids = set(
            admin.site._registry[TestIntentImport]
            .get_queryset(None)
            .values_list("pk", flat=True)
        )
        design_ids = set(
            admin.site._registry[TestPlanArtifact]
            .get_queryset(None)
            .values_list("pk", flat=True)
        )
        completed_design_ids = set(
            admin.site._registry[TestPlanArtifact]
            .get_queryset(
                RequestFactory().get("/", {"record_state": "completed"})
            )
            .values_list("pk", flat=True)
        )
        plan_ids = set(
            admin.site._registry[ExecutionPlanArtifact]
            .get_queryset(None)
            .values_list("pk", flat=True)
        )
        completed_plan_ids = set(
            admin.site._registry[ExecutionPlanArtifact]
            .get_queryset(
                RequestFactory().get("/", {"record_state": "completed"})
            )
            .values_list("pk", flat=True)
        )
        self.assertNotIn(source_intent.pk, intent_ids)
        self.assertIn(pending_plan.pk, design_ids)
        self.assertNotIn(approved_without_execution.pk, design_ids)
        self.assertNotIn(approved_with_execution.pk, design_ids)
        self.assertEqual(
            completed_design_ids,
            {approved_without_execution.pk, approved_with_execution.pk},
        )
        self.assertIn(execution_review.pk, plan_ids)
        self.assertNotIn(execution_approved.pk, plan_ids)
        self.assertEqual(completed_plan_ids, {execution_approved.pk})

        plan_list = self.client.get(
            reverse("admin:test_platform_testplanartifact_changelist"),
            {"record_state": "completed"},
        )
        self.assertContains(plan_list, "待处理")
        self.assertContains(plan_list, "已完成")
        self.assertContains(plan_list, "未通过")
        self.assertContains(plan_list, approved_without_execution.title)
        self.assertNotContains(plan_list, pending_plan.title)

        execution_list = self.client.get(
            reverse("admin:test_platform_executionplanartifact_changelist"),
            {"record_state": "completed"},
        )
        self.assertContains(execution_list, execution_approved.title)
        self.assertNotContains(execution_list, execution_review.title)

        completed_execution_page = self.client.get(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[execution_approved.pk],
            )
        )
        self.assertEqual(completed_execution_page.status_code, 200)
        self.assertContains(completed_execution_page, "已审批")
        self.assertContains(completed_execution_page, 'name="review_comments"')
        self.assertContains(completed_execution_page, 'name="_revise_execution_plan"')
        self.assertContains(completed_execution_page, 'name="_return_execution_plan"')

        response = self.client.get(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[execution_review.pk],
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "执行计划概览")
        self.assertNotContains(response, "审批重点")
        self.assertNotContains(response, "各分类生成的执行文件")
        self.assertContains(response, "审批通过")
        self.assertContains(response, "按修改需求重新生成当前计划")
        self.assertContains(response, "退回上一层")
        self.assertNotContains(response, "保存并继续编辑")

        response = self.client.get(
            reverse(
                "admin:test_platform_testplanartifact_change",
                args=[pending_plan.pk],
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "审批通过并生成执行计划")
        self.assertContains(response, "按修改需求重新生成当前计划")
        self.assertContains(response, "退回上一层")
        self.assertNotContains(response, 'name="action"')

    def test_execution_review_expands_real_artifacts_and_downloads_them(self):
        from openpyxl import Workbook

        profile = TestResourceProfile.objects.create(
            name="Review artifacts",
            port_host="127.0.0.1",
            port_number=9000,
        )
        source_plan = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Artifact source",
            design_id="DESIGN-DETAIL",
            version=1,
            content_hash="sha256:" + "a" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_root = root / "review-artifacts"
            api_stage = (
                artifact_root
                / "generated-files"
                / "api"
                / "PLAN-DETAIL"
                / "v1"
                / "FLOW-1"
                / "STAGE-API"
            )
            ui_stage = (
                artifact_root
                / "generated-files"
                / "ui"
                / "PLAN-DETAIL"
                / "v1"
                / "FLOW-1"
                / "STAGE-UI"
            )
            api_stage.mkdir(parents=True)
            ui_stage.mkdir(parents=True)
            (api_stage / "execution.json").write_text(
                json.dumps({"requests": [{"method": "GET", "path": "/health"}]}),
                encoding="utf-8",
            )
            (api_stage / "test_api_generated.py").write_text(
                "def test_generated_health():\n    assert True\n",
                encoding="utf-8",
            )
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Case"
            sheet.append(
                [
                    "Test Case ID",
                    "Test Case Name",
                    "Test Step ID",
                    "UR",
                    "Action",
                    "Input Data",
                    "Check",
                ]
            )
            sheet.append(
                ["FLOW-1", "登录", "ROW-1", "REQ-1", "提交登录", "账号=示例", "进入首页"]
            )
            workbook.save(ui_stage / "case.xlsx")

            execution = ExecutionPlanArtifact.objects.create(
                source_test_plan=source_plan,
                resource_profile=profile,
                title="Artifact review",
                plan_id="PLAN-DETAIL",
                version=1,
                content_hash="sha256:" + "b" * 64,
                status=ExecutionPlanArtifact.Status.REVIEW,
                artifact_root_ref="review-artifacts",
                compilation_result={
                    "plan": {
                        "plan_id": "PLAN-DETAIL",
                        "target_system_id": "demo",
                        "target_environment": "test",
                        "flows": [
                            {
                                "flow_id": "FLOW-1",
                                "stages": [
                                    {
                                        "stage_id": "STAGE-API",
                                        "execution": {
                                            "kind": "http_api",
                                            "requests": [
                                                {
                                                    "method": "GET",
                                                    "path": "/health",
                                                    "action": "读取健康状态",
                                                    "assertions": [
                                                        {
                                                            "statement": "接口应成功",
                                                            "kind": "status",
                                                            "operator": "equals",
                                                            "expected": 200,
                                                        }
                                                    ],
                                                }
                                            ],
                                        },
                                    },
                                    {
                                        "stage_id": "STAGE-UI",
                                        "execution": {
                                            "kind": "procedure_playwright",
                                            "rows": [
                                                {
                                                    "action": "提交登录",
                                                    "input_data": "测试账号",
                                                    "checkpoint": "进入首页",
                                                }
                                            ],
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                    "validation": {"passed": True, "findings": []},
                    "artifacts": [
                        {
                            "executor_kind": "http_api",
                            "flow_id": "FLOW-1",
                            "stage_id": "STAGE-API",
                            "artifact_refs": [
                                {
                                    "kind": "payload",
                                    "path_ref": "execution.json",
                                    "sha256": "sha256:" + "c" * 64,
                                },
                                {
                                    "kind": "pytest_source",
                                    "path_ref": "test_api_generated.py",
                                    "sha256": "sha256:" + "d" * 64,
                                },
                            ],
                        },
                        {
                            "executor_kind": "procedure_playwright",
                            "flow_id": "FLOW-1",
                            "stage_id": "STAGE-UI",
                            "artifact_refs": [
                                {
                                    "kind": "workbook",
                                    "path_ref": "case.xlsx",
                                    "sha256": "sha256:" + "e" * 64,
                                }
                            ],
                        },
                    ],
                },
            )
            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=root):
                response = self.client.get(
                    reverse(
                        "admin:test_platform_executionplanartifact_change",
                        args=[execution.pk],
                    )
                )
                self.assertContains(response, "查看执行配置")
                self.assertContains(response, "查看完整 pytest 代码")
                self.assertContains(response, "查看生成文件", count=2)
                self.assertContains(response, "def test_generated_health")
                self.assertContains(response, "查看 UI 测试步骤")
                self.assertContains(response, "提交登录")
                self.assertContains(response, "进入首页")
                self.assertContains(response, "关键执行逻辑", count=2)
                self.assertContains(response, "审批重点")
                self.assertContains(response, "实际请求或 SQL")
                self.assertNotContains(response, "最多 12 行")
                self.assertContains(response, "http_request")
                self.assertContains(response, "run_ui_procedure")
                self.assertContains(response, "GET /health")
                self.assertContains(response, "接口应成功")
                self.assertContains(response, "下载原始文件")

                download = self.client.get(
                    reverse(
                        "admin:test_platform_executionplanartifact_execution_artifact",
                        args=[
                            execution.pk,
                            "FLOW-1",
                            "STAGE-API",
                            "test_api_generated.py",
                        ],
                    )
                )
                self.assertEqual(download.status_code, 200)
                self.assertIn(
                    b"def test_generated_health",
                    b"".join(download.streaming_content),
                )
                download.close()

    def test_database_review_content_shows_ai_sql_origin_code_and_assertion(self):
        execution_admin = admin.site._registry[ExecutionPlanArtifact]

        rendered = execution_admin._stage_test_content(
            {
                "execution": {
                    "kind": "database",
                    "operations": [
                        {
                            "sql_origin": "ai_generated",
                            "sql": "SELECT locked FROM accounts WHERE id = :id",
                            "parameters_refs": {"id": "runtime.account_id"},
                            "assertions": [
                                {
                                    "statement": "账号应被锁定",
                                    "kind": "column",
                                    "column": "locked",
                                    "operator": "equals",
                                    "expected": True,
                                }
                            ],
                        }
                    ],
                }
            }
        )

        self.assertIn("AI 新生成，等待人工审批", str(rendered))
        self.assertIn("SELECT locked FROM accounts", str(rendered))
        self.assertIn("runtime.account_id", str(rendered))
        self.assertIn("账号应被锁定", str(rendered))

        key_code = execution_admin._stage_key_code(
            {
                "execution": {
                    "kind": "database",
                    "operations": [
                        {
                            "sql_origin": "ai_generated",
                            "sql": "SELECT locked FROM accounts WHERE id = :id",
                            "parameters_refs": {"id": "runtime.account_id"},
                            "assertions": [
                                {
                                    "kind": "column",
                                    "column": "locked",
                                    "operator": "equals",
                                    "expected": True,
                                }
                            ],
                        }
                    ],
                }
            }
        )
        self.assertIn("db.query", str(key_code))
        self.assertIn("SELECT locked FROM accounts", str(key_code))
        self.assertIn("result_1[&quot;locked&quot;] == true", str(key_code))

    def test_key_execution_preview_covers_all_key_performance_and_port_code(self):
        execution_admin = admin.site._registry[ExecutionPlanArtifact]
        performance = execution_admin._stage_key_code(
            {
                "execution": {
                    "kind": "performance",
                    "stages": [
                        {"virtual_users": index, "duration_seconds": 5}
                        for index in range(1, 15)
                    ],
                    "thresholds": [
                        {
                            "metric": "latency_ms",
                            "operator": "lte",
                            "value": 500,
                        }
                    ],
                }
            }
        )
        port = execution_admin._stage_key_code(
            {
                "execution": {
                    "kind": "tcp_port",
                    "probes": [
                        {
                            "host_ref": "runtime.port.host",
                            "port": 8007,
                            "timeout_seconds": 5,
                            "assertions": [
                                {
                                    "kind": "state",
                                    "operator": "equals",
                                    "expected": "open",
                                }
                            ],
                        }
                    ],
                }
            }
        )

        self.assertIn("run_load", str(performance))
        self.assertIn("load_14", str(performance))
        self.assertNotIn("最多 12 行", str(performance))
        self.assertNotIn("完整内容请展开下方生成文件", str(performance))
        self.assertIn("tcp_connect", str(port))
        self.assertIn("probe_1.state == &quot;open&quot;", str(port))


    def test_return_commands_require_review_comments(self):
        profile = TestResourceProfile.objects.create(
            name="Return guard",
            port_host="127.0.0.1",
            port_number=9000,
        )
        source_intent = TestWorkflow.objects.create(
            title="Return source",
            requirement_text="端口应可连接",
            resource_profile=profile,
        )
        plan = TestPlanArtifact.objects.create(
            source_intent=source_intent,
            resource_profile=profile,
            title="Return plan",
            design_id="DESIGN-RETURN",
            version=1,
            content_hash="sha256:" + "f" * 64,
            generation_result={"design": {"design_id": "DESIGN-RETURN"}},
            status=TestPlanArtifact.Status.REVIEW,
        )
        execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=plan,
            resource_profile=profile,
            title="Return execution",
            plan_id="PLAN-RETURN",
            version=1,
            content_hash="sha256:" + "1" * 64,
            artifact_root_ref="return-guard",
            status=ExecutionPlanArtifact.Status.REVIEW,
        )
        response = self.client.post(
            reverse("admin:test_platform_testplanartifact_change", args=[plan.pk]),
            {"review_comments": "", "_return_test_plan": "1"},
            follow=True,
        )
        plan.refresh_from_db()
        self.assertEqual(plan.status, TestPlanArtifact.Status.REVIEW)
        self.assertContains(response, "退回前请填写审批意见")

        response = self.client.post(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[execution.pk],
            ),
            {"review_comments": "", "_return_execution_plan": "1"},
            follow=True,
        )
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionPlanArtifact.Status.REVIEW)
        self.assertContains(response, "退回前请填写审批意见")

    def test_approval_comments_are_in_the_last_section(self):
        plan_admin = admin.site._registry[TestPlanArtifact]
        execution_admin = admin.site._registry[ExecutionPlanArtifact]

        self.assertEqual(plan_admin.fieldsets[-1][0], "审批处理")
        self.assertEqual(
            plan_admin.fieldsets[-1][1]["fields"],
            ("review_comments",),
        )
        self.assertEqual(execution_admin.fieldsets[-1][0], "审批处理")
        self.assertEqual(
            execution_admin.fieldsets[-1][1]["fields"],
            ("review_comments",),
        )

    def test_system_audit_records_are_summarized_and_downloadable(self):
        profile = TestResourceProfile.objects.create(
            name="Audit profile",
            port_host="127.0.0.1",
            port_number=9000,
        )
        plan = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Audit plan",
            design_id="DESIGN-AUDIT-1",
            version=2,
            content_hash="sha256:" + "a" * 64,
            generation_result={"design": {"marker": "RAW-DESIGN-MARKER"}},
            review_payload={"decision": "approved"},
            approved_bundle={"design": {"design_id": "DESIGN-AUDIT-1"}},
        )
        execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=plan,
            resource_profile=profile,
            title="Audit execution",
            plan_id="PLAN-AUDIT-1",
            version=1,
            content_hash="sha256:" + "b" * 64,
            catalog_snapshot={"catalog_id": "CATALOG-AUDIT-1"},
            compilation_result={"plan": {"marker": "RAW-PLAN-MARKER"}},
            artifact_root_ref="test-plans/audit/execution-v1",
        )
        approved_execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=plan,
            resource_profile=profile,
            title="Approved audit execution",
            plan_id="PLAN-AUDIT-APPROVED",
            version=1,
            content_hash="sha256:" + "c" * 64,
            runtime_config_hash="sha256:" + "d" * 64,
            catalog_snapshot={"catalog_id": "CATALOG-AUDIT-APPROVED"},
            compilation_result={"plan": {"plan_id": "PLAN-AUDIT-APPROVED"}},
            approved_bundle={"plan": {"plan_id": "PLAN-AUDIT-APPROVED"}},
            artifact_root_ref="test-plans/audit/approved-v1",
            status=ExecutionPlanArtifact.Status.APPROVED,
        )

        plan_page = self.client.get(
            reverse("admin:test_platform_testplanartifact_change", args=[plan.pk])
        )
        execution_page = self.client.get(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[execution.pk],
            )
        )

        self.assertNotContains(plan_page, "系统审计记录")
        self.assertNotContains(plan_page, "下层交接包")
        self.assertNotContains(plan_page, "RAW-DESIGN-MARKER")
        self.assertNotContains(execution_page, "资源目录快照")
        self.assertNotContains(execution_page, "执行协调器唯一接受的正式输入")
        self.assertNotContains(execution_page, "RAW-PLAN-MARKER")
        approved_execution_page = self.client.get(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[approved_execution.pk],
            )
        )
        self.assertEqual(approved_execution_page.status_code, 200)
        self.assertNotContains(approved_execution_page, "系统审计记录")
        self.assertNotContains(approved_execution_page, "执行交接包")

        download_url = reverse(
            "admin:test_platform_testplanartifact_audit_payload",
            args=[plan.pk, "generation_result"],
        )
        downloaded = self.client.get(download_url)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("attachment;", downloaded["Content-Disposition"])
        self.assertEqual(
            json.loads(downloaded.content),
            plan.generation_result,
        )
        invalid = self.client.get(
            reverse(
                "admin:test_platform_testplanartifact_audit_payload",
                args=[plan.pk, "approved_bundle"],
            ).replace("approved_bundle", "unknown_payload")
        )
        self.assertEqual(invalid.status_code, 404)

    def test_admin_json_serializes_nested_model_artifacts(self):
        class Artifact:
            def model_dump(self, *, mode):
                return {"artifact_id": "ART-1"}

        self.assertEqual(
            _json({"artifacts": [Artifact()]}),
            {"artifacts": [{"artifact_id": "ART-1"}]},
        )

    def test_plan_artifact_generation_lock_rejects_a_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with _artifact_generation_lock(root, "WF-LOCK"):
                with self.assertRaisesRegex(ValidationError, "正在生成执行计划"):
                    with _artifact_generation_lock(root, "WF-LOCK"):
                        pass

    def test_approved_test_plan_import_is_validated_and_idempotent(self):
        profile = TestResourceProfile(
            name="Imported account test",
            system_id="account-web",
            environment="staging",
            ui_procedure_database="account-assets.sqlite",
            database_query_file="queries.json",
            database_connection_ref="account-readonly",
        )
        profile.full_clean()
        profile.save()
        bundle = _approved_bundle()

        first = import_approved_test_plan(
            bundle.model_dump(mode="json"),
            profile,
        )
        second = import_approved_test_plan(
            bundle.model_dump(mode="json"),
            profile,
        )

        self.assertEqual(first.pk, second.pk)
        self.assertIsNone(first.source_intent)
        self.assertEqual(first.source_kind, TestPlanArtifact.SourceKind.IMPORTED)
        self.assertEqual(first.status, TestPlanArtifact.Status.APPROVED)
        self.assertTrue(first.approved_bundle)

    def test_failed_intent_remains_visible_and_can_be_regenerated(self):
        profile = TestResourceProfile.objects.create(
            name="Failed intent resource",
            port_host="127.0.0.1",
            port_number=9000,
        )
        intent = TestIntentImport.objects.create(
            title="模型输出失败但可恢复",
            requirement_text="端口应可连接",
            resource_profile=profile,
            allowed_channels=["port"],
            coverage_by_category={"port": ["positive"]},
            status=TestWorkflow.Status.ERROR,
            last_error="模型输出不符合严格候选 schema",
        )

        listing = self.client.get(
            reverse("admin:test_platform_testintentimport_changelist")
        )
        detail = self.client.get(
            reverse("admin:test_platform_testintentimport_change", args=[intent.pk])
        )

        self.assertContains(listing, "模型输出失败但可恢复")
        self.assertContains(detail, "模型输出不符合严格候选 schema")
        self.assertContains(detail, "生成测试计划")

    def test_admin_can_import_an_approved_test_plan_bundle(self):
        profile = TestResourceProfile.objects.create(
            name="Imported account test",
            system_id="account-web",
            environment="staging",
            ui_procedure_database="account-assets.sqlite",
            database_query_file="queries.json",
            database_connection_ref="account-readonly",
        )
        payload = json.dumps(
            _approved_bundle().model_dump(mode="json"),
            ensure_ascii=False,
        ).encode("utf-8")
        import_url = reverse("admin:test_platform_testplanartifact_import")
        import_page = self.client.get(import_url)
        self.assertContains(import_page, "测试计划文件")
        self.assertContains(import_page, "下载完整 JSON 示例")
        self.assertNotContains(import_page, "显示名称")
        self.assertNotContains(import_page, "test-design.v4")

        response = self.client.post(
            import_url,
            {
                "resource_profile": profile.pk,
                "artifact_file": SimpleUploadedFile(
                    "approved-test-plan.json",
                    payload,
                    content_type="application/json",
                ),
            },
        )

        artifact = TestPlanArtifact.objects.get(source_kind=TestPlanArtifact.SourceKind.IMPORTED)
        self.assertRedirects(
            response,
            reverse("admin:test_platform_testplanartifact_change", args=[artifact.pk]),
        )
        self.assertEqual(artifact.status, TestPlanArtifact.Status.APPROVED)

    def test_workflow_form_rejects_channel_missing_from_profile_catalog(self):
        profile = TestResourceProfile(
            name="Local TCP",
            port_host="127.0.0.1",
            port_number=9000,
        )
        profile.full_clean()
        profile.save()
        form = TestWorkflowForm(
            data={
                "title": "Unsupported UI",
                "requirement_text": "验证首页",
                "request_id": "REQ-UI-1",
                "resource_profile": profile.pk,
                "techniques": ["positive"],
                "allowed_channels": ["ui"],
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("测试资源配置不支持所选测试分类: UI 页面测试", str(form.errors))

    def test_resource_admin_hides_internal_catalog_and_runtime_fields(self):
        model_admin = admin.site._registry[TestResourceProfile]
        fieldsets = model_admin.get_fieldsets(None)
        visible = {
            field
            for _, options in fieldsets
            for field in options["fields"]
        }
        self.assertIn("ui_procedure_database", visible)
        self.assertNotIn("ui_procedure_api_url", visible)
        self.assertNotIn("ui_procedure_site", visible)
        self.assertIn("api_openapi_file", visible)
        self.assertIn("api_asset_text", visible)
        self.assertIn("database_query_file", visible)
        self.assertIn("database_asset_text", visible)
        self.assertIn("performance_profile_file", visible)
        self.assertIn("performance_asset_text", visible)
        self.assertIn("port_host", visible)
        self.assertNotIn("catalog_snapshot", visible)
        self.assertNotIn("runtime_config", visible)
        self.assertIn("system_id", visible)
        self.assertNotIn("environment", visible)
        typed_sections = {
            title: set(options.get("classes", ()))
            for title, options in fieldsets
            if "resource-section" in options.get("classes", ())
        }
        self.assertEqual(
            set(typed_sections),
            {
                "UI 页面测试资源",
                "接口测试资源",
                "数据库测试资源",
                "性能/压力测试资源",
                "TCP 端口测试资源",
            },
        )
        self.assertIn("resource-api", typed_sections["接口测试资源"])

    def test_one_resource_profile_can_hold_compound_test_capabilities(self):
        form = TestResourceProfileForm(
            data={
                "name": "复合测试总资源",
                "system_id": "compound-system",
                "environment": "staging",
                "resource_types": ["ui", "port"],
                "port_host": "127.0.0.1",
                "port_number": 8000,
                "enabled": True,
            },
            files={
                "ui_procedure_database": SimpleUploadedFile(
                    "compound.sqlite",
                    b"sqlite-test-fixture",
                    content_type="application/vnd.sqlite3",
                )
            },
        )

        self.assertTrue(form.is_valid(), form.errors)
        profile = form.save()
        self.assertEqual(profile.configured_channels(), {"ui", "port"})

    def test_approved_execution_plan_offers_regeneration_and_return(self):
        model_admin = admin.site._registry[ExecutionPlanArtifact]
        artifact = SimpleNamespace(status=ExecutionPlanArtifact.Status.APPROVED)

        commands = model_admin.get_record_commands(None, artifact)

        self.assertEqual(
            [item["name"] for item in commands],
            ["_revise_execution_plan", "_return_execution_plan"],
        )

    def test_blocked_execution_plan_has_no_approve_command_and_shows_findings(self):
        model_admin = admin.site._registry[ExecutionPlanArtifact]
        blocked = SimpleNamespace(
            status=ExecutionPlanArtifact.Status.BLOCKED,
            compilation_result={
                "validation": {
                    "passed": False,
                    "findings": [
                        {
                            "rule_id": "FLOWS_REQUIRED",
                            "field_path": "flows",
                            "message": "计划至少需要一个 flow",
                            "blocking": True,
                        }
                    ],
                }
            },
        )

        commands = model_admin.get_record_commands(None, blocked)
        summary = str(model_admin.validation_summary(blocked))

        self.assertNotIn("_approve_execution_plan", [item["name"] for item in commands])
        self.assertNotIn("FLOWS_REQUIRED", summary)
        self.assertIn("计划至少需要一个 flow", summary)

    def test_execution_plan_shows_selected_procedure_asset_identity(self):
        model_admin = admin.site._registry[ExecutionPlanArtifact]
        artifact = SimpleNamespace(
            compilation_result={
                "plan": {
                    "flows": [{
                        "stages": [{
                            "execution": {
                                "kind": "procedure_playwright",
                                "procedure_refs": [],
                                "rows": [{"operation_ref": "procedure.login.submit"}],
                            }
                        }]
                    }]
                }
            },
            catalog_snapshot={
                "procedure_profiles": [{
                    "profile_ref": "procedure.account",
                    "site": "account.example.test",
                    "library_id": "site.account.example.test",
                    "library_hash": "sha256:" + "a" * 64,
                    "operations": [{
                        "operation_ref": "procedure.login.submit",
                        "procedure_id": "account.login",
                        "procedure_version": 7,
                        "action": "提交登录并检查结果",
                    }],
                }]
            }
        )

        summary = str(model_admin.page_execution_basis(artifact))

        self.assertIn("account.example.test", summary)
        self.assertIn("site.account.example.test", summary)
        self.assertIn("account.login@v7", summary)
        self.assertIn("提交登录并检查结果", summary)
        self.assertNotIn("procedure.login.submit", summary)
        self.assertNotIn("procedure_version", summary)

    def test_resource_form_accepts_unstructured_performance_material(self):
        form = TestResourceProfileForm(
            data={
                "name": "Invalid performance",
                "system_id": "demo",
                "environment": "test",
                "resource_types": ["performance"],
                "enabled": True,
            },
            files={
                "performance_profile_file": SimpleUploadedFile(
                    "performance.json", b"not-json"
                )
            },
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_resource_form_rejects_secret_and_ambiguous_duplicate_sources(self):
        secret_form = TestResourceProfileForm(
            data={
                "name": "Secret API material",
                "system_id": "demo",
                "environment": "test",
                "resource_types": ["api"],
                "api_asset_text": "GET /health Authorization: Bearer actual-secret-token",
                "api_base_url": "http://127.0.0.1:9000",
                "enabled": True,
            }
        )
        self.assertFalse(secret_form.is_valid())
        self.assertIn("疑似凭据", str(secret_form.errors))

        duplicate_form = TestResourceProfileForm(
            data={
                "name": "Duplicate API material",
                "system_id": "demo",
                "environment": "test",
                "resource_types": ["api"],
                "api_asset_text": "GET /health",
                "api_base_url": "http://127.0.0.1:9000",
                "enabled": True,
            },
            files={
                "api_openapi_file": SimpleUploadedFile(
                    "api.md",
                    b"GET /health",
                )
            },
        )
        self.assertFalse(duplicate_form.is_valid())
        self.assertIn("文件和文字说明选择一种", str(duplicate_form.errors))

    def test_uploaded_requirement_is_served_only_when_registered(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            profile = TestResourceProfile.objects.create(
                name="Upload resource",
                port_host="127.0.0.1",
                port_number=9000,
            )
            workflow = TestWorkflow.objects.create(
                title="Protected requirement",
                requirement_file=SimpleUploadedFile("requirement.md", b"private requirement"),
                resource_profile=profile,
                allowed_channels=["port"],
            )
            url = workflow.requirement_file.url

            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"private requirement", b"".join(response.streaming_content))
            response.close()

    def test_review_previews_show_business_fields_and_escape_model_text(self):
        model_admin = admin.site._registry[TestIntentImport]
        workflow = SimpleNamespace(
            approved_design_bundle={},
            design_generation={
                "design": {
                    "title": "登录 <script>alert(1)</script>",
                    "objective": {"text": "验证登录"},
                    "scenarios": [
                        {
                            "scenario_id": "SCN-1",
                            "title": "正常登录",
                            "techniques": ["positive"],
                            "operations": [
                                {"channel_hint": "ui", "text": "提交账号密码"}
                            ],
                            "expected_results": [
                                {
                                    "channel_hint": "ui",
                                    "text": "进入首页",
                                    "operator": "equals",
                                    "expected": True,
                                }
                            ],
                            "state_impact": {"impact": "read_only"},
                        }
                    ],
                    "open_questions": [],
                }
            },
            plan_compilation={
                "plan": {
                    "plan_id": "PLAN-1",
                    "version": 1,
                    "target_system_id": "account-web",
                    "target_environment": "test",
                    "flows": [
                        {
                            "flow_id": "FLOW-1",
                            "name": "正常登录",
                            "requirement_ids": ["REQ-1"],
                            "stages": [
                                {
                                    "order": 1,
                                    "executor_kind": "procedure_playwright",
                                    "execution": {
                                        "kind": "procedure_playwright",
                                        "procedure_refs": ["local.login@v2"],
                                        "rows": [
                                            {"operation_ref": "procedure.login.submit"}
                                        ],
                                    },
                                }
                            ],
                            "cleanup": None,
                        }
                    ],
                    "open_questions": [],
                }
            },
        )
        design_html = str(model_admin.design_review_preview(workflow))
        plan_html = str(model_admin.plan_review_preview(workflow))
        self.assertIn("正常登录", design_html)
        self.assertIn("提交账号密码", design_html)
        self.assertIn("进入首页", design_html)
        self.assertNotIn("<script>", design_html)
        self.assertIn("tb-review-summary", design_html)
        self.assertIn("测试分类", design_html)
        self.assertIn("覆盖方式", design_html)
        self.assertNotIn(">技术<", design_html)
        self.assertIn("tb-plan-notes", design_html)
        self.assertNotIn("FLOW-1", plan_html)
        self.assertNotIn("版本", plan_html)
        self.assertIn("procedure.login.submit", plan_html)
        self.assertIn("local.login@v2", plan_html)
        self.assertIn("tb-flow-stage", plan_html)

    def test_resource_profile_generates_internal_identity_and_runtime_context(self):
        self.assertTrue(admin.site.is_registered(TestResourceProfile))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = TestResourceProfile(
                name="Local TCP",
                port_host="127.0.0.1",
                port_number=9000,
            )
            profile.full_clean()
            profile.save()
            self.assertEqual(profile.system_id, profile.profile_id.lower())
            self.assertEqual(profile.environment, "test")
            self.assertEqual(profile.configured_channels(), {"port"})

            with override_settings(
                TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY="",
                TEST_PLATFORM_RUNTIME_CONTEXT_JSON=json.dumps(
                    {
                        "variables": {"from_process": "kept"},
                    }
                ),
            ):
                context = get_runtime_context(
                    evidence_dir=root / "evidence",
                    runtime_config={
                        "network_hosts": {"runtime.port.host": profile.port_host}
                    },
                )

            self.assertEqual(context.network_hosts["runtime.port.host"], "127.0.0.1")
            self.assertEqual(context.variables["from_process"], "kept")

    def test_runtime_execution_input_merges_only_at_execution_boundary(self):
        from apps.test_platform.service_factory import get_runtime_context
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            with override_settings(TEST_PLATFORM_RUNTIME_CONTEXT_JSON='{"variables":{"base":"kept"}}'):
                context = get_runtime_context(
                    evidence_dir=Path(temp) / "evidence",
                    execution_input={
                        "schema_version": "test-runtime-input.v1",
                        "variables": {"account_id": "ACCOUNT-1"},
                        "performance_mode": "live",
                    },
                )

        self.assertEqual(context.variables, {"base": "kept", "account_id": "ACCOUNT-1"})
        self.assertEqual(context.performance_mode, "live")

    def test_test_plan_input_form_accepts_line_variables_and_rejects_secrets(self):
        instance = TestPlanArtifact(test_categories=["port"])
        valid = TestPlanExecutionInputForm(
            data={
                "runtime_variables": "account_id=A-1\ncount=3",
            },
            instance=instance,
        )
        self.assertTrue(valid.is_valid(), valid.errors)
        self.assertNotIn("performance_mode", valid.fields)
        self.assertEqual(
            valid.cleaned_data["runtime_variables"],
            {"account_id": "A-1", "count": 3},
        )
        self.assertEqual(
            valid.execution_input(),
            {
                "schema_version": "test-runtime-input.v1",
                "variables": {"account_id": "A-1", "count": 3},
            },
        )

        invalid_values = (
            ("missing-separator", "名称=值"),
            ("api_token=secret-value", "秘密值"),
        )
        for raw, message in invalid_values:
            with self.subTest(raw=raw):
                form = TestPlanExecutionInputForm(
                    data={"runtime_variables": raw},
                    instance=instance,
                )
                self.assertFalse(form.is_valid())
                self.assertIn(message, str(form.errors["runtime_variables"]))

    def test_resource_profile_rejects_incomplete_pairs(self):
        incomplete = TestResourceProfile(
            name="Incomplete API",
            api_base_url="http://127.0.0.1:9000",
        )
        with self.assertRaisesRegex(ValidationError, "接口资料和 API 基础地址必须同时配置"):
            incomplete.full_clean()

    def test_admin_composition_registers_real_procedure_runner(self):
        workflow = get_workflow()
        self.assertIn("procedure_playwright", workflow.coordinator.registry.registered_kinds)

    def test_model_gateway_is_lazy_but_generation_requires_credentials(self):
        gateway = OpenAICompatibleModelGateway(
            api_key="",
            base_url=None,
            model="",
            timeout=1,
        )
        self.assertIsNone(gateway.client)
        with self.assertRaisesRegex(RuntimeError, "TEST_PLATFORM_LLM_API_KEY"):
            gateway.generate([], object)

    def test_model_gateway_requests_strict_json_schema(self):
        class Output(BaseModel):
            model_config = ConfigDict(extra="forbid")
            value: int

        captured = {}

        class Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content='{"value": 1}')
                        )
                    ]
                )

        gateway = OpenAICompatibleModelGateway(
            api_key="test-key",
            base_url=None,
            model="test-model",
            timeout=1,
        )
        gateway.client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )

        result = gateway.generate([], Output)

        self.assertEqual(result.value, 1)
        self.assertEqual(captured["response_format"]["type"], "json_schema")
        self.assertTrue(captured["response_format"]["json_schema"]["strict"])
        self.assertFalse(
            captured["response_format"]["json_schema"]["schema"][
                "additionalProperties"
            ]
        )

    def test_report_stays_under_recorded_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_dir = root / "runs" / "RUN-ADMIN-1" / "reports"
            report_dir.mkdir(parents=True)
            (report_dir / "report.html").write_text("<h1>ok</h1>", encoding="utf-8")
            record = TestExecutionRun.objects.create(
                run_id="RUN-ADMIN-1",
                status=TestExecutionRun.Status.PASSED,
                report_status=TestExecutionRun.ReportStatus.AVAILABLE,
                started_at="2026-07-20T00:00:00Z",
                storage_root_ref="runs/RUN-ADMIN-1",
                report_paths={"html": "reports/report.html"},
            )
            url = reverse(
                "test_platform_report",
                kwargs={"run_id": record.run_id, "kind": "html"},
            )
            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=root):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response["Content-Security-Policy"],
                    "sandbox allow-top-navigation-by-user-activation",
                )
                response.close()

    def test_report_downloads_only_registered_evidence_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_root = root / "runs" / "RUN-DOWNLOAD-1"
            evidence_dir = run_root / "evidence"
            artifact_dir = run_root / "plan" / "v1" / "FLOW-1" / "STAGE-1"
            report_dir = run_root / "reports"
            evidence_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            report_dir.mkdir(parents=True)
            (evidence_dir / "allowed.json").write_text("{}", encoding="utf-8")
            (evidence_dir / "hidden.json").write_text("{}", encoding="utf-8")
            (artifact_dir / "execution.json").write_text("{}", encoding="utf-8")
            report_payload = {
                "run_id": "RUN-DOWNLOAD-1",
                "evidence": ["allowed.json"],
                "flows": [
                    {
                        "stages": [
                            {
                                "artifacts": [
                                    {
                                        "artifact_path_ref": (
                                            "plan/v1/FLOW-1/STAGE-1/execution.json"
                                        )
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
            (report_dir / "report.json").write_text(
                json.dumps(report_payload), encoding="utf-8"
            )
            TestExecutionRun.objects.create(
                run_id="RUN-DOWNLOAD-1",
                status=TestExecutionRun.Status.PASSED,
                report_status=TestExecutionRun.ReportStatus.AVAILABLE,
                started_at="2026-07-20T00:00:00Z",
                storage_root_ref="runs/RUN-DOWNLOAD-1",
                report_paths={"json": "reports/report.json"},
            )
            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=root):
                evidence = self.client.get(
                    reverse(
                        "test_platform_evidence",
                        kwargs={"run_id": "RUN-DOWNLOAD-1", "name": "allowed.json"},
                    )
                )
                self.assertEqual(evidence.status_code, 200)
                evidence.close()
                hidden = self.client.get(
                    reverse(
                        "test_platform_evidence",
                        kwargs={"run_id": "RUN-DOWNLOAD-1", "name": "hidden.json"},
                    )
                )
                self.assertEqual(hidden.status_code, 404)
                artifact = self.client.get(
                    reverse(
                        "test_platform_artifact",
                        kwargs={
                            "run_id": "RUN-DOWNLOAD-1",
                            "path_ref": "plan/v1/FLOW-1/STAGE-1/execution.json",
                        },
                    )
                )
                self.assertEqual(artifact.status_code, 200)
                artifact.close()

    def test_workflow_form_rejects_declared_large_upload_before_ingestion(self):
        profile = TestResourceProfile.objects.create(
            name="Port upload limit",
            port_host="127.0.0.1",
            port_number=9000,
        )
        uploaded = SimpleUploadedFile("requirement.md", b"small")
        uploaded.size = 20 * 1024 * 1024 + 1
        form = TestWorkflowForm(
            data={
                "title": "oversized",
                "allowed_channels": ["port"],
                "port_coverage": ["positive"],
                "resource_profile": profile.pk,
            },
            files={"requirement_file": uploaded},
        )

        self.assertFalse(form.is_valid())
        self.assertIn("单个文件不能超过", form.errors["requirement_file"][0])


class TestExecutionHistoryAdminTests(TestCase):
    def setUp(self):
        self.profile = TestResourceProfile.objects.create(
            name="历史记录复合资源",
            port_host="127.0.0.1",
            port_number=9000,
        )
        self.intent = TestWorkflow.objects.create(
            title="登录服务容量需求",
            requirement_text="验证压力表现和服务端口连通性",
            resource_profile=self.profile,
            allowed_channels=["performance", "port"],
            coverage_by_category={
                "performance": ["positive"],
                "port": ["negative"],
            },
            status=TestWorkflow.Status.DESIGN_APPROVED,
        )
        self.test_plan = TestPlanArtifact.objects.create(
            source_intent=self.intent,
            resource_profile=self.profile,
            title="登录服务测试计划",
            test_categories=["performance", "port"],
            design_id="DESIGN-HISTORY-1",
            version=1,
            content_hash="sha256:" + "a" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        self.execution_plan = ExecutionPlanArtifact.objects.create(
            source_test_plan=self.test_plan,
            resource_profile=self.profile,
            title="登录服务执行计划",
            test_categories=["performance", "port"],
            plan_id="PLAN-HISTORY-1",
            version=1,
            content_hash="sha256:" + "b" * 64,
            runtime_config_hash="sha256:" + "c" * 64,
            artifact_root_ref="history/plan-1",
            status=ExecutionPlanArtifact.Status.APPROVED,
        )

    def _run(
        self,
        run_id,
        status,
        *,
        report=True,
        execution_plan=None,
        started_at=None,
    ):
        return TestExecutionRun.objects.create(
            run_id=run_id,
            status=status,
            report_status=(
                TestExecutionRun.ReportStatus.AVAILABLE
                if report
                else TestExecutionRun.ReportStatus.FAILED
            ),
            execution_plan=execution_plan or self.execution_plan,
            resource_profile=self.profile,
            started_at=started_at or timezone.now(),
            finished_at=timezone.now(),
            storage_root_ref=f"history/{run_id}",
            report_paths={"html": "reports/report.html"} if report else {},
        )

    def test_imported_run_is_labeled_manual_and_actions_are_separate_columns(self):
        imported_plan = TestPlanArtifact.objects.create(
            resource_profile=self.profile,
            source_kind=TestPlanArtifact.SourceKind.IMPORTED,
            title="手动导入测试计划",
            test_categories=["port"],
            design_id="DESIGN-HISTORY-IMPORTED",
            version=1,
            content_hash="sha256:" + "d" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        imported_execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=imported_plan,
            resource_profile=self.profile,
            title="手动导入执行计划",
            test_categories=["port"],
            plan_id="PLAN-HISTORY-IMPORTED",
            version=1,
            content_hash="sha256:" + "e" * 64,
            runtime_config_hash="sha256:" + "f" * 64,
            artifact_root_ref="history/imported",
            status=ExecutionPlanArtifact.Status.APPROVED,
        )
        self._run(
            "RUN-HISTORY-IMPORTED",
            TestExecutionRun.Status.FAILED,
            execution_plan=imported_execution,
        )

        response = self.client.get(
            reverse("admin:test_platform_testexecutionrun_changelist")
        )

        self.assertContains(response, "手动导入")
        html = response.content.decode("utf-8")
        self.assertIn('class="field-quick_report_link"', html)
        self.assertIn('class="field-retry_link"', html)
        self.assertIn("tb-source-kind--imported", html)

    def test_date_range_filters_closed_open_and_invalid_ranges(self):
        day_one = timezone.make_aware(datetime(2026, 7, 20, 8, 15))
        day_two = timezone.make_aware(datetime(2026, 7, 21, 9, 30))
        first = self._run(
            "RUN-HISTORY-DATE-ONE",
            TestExecutionRun.Status.PASSED,
            started_at=day_one,
        )
        second = self._run(
            "RUN-HISTORY-DATE-TWO",
            TestExecutionRun.Status.PASSED,
            started_at=day_two,
        )
        url = reverse("admin:test_platform_testexecutionrun_changelist")

        selected = self.client.get(
            url,
            {"date_from": "2026-07-20", "date_to": "2026-07-20"},
        )
        self.assertEqual(selected.status_code, 200)
        self.assertContains(selected, 'type="date"', count=2)
        self.assertContains(selected, 'name="date_from" value="2026-07-20"')
        self.assertContains(selected, 'name="date_to" value="2026-07-20"')
        self.assertContains(selected, first.run_id)
        self.assertNotContains(selected, second.run_id)
        self.assertContains(selected, "清除日期")

        from_only = self.client.get(url, {"date_from": "2026-07-21"})
        self.assertNotContains(from_only, first.run_id)
        self.assertContains(from_only, second.run_id)

        to_only = self.client.get(url, {"date_to": "2026-07-20"})
        self.assertContains(to_only, first.run_id)
        self.assertNotContains(to_only, second.run_id)

        reversed_range = self.client.get(
            url,
            {"date_from": "2026-07-21", "date_to": "2026-07-20"},
        )
        self.assertEqual(reversed_range.status_code, 200)
        self.assertContains(reversed_range, "开始日期不能晚于结束日期")
        self.assertContains(reversed_range, first.run_id)
        self.assertContains(reversed_range, second.run_id)

    def test_history_table_shows_result_domain_sources_report_and_retry(self):
        failed = self._run("RUN-HISTORY-FAILED", TestExecutionRun.Status.FAILED)
        passed = self._run("RUN-HISTORY-PASSED", TestExecutionRun.Status.PASSED)

        response = self.client.get(
            reverse("admin:test_platform_testexecutionrun_changelist")
        )

        self.assertEqual(response.status_code, 200)
        for text in (
            failed.run_id,
            passed.run_id,
            "性能/压力测试",
            "TCP 端口测试",
            "失败",
            "成功",
            "查看报告",
            self.test_plan.title,
            self.intent.title,
            "重试",
        ):
            self.assertContains(response, text)
        self.assertNotContains(response, "报告状态")
        html = response.content.decode("utf-8")
        self.assertIn(
            reverse("admin:test_platform_testplanartifact_change", args=[self.test_plan.pk]),
            html,
        )
        self.assertIn(
            reverse("admin:test_platform_testintentimport_change", args=[self.intent.pk]),
            html,
        )
        self.assertEqual(
            self.client.get(
                reverse("admin:test_platform_testplanartifact_change", args=[self.test_plan.pk])
            ).status_code,
            200,
        )

    def test_history_uses_merged_status_filter_and_detail_field(self):
        failed = self._run("RUN-HISTORY-ERROR", TestExecutionRun.Status.ERROR)
        self._run("RUN-HISTORY-PASSED-FILTER", TestExecutionRun.Status.PASSED)
        list_url = reverse("admin:test_platform_testexecutionrun_changelist")

        response = self.client.get(list_url, {"result_state": "failed"})

        self.assertContains(response, failed.run_id)
        self.assertNotContains(response, "RUN-HISTORY-PASSED-FILTER")
        for label in ("等待中", "执行中", "成功", "失败", "阻断", "预检"):
            self.assertContains(response, label)
        self.assertNotContains(response, "报告状态")

        detail = self.client.get(
            reverse("admin:test_platform_testexecutionrun_change", args=[failed.pk])
        )
        self.assertContains(detail, "执行状态")
        self.assertContains(detail, "失败")
        self.assertNotContains(detail, "报告状态")
        self.assertEqual(
            self.client.get(
                reverse("admin:test_platform_testintentimport_change", args=[self.intent.pk])
            ).status_code,
            200,
        )

    def test_failed_run_retry_reuses_the_frozen_execution_plan_input(self):
        failed = self._run("RUN-HISTORY-RETRY", TestExecutionRun.Status.ERROR, report=False)
        retry_url = reverse(
            "admin:test_platform_testexecutionrun_retry", args=[failed.pk]
        )

        self.assertEqual(self.client.get(retry_url).status_code, 200)
        with patch(
            "apps.test_platform.execution_service.queue_execution_plan_artifact",
            return_value=SimpleNamespace(run_id="RUN-HISTORY-RETRY-NEW"),
        ) as queue:
            response = self.client.post(retry_url, {})

        self.assertRedirects(
            response,
            reverse("admin:test_platform_testexecutionrun_changelist"),
        )
        queue.assert_called_once_with(self.execution_plan)

    def test_retry_rejects_passed_run_and_offers_return_to_execution_plan(self):
        passed = self._run("RUN-HISTORY-NO-RETRY", TestExecutionRun.Status.PASSED)
        failed = self._run("RUN-HISTORY-SECRET", TestExecutionRun.Status.FAILED)
        passed_url = reverse(
            "admin:test_platform_testexecutionrun_retry", args=[passed.pk]
        )
        failed_url = reverse(
            "admin:test_platform_testexecutionrun_retry", args=[failed.pk]
        )

        with patch(
            "apps.test_platform.execution_service.queue_execution_plan_artifact"
        ) as queue:
            passed_response = self.client.post(passed_url, {})
            failed_page = self.client.get(failed_url)

        self.assertRedirects(
            passed_response,
            reverse("admin:test_platform_testexecutionrun_changelist"),
        )
        self.assertEqual(failed_page.status_code, 200)
        self.assertContains(failed_page, "按原执行计划重试")
        self.assertContains(failed_page, "返回执行计划修改")
        self.assertNotContains(failed_page, "runtime_variables_json")
        queue.assert_not_called()


__all__ = ["TestExecutionHistoryAdminTests", "TestWorkflowAdminTests"]
