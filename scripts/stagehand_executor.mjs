import { Stagehand } from "@browserbasehq/stagehand";
import fs from "node:fs/promises";
import path from "node:path";

const PREFIX = "__TEST_CONDUCTOR_RESULT__";
const input = JSON.parse(await new Promise((resolve, reject) => {
  let body = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", chunk => { body += chunk; });
  process.stdin.on("end", () => resolve(body));
  process.stdin.on("error", reject);
}));

let stagehand;
let page;
let screenshotName = "failure.png";
try {
  stagehand = new Stagehand({
    env: "LOCAL",
    experimental: true,
    disableAPI: true,
    selfHeal: false,
    headless: input.headless !== false,
    verbose: 0,
  });
  await stagehand.init();
  page = stagehand.context.pages()[0];
  await page.goto(input.start_url);
  const planned = input.rows.map((row, index) => {
    const checks = row.checks.map(check => check.statement).filter(Boolean);
    return `${index + 1}. Action: ${row.action}\n   Check: ${checks.length ? checks.join("; ") : "无独立检查"}`;
  }).join("\n");
  const instruction = [
    "严格执行下面已审批的测试，不得改变业务目标。可以自行探索页面上的技术操作路径。",
    "逐项完成 Action，并验证对应 Check。只有全部 Check 均得到页面证据支持时才报告完成。",
    "遇到未提供的输入时不要声称已验证；明确报告失败或未完成。",
    planned,
  ].join("\n\n");
  const modelName = process.env.STAGEHAND_MODEL || "openai/gpt-4.1-mini";
  const model = process.env.STAGEHAND_API_KEY
    ? {
        modelName,
        apiKey: process.env.STAGEHAND_API_KEY,
        ...(process.env.STAGEHAND_BASE_URL ? { baseURL: process.env.STAGEHAND_BASE_URL } : {}),
      }
    : modelName;
  const agent = stagehand.agent({ model });
  const variables = Object.fromEntries(Object.entries(input.variables || {}).map(([name, value]) => [
    name, { value: String(value), description: `本次执行输入 ${name}` },
  ]));
  const outcome = await agent.execute({
    instruction,
    maxSteps: input.max_steps,
    variables,
    excludeTools: ["search"],
  });
  screenshotName = outcome.success === true && outcome.completed === true ? "final.png" : "failure.png";
  await fs.mkdir(input.evidence_dir, { recursive: true });
  await page.screenshot({ path: path.join(input.evidence_dir, screenshotName), fullPage: true });
  const actions = (outcome.actions || []).map(item => ({
    type: item.type,
    action: item.action,
    pageUrl: item.pageUrl,
    taskCompleted: item.taskCompleted,
    timestamp: item.timestamp,
  }));
  console.log(PREFIX + JSON.stringify({
    success: outcome.success === true,
    completed: outcome.completed === true,
    message: outcome.message || "",
    final_url: page.url(),
    actions,
    evidence: [screenshotName],
  }));
} catch (error) {
  const evidence = [];
  if (page) {
    try {
      await fs.mkdir(input.evidence_dir, { recursive: true });
      await page.screenshot({ path: path.join(input.evidence_dir, screenshotName), fullPage: true });
      evidence.push(screenshotName);
    } catch {}
  }
  console.log(PREFIX + JSON.stringify({
    success: false,
    completed: false,
    message: String(error?.message || error),
    final_url: page?.url?.() || "",
    actions: [],
    evidence,
  }));
} finally {
  if (stagehand) await stagehand.close().catch(() => {});
}
