# TestConductor 三层输入输出

本文定义当前产品边界。普通用户不填写 Catalog、Hash、执行器引用、SQL 载荷或 UI Workbook；
这些都是系统产物。

## 支撑配置：测试资源

测试资源配置不是第四层，也不是测试意图。它只回答“第二层去哪里取得可用资源、第三层
去哪里执行”。一个配置至少包含一种资源：

| 渠道 | 人工配置 | 系统使用方式 |
| --- | --- | --- |
| UI | 一个站点的 Procedure 资产库文件 | 第二层读取已发布 Procedure；第三层用本地 Playwright 顺序执行 |
| API | OpenAPI 或接口说明 + 精确基础地址 | 标准文件直接解析；宽松资料由模型整理 operation；第三层发 HTTP 请求 |
| 数据库 | DDL/数据字典/表结构说明 + 连接引用 | 模型整理表/字段边界；第三层从安全运行环境解析连接 |
| 性能 | 性能要求、历史方案或现有配置 | 模型整理目标、负载安全上限和指标；第三层从运行环境取得 driver |
| 端口 | 单个主机和端口 | 第二层生成单点探测；第三层执行 TCP connect |

敏感值和可执行 Python 对象只能由进程环境或
`TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY` 注入，不进入数据库、计划和报告。

`PlanningCatalogSnapshot` 仍然存在，但它是第二层每次生成计划时自动构建的内部快照，
不是测试资源配置的人工输入。

## 第一层：测试意图

### 人工输入

```text
测试资源配置
需求原文或需求文件
测试渠道：ui / api / database / performance / port
```

标题和外部需求号可选。系统自动生成工作单号、需求 ID、内部目标 ID 和输入 Hash。

### 模型输入与输出

第一层模型只看到需求、可选知识和内部目标身份，不读取 OpenAPI、SQL、Procedure、
页面采集结果或执行地址。模型输出 `TestDesignCandidate`：逻辑场景、业务操作、预期结果、
数据需求、状态影响、清理目标和未决问题。

```text
TestDesignCandidate
  -> 系统生成稳定 ID 并执行规则校验
  -> 人工审核或退回
  -> ApprovedTestDesignBundle
```

第一层的意义是确认“测什么”。第二层只消费审核通过的 Bundle。

## 第二层：测试计划

### 系统输入

```text
ApprovedTestDesignBundle
工作单选择的测试资源配置
```

第二层先整理资源，最终统一进入严格内部契约：

```text
Procedure 资产库     -> 单站点已发布 Procedure、参数契约、精确版本和 fingerprint
OpenAPI             -> 确定性导入 HTTP operation
接口说明/文档        -> 模型整理 HTTP operation
DDL/数据字典/表说明  -> 模型整理数据库方言、表、字段和运行参数
性能要求/历史方案    -> 模型整理 driver ref、负载安全上限和指标
主机 + 端口         -> 单点 TCP probe
```

资源、知识和本次测试必须分层，五类测试使用同一规则：

| 分类 | 测试资源：当前允许边界 | 业务知识库：历史经验 | 测试意图/计划：本次内容 |
| --- | --- | --- | --- |
| UI | 当前选择的单站点 Procedure 资产库 | 已发布 Procedure 的说明和适用业务流程 | 本次 Procedure 顺序、输入与断言 |
| API | 当前接口资料、基础地址、整理后的 method/path/参数 | 历史兼容经验、调用惯例和故障模式 | 本次请求目的、数据与断言 |
| 数据库 | 方言、连接引用、允许的表/字段/运行参数 | 历史 SQL、用途和适用条件 | 本次采用或生成的 SQL 与检查 |
| 性能 | 可用 driver、负载上限和可采集指标 | 历史压测结果、瓶颈和调优经验 | 本次负载阶段、并发/SLA/阈值 |
| TCP | 当前允许探测的单个主机和端口 | 端口用途、协议背景和历史故障 | 本次期望状态和连接时延检查 |

业务知识不能扩大测试资源权限，测试资源也不能代替本次测试意图。历史数据只能影响建议
和复用选择，最终执行内容必须在当前资源边界内生成并由人工审批。

正式文件的解析结果或宽松资料的模型规范化结果形成只属于本次计划的 `PlanningCatalogSnapshot`。
模型规范化结果按源资料 Hash 缓存，第三层只复检，不会重新猜测。执行计划智能体把审核后的设计 ID
映射到已发布的资源 ref，并生成资源允许的执行语义；不能编写 URL、locator、凭据或不存在的 Procedure ID。
数据库是受控例外：先复用已审批知识库中的历史 SQL；没有合适查询时，模型可依据访问策略
明确登记的表、字段和运行参数生成一条只读 SQL 草稿。测试资源不得保存历史 SQL。草稿及
知识 SQL 都必须通过资源边界校验、人工审批和运行前复检。

```text
资源解析
  -> PlanningCatalogSnapshot
  -> 执行计划智能体生成 PlanCandidate
  -> 编译 TestPlanDraft 和执行器产物
  -> 规则校验
  -> 人工审核或退回
  -> ApprovedTestPlanBundle
  -> 审批通过后自动执行
```

### UI 执行专用边界

- UI 操作必须映射到所选资产库中已发布的 Procedure，计划绑定精确 id、version 和 fingerprint。
- TestConductor 不读取 auto_ui_test 的录制、Candidate、Repair、Navigation 或调用历史。
- 一个资产库只对应一个网站；换网站时在测试资源配置中选择另一个 SQLite 文件。
- `profile/secret/remember` 参数由 TestConductor RuntimeContext 按 `source_key` 解析；`input_data`
  参数必须由第二层生成明确 data binding，不会猜参数。
- 初始 URL 来自首个 Procedure 的前置条件，不进入测试资源表或计划输入。
- 计划审核摘要显示精确 Procedure 模块及版本；不存在模块时阻断，不让模型编造控件操作。

第二层的意义是确认“用当前真实资源怎样测”。

## 第三层：自动执行

### 输入

```text
ApprovedTestPlanBundle
计划生成时冻结的 PlanningCatalogSnapshot
从测试计划生成执行计划时冻结的非秘密变量和性能模式
当前测试资源配置
进程环境注入的 secret、数据库连接、性能 driver 和 cleanup hook
```

执行前比较源资料、Catalog 和运行配置 Hash。接口资料、数据库资料、性能资料、端口或 Procedure
资产库发生变化时，旧计划不会继续执行，必须重新生成和审核。

第三层不调用模型、不重新解释需求，也不重新规划。它只校验 Bundle/Artifact Hash，按顺序
调用 UI Procedure、HTTP、数据库、性能和 TCP runner，执行 flow cleanup，保存证据和报告。

### 输出

```text
TestExecutionRun：一次独立批次、时间、状态和结果摘要
report.json：API 和机器消费
report.html：人工查看
junit.xml：CI 消费
evidence / run manifest：失败定位和审计
```

失败批次可复用冻结输入原样重试，也可返回执行计划填写修改需求后重新生成；每次运行都创建独立记录和报告，不覆盖历史。

## 用户流程

```text
一次性配置实际测试资源

每次测试：
选择资源 + 输入需求 + 选择技术/渠道
  -> 审核测试意图（测什么）
  -> 审核测试计划（怎样测）
  -> 执行批准产物（实际运行）
  -> 查看报告
```
