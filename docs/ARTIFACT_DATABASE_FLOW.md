# 产物数据库流转

后台页面按数据库产物交接，不依赖同一工作单的状态跳转。

| 页面 | 查询 | 操作后写入 |
| --- | --- | --- |
| 导入测试意图 | `TestWorkflow` 中待生成或被退回的需求记录 | 新增 `TestPlanArtifact` |
| 审批测试计划 | `TestPlanArtifact` 中待审批的测试计划，或尚未生成下游的已审批计划 | 审批原记录；新增 `ExecutionPlanArtifact` |
| 审批执行计划 | `ExecutionPlanArtifact` 中待审批的执行计划 | 写入审批 bundle；审批通过后立即新增 `TestExecutionRun` |
| 执行历史 | `TestExecutionRun` | 展示批次与报告；失败可原样重试或返回执行计划修改 |

`TestResourceProfile` 是资源目录，不属于业务产物层。测试计划通过
`source_intent_id` 追溯输入；执行计划通过 `source_test_plan_id` 追溯测试计划；运行批次通过
`execution_plan_id` 追溯执行计划。各层同时保存版本与内容 hash，审批不会覆盖上游产物。

已审批测试计划可以通过 `artifact_store.import_approved_test_plan` 导入。该接口先验证
`ApprovedTestDesignBundle` 的 schema、审批和校验 hash、目标环境及测试渠道，然后幂等写入
`TestPlanArtifact`。导入记录的 `source_intent_id` 为空，因此可以跳过需求导入和测试计划审批，
直接生成执行计划。
