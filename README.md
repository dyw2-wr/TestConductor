# TestConductor

这是一个与 AI 多轮协作后形成的项目，目前仍有一些瑕疵待完善。

UI 测试可以选择编排已有 UI 函数，或根据粗粒度网页资料交给 Stagehand Agent 探索执行。函数资产库使用固定格式；Agent 资料只需提供 URL、功能和最大步数。

TestConductor 是一个本地运行、人工审批后执行的 AI 测试工作台。它把需求逐层转换为可审核的测试计划和执行计划，审批执行计划后自动运行并生成证据与报告。

```text
测试意图
  -> AI 生成详细测试计划
  -> 人工审批或填写修改要求重新生成
  -> AI 根据测试资源生成完整执行计划
  -> 人工运行，或填写修改要求重新生成当前执行计划
  -> 自动执行
  -> 执行历史、证据和报告
```

项目适合本地开发和实验，不包含账号、登录、审批人身份或权限系统。管理后台只允许从本机访问，不应直接暴露到公网或不受信任的局域网。

## 支持范围

| 测试渠道 | 计划和执行方式 |
| --- | --- |
| UI | 选择编排已发布 UI 函数，或由本地 Stagehand Agent 执行已审批 Action/Check |
| API | 根据 OpenAPI、接口文档或文字说明生成受控 HTTP 请求与断言 |
| 数据库 | 根据表结构、数据字典和知识生成只读 SQL 与检查 |
| 性能/压力 | 根据需求和资源上限生成负载阶段、指标和阈值 |
| TCP 端口 | 对测试资源中明确登记的单个主机和端口执行连接检查 |

API、数据库、性能和端口测试由 AI 在资源约束内生成执行内容，再交给人工审批。UI 函数模式编排已发布 Procedure；Agent 模式保留已审批 Action/Check，由 Stagehand 在资产限定的网站和最大步数内探索页面。

## UI 函数资产

TestConductor 不依赖 `auto_ui_test` 的运行或导航接口。两个项目只通过一个站点级 SQLite 文件交换已发布的 UI 函数资产。

测试资源中的“UI 函数资产库”应选择 `ProcedureAssetLibraryV1` 文件。执行计划使用的核心数据包括：

- 资产库版本和内容 Hash。
- Procedure 的稳定 ID、版本和 fingerprint。
- Procedure 名称、用途、前置条件和参数契约。
- 已发布状态以及按顺序执行的 Action/Check 调用。
- 参数来源，例如测试数据、profile、secret 或 remember 值。

资产库不需要提供 Navigation，也不提供 Repair 经验。执行前会重新校验资产库及 Procedure 身份；文件变化后，旧执行计划会被阻断并要求重新生成和审批。

## 三层职责

### 测试意图到测试计划

用户输入自然语言需求或上传常见文件，并选择需要覆盖的测试渠道。测试类型和具体要求直接写在需求中，不受固定测试类型枚举限制。

测试计划描述：

- 要测试的业务场景和测试方式。
- 对哪个对象执行什么业务操作。
- 每个操作后检查什么预期结果。
- 所需状态、测试数据和清理目标。

这一层不提前生成 locator、HTTP 请求、SQL 或压测脚本。

### 测试计划到执行计划

执行计划智能体读取已审批测试计划和当前测试资源，生成可以直接审批的完整执行内容：

- UI 函数模式：所选 Procedure 及排列顺序、输入绑定和检查点。
- UI Agent 模式：起始 URL、资产最大步数以及逐项 Action/Check。
- API：method、URL、参数、请求体和断言。
- 数据库：连接引用、只读 SQL、参数和结果断言。
- 性能：目标、负载阶段、持续时间、并发量、指标和阈值。
- 端口：已登记端点、超时和预期状态。

模型只能生成候选内容，系统负责 ID、版本、Hash、资源边界和确定性校验，最终由人工审批。

### 执行与报告

审批执行计划后立即创建独立运行批次。普通执行器只执行已审批产物；Stagehand Agent 可调用模型探索页面技术路径，但不能改变已审批 Action/Check。每次运行都会保存：

- 执行状态和各测试分类结果。
- 流程、阶段、步骤和断言结果。
- 脱敏后的证据与错误。
- JSON、HTML 和 JUnit 报告。

失败后可以回到原执行计划重新运行；需要改变输入或执行内容时，在当前执行计划或测试计划中填写修改要求并生成新版本。

## 安装

要求 Python 3.11。推荐使用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
npm install
```

复制 `.env.example` 为 `.env`，至少配置模型参数：

```dotenv
TEST_PLATFORM_LLM_API_KEY=your-api-key
TEST_PLATFORM_LLM_BASE_URL=https://your-provider.example/v1
TEST_PLATFORM_LLM_MODEL=your-model
```

平台和数据库密钥只能放在本机 `.env` 或运行环境中，不能提交到 Git。被测网站的测试账号密码可在测试计划转执行计划时作为本次变量填写，不要写进可复用测试资产。

初始化并启动：

```powershell
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Windows 也可以直接运行：

```powershell
.\start.cmd
```

打开 [http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/)。

## 使用顺序

1. 在“测试资源配置”中登记当前网站或系统可用的测试资源。
2. 可选：在“业务知识库”中导入并发布稳定业务规则或历史经验。
3. 在“导入测试意图”中填写需求、选择测试渠道并生成测试计划。
4. 审批测试计划；需要调整时填写修改要求让 AI 重新生成。
5. 审批完整执行计划；通过后系统立即执行。
6. 在“执行历史”查看结果、证据和报告。

业务知识只作为模型参考，不会扩大测试资源权限，也不会直接执行。

## 输入文件

需求和业务知识支持直接粘贴文本，也支持常见文件：

- 文本与结构化文件：TXT、Markdown、JSON、YAML、XML、ReqIF、Gherkin Feature、HTML。
- Office：DOCX、XLSX、XLS、CSV、TSV、PPTX。
- PDF 和常见图片；OCR 需要本机安装 Poppler 与 Tesseract。
- OpenAPI JSON/YAML。

格式解析层只提取和校验内容，不调用模型、不执行文件命令、不访问文件中的外部链接。无法可靠保留的信息会作为警告显示，不会静默改写或截断需求。

## 示例

`examples/test_resources/` 提供可用于测试资源配置的合成示例：

- `openapi.yaml`
- `database_queries.json`
- `performance_profiles.json`
- `runtime_inputs.example.json`

`examples/manual_trial/` 提供端口、API+数据库、性能、UI 和全渠道需求文本。它们只描述测试意图，不包含真实凭据、locator 或可执行 SQL。

离线多通道演示：

```powershell
python examples/initial_multichannel_demo.py
```

演示会临时启动本地 HTTP/TCP 服务和只读 SQLite，使用确定性的合成数据模拟模型候选，并走完整的契约校验、编译、审批、执行和报告链路。产物写入被 Git 忽略的 `run_artifacts/`。

## 可选 Milvus

Milvus 默认关闭，只用于已审批知识的候选检索，不替代测试资源约束或人工审批。

```powershell
pip install -r requirements-milvus.txt
.\start_with_milvus.cmd
```

默认 hashing embedding 不下载模型；需要更高语义质量时可在本地配置 BGE-M3。

## 目录

```text
apps/test_platform/   Django 业务应用、两层生成、审批、执行和报告
config/               Django 配置
templates/            本地管理工作台模板
docs/                 当前数据契约、输入时机和执行边界
examples/             可执行演示、演示数据与测试资料示例
infra/milvus/          可选本地 Milvus 配置
scripts/               检索评估工具
```

本地 `.env`、SQLite 数据库、上传文件、运行产物、日志、缓存和 Milvus 数据均已加入 `.gitignore`。

## 许可证

Apache License 2.0，详见 `LICENSE`。
