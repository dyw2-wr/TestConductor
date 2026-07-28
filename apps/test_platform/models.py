from hashlib import sha256
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class TestCategory(models.TextChoices):
    """Product-facing test categories shared by every workflow artifact."""

    UI = "ui", "UI 页面测试"
    API = "api", "接口测试"
    DATABASE = "database", "数据库测试"
    PERFORMANCE = "performance", "性能/压力测试"
    PORT = "port", "TCP 端口测试"


def _workflow_id() -> str:
    return f"WF-{uuid4().hex.upper()}"


def _resource_profile_id() -> str:
    return f"PROFILE-{uuid4().hex.upper()}"


def _test_plan_artifact_id() -> str:
    return f"TP-{uuid4().hex.upper()}"


def _execution_plan_artifact_id() -> str:
    return f"EP-{uuid4().hex.upper()}"


def _knowledge_id() -> str:
    return f"knowledge-{uuid4().hex}"


def _knowledge_scope_id() -> str:
    return f"scope-{uuid4().hex}"


class ApprovedKnowledgeEntry(models.Model):
    """Locally managed business knowledge; only approved rows enter test design."""

    class Status(models.TextChoices):
        DRAFT = "draft", "待审核"
        APPROVED = "approved", "已发布"

    system_id = models.CharField(max_length=128, verbose_name="所属系统")
    title = models.CharField(max_length=200, verbose_name="知识标题")
    content = models.TextField(verbose_name="知识内容")
    source_file = models.FileField(
        upload_to="test_platform/knowledge/%Y/%m/",
        blank=True,
        verbose_name="导入文件（可选）",
        help_text="可上传 Word、PDF、Markdown、TXT、Excel、JSON 等常见文档；系统会提取文字供审核。",
    )
    scope_id = models.CharField(
        max_length=128,
        unique=True,
        default=_knowledge_scope_id,
        editable=False,
    )
    knowledge_id = models.CharField(
        max_length=128,
        unique=True,
        default=_knowledge_id,
        editable=False,
    )
    version = models.PositiveIntegerField(default=1, editable=False)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        editable=False,
        verbose_name="发布状态",
    )
    approval_id = models.CharField(max_length=128, blank=True, editable=False)
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    content_hash = models.CharField(max_length=71, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "业务知识"
        verbose_name_plural = "业务知识库"
        ordering = ("system_id", "title", "id")

    def clean(self):
        super().clean()
        self.system_id = str(self.system_id or "").strip()
        self.title = str(self.title or "").strip()
        self.content = str(self.content or "").strip()
        if not self.system_id:
            raise ValidationError({"system_id": "请选择或填写知识所属系统"})
        if not self.title:
            raise ValidationError({"title": "请填写知识标题"})
        if not self.content:
            raise ValidationError({"content": "请填写知识内容或上传可提取的文档"})
        from .intent.contracts import contains_secret_literal

        if contains_secret_literal(self.content):
            raise ValidationError({"content": "知识内容不能包含密码、Token、连接串等凭据实际值"})

    def save(self, *args, **kwargs):
        self.content_hash = "sha256:" + sha256(
            str(self.content or "").encode("utf-8")
        ).hexdigest()
        super().save(*args, **kwargs)

    def approve(self) -> None:
        self.status = self.Status.APPROVED
        self.approved_at = timezone.now()
        self.approval_id = f"approval-{uuid4().hex}"
        self.full_clean()
        self.save()

    def __str__(self) -> str:
        return self.title


class TestResourceProfile(models.Model):
    """Human-facing test resources used by layer two and layer three.

    The normal UI stores only real resource connections or uploaded definitions.
    Layer two resolves them into an immutable planning catalog; layer three resolves
    runtime dependencies.
    """

    profile_id = models.CharField(
        max_length=40,
        unique=True,
        default=_resource_profile_id,
        editable=False,
        verbose_name="配置编号",
    )
    name = models.CharField(
        max_length=200,
        verbose_name="配置名称",
        help_text="例如：账号系统测试资源。",
    )
    system_id = models.CharField(
        max_length=128,
        blank=True,
        verbose_name="被测系统标识",
        help_text="稳定的系统编号，例如 account-web；用于关联各层产物。",
    )
    environment = models.CharField(
        max_length=128,
        default="test",
        verbose_name="测试环境",
        help_text="例如 local、test 或 staging；不同环境的资源和产物不会混用。",
    )
    ui_agent_asset_file = models.FileField(
        upload_to="test_platform/resources/ui-agent/%Y/%m/",
        blank=True,
        verbose_name="网页 Agent 资料文件（可选）",
        help_text="上传包含 URL、功能和最大步数的表格或常见文本文件。",
    )
    ui_agent_asset_text = models.TextField(
        blank=True,
        verbose_name="网页 Agent 资料说明（可选）",
        help_text="直接填写网站 URL、大致功能和最大步数；与资料文件二选一。",
    )
    api_openapi_file = models.FileField(
        upload_to="test_platform/resources/openapi/%Y/%m/",
        blank=True,
        verbose_name="接口资料文件（可选）",
        help_text="可上传 OpenAPI，也可上传包含 method、path 和参数说明的常见文档。",
    )
    api_asset_text = models.TextField(
        blank=True,
        verbose_name="接口资料说明（可选）",
        help_text="没有现成文件时，可直接描述可调用接口及参数；模型会整理为内部接口目录。",
    )
    api_base_url = models.URLField(
        blank=True,
        verbose_name="被测 API 基础地址",
        help_text="例如 https://staging.example.test/api。",
    )
    database_query_file = models.FileField(
        upload_to="test_platform/resources/database/%Y/%m/",
        blank=True,
        verbose_name="数据库资料文件（可选）",
        help_text="可上传表结构、DDL、数据字典或数据库说明，不要包含密码或连接串。",
    )
    database_asset_text = models.TextField(
        blank=True,
        verbose_name="数据库资料说明（可选）",
        help_text="可直接描述方言、可测试的表和字段；模型会整理为只读数据库边界。",
    )
    database_connection_ref = models.CharField(
        max_length=128,
        blank=True,
        verbose_name="数据库连接引用",
        help_text="只填写运行环境中已登记的连接名称，不填写密码或连接串。",
    )
    performance_profile_file = models.FileField(
        upload_to="test_platform/resources/performance/%Y/%m/",
        blank=True,
        verbose_name="性能资料文件（可选）",
        help_text="可上传性能要求、历史方案或现有配置，格式不必固定。",
    )
    performance_asset_text = models.TextField(
        blank=True,
        verbose_name="性能资料说明（可选）",
        help_text="可直接描述目标地址、负载上限和指标；模型会整理为内部性能配置。",
    )
    port_host = models.CharField(
        max_length=253,
        blank=True,
        verbose_name="被测主机",
        help_text="填写主机名或 IP，不填写协议和路径。",
    )
    port_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="被测 TCP 端口",
    )
    normalized_resource_data = models.JSONField(default=dict, blank=True, editable=False)
    normalized_resource_source_hash = models.CharField(
        max_length=71,
        blank=True,
        editable=False,
    )
    enabled = models.BooleanField(default=True, db_index=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["system_id", "environment", "name"]
        verbose_name = "测试资源配置"
        verbose_name_plural = "测试资源配置"

    def configured_channels(self) -> set[str]:
        channels: set[str] = set()
        if self.ui_agent_asset_file or self.ui_agent_asset_text.strip():
            channels.add("ui")
        if (self.api_openapi_file or self.api_asset_text.strip()) and self.api_base_url:
            channels.add("api")
        if (
            self.database_query_file or self.database_asset_text.strip()
        ) and self.database_connection_ref:
            channels.add("database")
        if self.performance_profile_file or self.performance_asset_text.strip():
            channels.add("performance")
        if self.port_host and self.port_number is not None:
            channels.add("port")
        return channels

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.ui_agent_asset_file and self.ui_agent_asset_text.strip():
            errors["ui_agent_asset_text"] = "网页 Agent 资料文件和文字说明只能选择一种"
        has_api_source = bool(self.api_openapi_file or self.api_asset_text.strip())
        if has_api_source != bool(self.api_base_url):
            errors["api_openapi_file"] = "接口资料和 API 基础地址必须同时配置"
        has_database_source = bool(
            self.database_query_file or self.database_asset_text.strip()
        )
        if has_database_source != bool(self.database_connection_ref):
            errors["database_query_file"] = "数据库资料和连接引用必须同时配置"
        if bool(self.port_host) != (self.port_number is not None):
            errors["port_host"] = "TCP 主机和端口必须同时配置"
        if self.port_number is not None and not 1 <= self.port_number <= 65535:
            errors["port_number"] = "TCP 端口必须在 1-65535 之间"
        configured = self.configured_channels()
        if not configured:
            errors["name"] = "至少配置一种测试资源"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.system_id:
            self.system_id = self.profile_id.lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class TestWorkflow(models.Model):
    """Raw test intent retained until it produces a separate test-plan artifact."""

    class Status(models.TextChoices):
        DRAFT = "draft", "待生成测试计划"
        DESIGN_GENERATING = "design_generating", "测试计划生成中"
        DESIGN_REVIEW = "design_review", "测试计划待审核"
        DESIGN_CHANGES = "design_changes", "测试计划待修改"
        DESIGN_APPROVED = "design_approved", "测试计划已审核"
        ERROR = "error", "处理失败"

    workflow_id = models.CharField(
        max_length=35,
        unique=True,
        default=_workflow_id,
        editable=False,
        verbose_name="工作单号",
    )
    title = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="测试任务名称（可选）",
    )
    requirement_text = models.TextField(blank=True, verbose_name="需求原文")
    requirement_file = models.FileField(
        upload_to="test_platform/requirements/%Y/%m/",
        blank=True,
        verbose_name="主需求文档",
        help_text="可上传 DOCX、PDF、Markdown、Excel、JSON、YAML、ReqIF 等常见需求文件。",
    )
    request_id = models.CharField(max_length=128, blank=True, verbose_name="外部需求号")
    system_id = models.CharField(max_length=128, default="unknown", verbose_name="目标系统")
    target_environment = models.CharField(
        max_length=128,
        default="unknown",
        verbose_name="目标环境",
    )
    coverage_by_category = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="各分类覆盖方式",
        help_text="按测试分类保存适用的覆盖方式。",
    )
    allowed_channels = models.JSONField(default=list, verbose_name="测试分类")
    knowledge_scope_ids = models.JSONField(default=list, blank=True, verbose_name="知识范围")
    resource_profile = models.ForeignKey(
        TestResourceProfile,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="workflows",
        verbose_name="测试资源",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        verbose_name="当前阶段",
    )
    generation_progress = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        verbose_name="生成进度",
    )
    last_error = models.TextField(blank=True, editable=False, verbose_name="最近错误")
    is_marked = models.BooleanField(default=False, db_index=True, verbose_name="已标记")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["-updated_at", "-id"]
        verbose_name = "测试任务"
        verbose_name_plural = "测试任务"

    def __str__(self) -> str:
        return self.title or "未命名测试任务"


class TestIntentImport(TestWorkflow):
    """流程入口代理：导入需求并生成待审核的测试意图。"""

    class Meta:
        proxy = True
        verbose_name = "导入测试意图"
        verbose_name_plural = "导入测试意图"


class TestPlanArtifact(models.Model):
    """Immutable layer-one output consumed by test-plan review."""

    class Status(models.TextChoices):
        REVIEW = "review", "待审批"
        BLOCKED = "blocked", "生成结果已阻塞"
        CHANGES = "changes", "退回修改"
        APPROVED = "approved", "已审批"
        ERROR = "error", "处理失败"

    class SourceKind(models.TextChoices):
        GENERATED = "generated", "需求生成"
        IMPORTED = "imported", "已有产物导入"

    artifact_id = models.CharField(
        max_length=35,
        unique=True,
        default=_test_plan_artifact_id,
        editable=False,
        verbose_name="测试计划编号",
    )
    source_intent = models.ForeignKey(
        TestWorkflow,
        null=True,
        blank=True,
        editable=False,
        on_delete=models.CASCADE,
        related_name="test_plan_artifacts",
        verbose_name="来源测试意图",
    )
    resource_profile = models.ForeignKey(
        TestResourceProfile,
        on_delete=models.PROTECT,
        related_name="test_plan_artifacts",
        verbose_name="测试资源",
    )
    source_kind = models.CharField(
        max_length=16,
        choices=SourceKind.choices,
        default=SourceKind.GENERATED,
        editable=False,
        verbose_name="产物来源",
    )
    title = models.CharField(max_length=200, verbose_name="测试计划名称")
    test_categories = models.JSONField(
        default=list,
        editable=False,
        verbose_name="测试分类",
    )
    design_id = models.CharField(max_length=192, db_index=True, verbose_name="设计编号")
    version = models.PositiveIntegerField(verbose_name="版本")
    content_hash = models.CharField(max_length=71, db_index=True, verbose_name="内容 Hash")
    generation_result = models.JSONField(default=dict, blank=True, editable=False)
    review_payload = models.JSONField(default=dict, blank=True, editable=False)
    approved_bundle = models.JSONField(default=dict, blank=True, editable=False)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.REVIEW,
        db_index=True,
        verbose_name="审批状态",
    )
    review_comments = models.TextField(blank=True, verbose_name="审批意见")
    last_error = models.TextField(blank=True, editable=False, verbose_name="最近错误")
    is_marked = models.BooleanField(default=False, db_index=True, verbose_name="已标记")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["design_id", "version", "content_hash"],
                name="tp_unique_design_revision",
            )
        ]
        verbose_name = "测试计划"
        verbose_name_plural = "测试计划"

    def __str__(self) -> str:
        return self.title or "未命名测试计划"


class ExecutionPlanArtifact(models.Model):
    """Layer-two output consumed by execution-plan review and execution."""

    class Status(models.TextChoices):
        GENERATING = "generating", "生成中"
        REVIEW = "review", "待审批"
        BLOCKED = "blocked", "生成结果已阻塞"
        CHANGES = "changes", "退回修改"
        APPROVED = "approved", "已审批"
        SUPERSEDED = "superseded", "已被新版本替代"
        ERROR = "error", "处理失败"

    artifact_id = models.CharField(
        max_length=35,
        unique=True,
        default=_execution_plan_artifact_id,
        editable=False,
        verbose_name="执行计划编号",
    )
    source_test_plan = models.ForeignKey(
        TestPlanArtifact,
        on_delete=models.CASCADE,
        related_name="execution_plans",
        verbose_name="来源测试计划",
    )
    resource_profile = models.ForeignKey(
        TestResourceProfile,
        on_delete=models.PROTECT,
        related_name="execution_plan_artifacts",
        verbose_name="测试资源",
    )
    title = models.CharField(max_length=200, verbose_name="执行计划名称")
    test_categories = models.JSONField(
        default=list,
        editable=False,
        verbose_name="测试分类",
    )
    plan_id = models.CharField(max_length=192, db_index=True, verbose_name="计划编号")
    version = models.PositiveIntegerField(verbose_name="版本")
    content_hash = models.CharField(max_length=71, db_index=True, verbose_name="内容 Hash")
    catalog_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    runtime_config_hash = models.CharField(
        max_length=71,
        blank=True,
        editable=False,
        verbose_name="运行资源 Hash",
    )
    execution_input = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        verbose_name="已冻结运行输入",
    )
    compilation_result = models.JSONField(default=dict, blank=True, editable=False)
    review_payload = models.JSONField(default=dict, blank=True, editable=False)
    approved_bundle = models.JSONField(default=dict, blank=True, editable=False)
    artifact_root_ref = models.CharField(max_length=512, editable=False)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.REVIEW,
        db_index=True,
        verbose_name="审批状态",
    )
    generation_progress = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        verbose_name="生成进度",
    )
    review_comments = models.TextField(blank=True, verbose_name="审批意见")
    last_error = models.TextField(blank=True, editable=False, verbose_name="最近错误")
    is_marked = models.BooleanField(default=False, db_index=True, verbose_name="已标记")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan_id", "version", "content_hash"],
                name="tp_unique_plan_revision",
            ),
            models.UniqueConstraint(
                fields=["source_test_plan"],
                condition=models.Q(status="generating"),
                name="tp_one_generating_plan_per_source",
            ),
        ]
        verbose_name = "执行计划"
        verbose_name_plural = "执行计划"

    def __str__(self) -> str:
        return self.title or "未命名执行计划"


class TestExecutionRun(models.Model):
    """One audited invocation of the approved-plan execution coordinator."""

    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "运行中"
        PASSED = "passed", "通过"
        FAILED = "failed", "失败"
        BLOCKED = "blocked", "阻断"
        ERROR = "error", "错误"
        INCONCLUSIVE = "inconclusive", "未定"
        DRY_RUN = "dry_run", "预检"

    class ReportStatus(models.TextChoices):
        PENDING = "pending", "待生成"
        AVAILABLE = "available", "可用"
        FAILED = "failed", "生成失败"

    run_id = models.CharField(max_length=128, unique=True, verbose_name="运行批次号")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RUNNING,
        db_index=True,
        verbose_name="运行状态",
    )
    report_status = models.CharField(
        max_length=16,
        choices=ReportStatus.choices,
        default=ReportStatus.PENDING,
        verbose_name="报告状态",
    )

    design_id = models.CharField(max_length=192, blank=True)
    design_version = models.PositiveIntegerField(null=True, blank=True)
    plan_id = models.CharField(max_length=192, blank=True, db_index=True)
    plan_version = models.PositiveIntegerField(null=True, blank=True)
    plan_content_hash = models.CharField(max_length=71, blank=True)
    artifact_set_hash = models.CharField(max_length=71, blank=True)
    target_system_id = models.CharField(max_length=192, blank=True)
    target_environment = models.CharField(max_length=192, blank=True)
    resource_profile = models.ForeignKey(
        TestResourceProfile,
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="runs",
        verbose_name="执行时测试资源配置",
    )
    execution_plan = models.ForeignKey(
        ExecutionPlanArtifact,
        null=True,
        blank=True,
        editable=False,
        on_delete=models.CASCADE,
        related_name="runs",
        verbose_name="执行计划产物",
    )
    started_at = models.DateTimeField(db_index=True, verbose_name="开始时间")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")
    duration_ms = models.FloatField(null=True, blank=True, verbose_name="耗时（毫秒）")

    # Paths are logical refs below TEST_PLATFORM_ARTIFACT_ROOT, never machine paths.
    storage_root_ref = models.CharField(max_length=1024)
    manifest_path = models.CharField(max_length=1024, blank=True)
    report_paths = models.JSONField(default=dict, blank=True)
    report_content_hash = models.CharField(max_length=71, blank=True)
    result_summary = models.JSONField(default=dict, blank=True)
    errors = models.JSONField(default=list, blank=True)
    is_marked = models.BooleanField(default=False, db_index=True, verbose_name="已标记")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["execution_plan"],
                condition=models.Q(status__in=("queued", "running")),
                name="tp_one_active_run_per_plan",
            ),
        ]
        indexes = [
            models.Index(
                fields=["plan_id", "started_at"],
                name="tp_run_plan_started_idx",
            ),
        ]
        verbose_name = "执行历史"
        verbose_name_plural = "执行历史"

    def __str__(self) -> str:
        return f"{self.run_id} ({self.status})"


__all__ = [
    "ExecutionPlanArtifact",
    "TestExecutionRun",
    "TestIntentImport",
    "TestPlanArtifact",
    "TestResourceProfile",
    "TestWorkflow",
]
