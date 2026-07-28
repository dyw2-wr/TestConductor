# 网页 Agent UI 执行

网页 UI 有两种互斥执行方式：函数编排和网页 Agent。函数编排继续读取一个网站的
Procedure SQLite 资产库；网页 Agent 不读取该数据库。

网页 Agent 资产只描述粗粒度边界：绝对 HTTP(S) URL、页面或功能说明、最大步数。
可以上传常见文档，也可以直接填写文字。不要在资产中维护控件步骤、变量、账号密码、
执行历史或缓存。

已审批测试计划决定本次 Action 和 Check。生成执行计划前填写本次变量；UI 页面可能在
执行前无法确定全部输入，未注明的输入可能被模型自动编造，因此审批执行计划时必须重点
检查。测试计划明确给出 URL 时可覆盖资产 URL，最大步数始终由资产决定。

执行计划审批通过后立即由本地 Stagehand 执行。Runner 保存实际动作、最终 URL、结果、
失败原因和最终或失败截图，不保存模型推理、页面正文或运行变量。首版不启用搜索、缓存、
自愈知识、向量库、自动转 Procedure、多智能体、移动端或 Browserbase。

运行前执行 `npm install`，并按 `STAGEHAND_MODEL` 所选提供商设置模型密钥。默认模型是
`openai/gpt-4.1-mini`，对应 `OPENAI_API_KEY`。也可以使用 `STAGEHAND_API_KEY` 和
`STAGEHAND_BASE_URL` 配置 OpenAI 兼容服务。
