#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const API_ORIGIN = "http://127.0.0.1:8765";
const UI_ORIGIN = "http://127.0.0.1:4317";
const workflow = valueAfter("--workflow");

if (!workflow) {
  console.error("用法：node scripts/workflow_check.mjs --workflow catalog|observe|compare");
  process.exit(2);
}

const runtimeDir = mkdtempSync(join(tmpdir(), "orchard-atlas-check-"));
let backend;
let frontend;
let browser;

try {
  backend = startProcess(
    "python3",
    [
      "scripts/run_server.py",
      "--port",
      "8765",
      "--data-dir",
      runtimeDir,
    ],
  );
  await waitForUrl(`${API_ORIGIN}/api/health`);
  frontend = startProcess("npm", [
    "run",
    "dev",
    "--",
    "--strictPort",
  ]);
  await waitForUrl(`${UI_ORIGIN}/`);

  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 980 },
    locale: "zh-CN",
  });
  const page = await context.newPage();
  await page.goto(UI_ORIGIN, { waitUntil: "networkidle" });
  await page.locator('[data-check="nav-catalog"]').waitFor();

  if (workflow === "catalog") {
    await checkCatalog(page);
  } else if (workflow === "observe") {
    await checkObservation(page);
  } else if (workflow === "compare") {
    await checkComparison(page);
  } else {
    throw new Error(`未知工作流：${workflow}`);
  }

  await browser.close();
  browser = undefined;
  console.log(`✅ ${workflow} 浏览器工作流检查通过`);
} catch (error) {
  console.error(`❌ ${workflow} 浏览器工作流检查失败`);
  console.error(error instanceof Error ? error.stack : String(error));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close().catch(() => undefined);
  await stopProcess(frontend);
  await stopProcess(backend);
  rmSync(runtimeDir, { recursive: true, force: true });
}

async function checkCatalog(page) {
  await page.locator('[data-check="nav-catalog"]').click();
  await page.locator('[data-check="plot-code"]').fill("OR-2101");
  await page.locator('[data-check="plot-name"]').fill("北岭老梨园");
  await page.locator('[data-check="plot-locality"]').fill("河湾村北岭东侧");
  await page.locator('[data-check="plot-focus"]').fill("黄皮秋梨");
  await page.locator('[data-check="plot-steward"]').fill("县农业志编研组");
  await page.locator('[data-check="plot-year"]').fill("2009");
  await page.locator('[data-check="plot-note"]').fill("保留传统梯田式栽植格局");
  await page.locator('[data-check="create-plot"]').click();
  await page.getByText("园区草稿已入档").waitFor();

  await page.locator('[data-check="tree-code"]').fill("OR-2101-T01");
  await page.locator('[data-check="tree-cultivar"]').fill("黄皮秋梨");
  await page.locator('[data-check="tree-rootstock"]').fill("杜梨");
  await page.locator('[data-check="tree-year"]').fill("2009");
  await page.locator('[data-check="create-tree"]').click();
  await page.getByText("植株已加入园区").waitFor();
  await page.locator('[data-check="tree-card"]').first().waitFor();

  // 普通园区信息编辑：只改名称等字段，编号与植株编号不受影响。
  await page.locator('[data-check="plot-edit-toggle"]').click();
  await page.locator('[data-check="plot-edit-name"]').fill("北岭老梨园（核定）");
  await page.locator('[data-check="plot-edit-save"]').click();
  await page.getByText("园区基础信息已更新").waitFor();
  const edited = (await api("/plots?q=OR-2101")).items[0];
  if (edited.name !== "北岭老梨园（核定）" || edited.code !== "OR-2101") {
    throw new Error("普通字段编辑后园区名称或编号不符合预期");
  }
  let editedTrees = await api(
    `/trees?plot_id=${encodeURIComponent(edited.id)}`,
  );
  if (editedTrees.items.length !== 1 || editedTrees.items[0].code !== "OR-2101-T01") {
    throw new Error("普通字段编辑不应改动植株编号");
  }

  // 编号修正：验证未决植株阻断、页面内归位、重复编号阻断和整批级联。
  await checkCodeCorrection(page, runtimeDir);

  await page.locator('[data-check="confirm-plot"]').click();
  await page.getByText("园区档案已确认并锁定").waitFor();
  const status = await page.locator('[data-check="plot-status"]').innerText();
  if (!status.includes("已确认")) {
    throw new Error("页面未显示园区已确认状态");
  }

  const plots = await api("/plots?q=OR-2401");
  const plot = plots.items.find((item) => item.code === "OR-2401");
  if (!plot || plot.status !== "confirmed" || plot.tree_count !== 2) {
    throw new Error("服务端园区确认结果不符合预期");
  }
  if (!plot.code_aliases?.some((alias) => alias.code === "OR-2101")) {
    throw new Error("园区详情缺少旧编号别名轨迹");
  }
  const trees = await api(`/trees?plot_id=${encodeURIComponent(plot.id)}`);
  const codes = trees.items.map((item) => item.code).sort();
  if (JSON.stringify(codes) !== JSON.stringify(["OR-2401-T01", "OR-2401-T02"])) {
    throw new Error(`级联后植株编号不符合预期：${codes.join(", ")}`);
  }
  for (const tree of trees.items) {
    if (!tree.code_aliases?.some((alias) => alias.code.startsWith("OR-2101-"))) {
      throw new Error(`植株 ${tree.code} 缺少旧编号轨迹`);
    }
  }

  // 身份核对：园区、植株编号唯一，全部历史对象都能用别名解释。
  const identity = await api("/identity-report");
  if (!identity.unique || identity.unresolved_trees.length !== 0) {
    throw new Error("修正后身份核对未通过");
  }
  if (identity.historical_objects.some((item) => !item.explainable)) {
    throw new Error("存在无法解释新旧编号归属的历史对象");
  }
}

async function checkCodeCorrection(page, runtimeDir) {
  const plot = (await api("/plots?q=OR-2101")).items[0];

  // 1) 未决植株：先创建合规 T02，再通过临时数据库把它改成历史遗留的
  //    跨前缀编号 XX-9001-T02，模拟旧档案导入后的脏数据。
  const stray = await api("/trees", "PUT", {
    plot_id: plot.id,
    code: "OR-2101-T02",
    cultivar: "黄皮秋梨",
    rootstock: "杜梨",
    planting_year: 2009,
    status: "active",
    note: "历史遗留的未决编号",
  });
  rewriteTreeCode(runtimeDir, stray.id, "XX-9001-T02");

  await page.locator('[data-check="recode-code"]').fill("OR-2401");
  await page.locator('[data-check="recode-reason"]').fill("园区编号换段修正");
  await page.locator('[data-check="recode-preview"]').click();
  await page.locator('[data-check="recode-plan"]').waitFor();
  await page
    .locator('[data-check="recode-plan"] .recode-plan__banner--blocked')
    .waitFor();
  await page.locator('[data-check="recode-pending"]').waitFor();
  await page
    .locator('[data-check="recode-pending"]')
    .getByText("XX-9001-T02")
    .first()
    .waitFor();
  const blockedApply = page.locator('[data-check="recode-apply"]');
  if (await blockedApply.isEnabled()) {
    throw new Error("存在未决植株时修正按钮不应可执行");
  }

  // 阻断期间所有对象保持旧编号。
  const blockedPlot = (await api(`/plots/${encodeURIComponent(plot.id)}`));
  if (blockedPlot.code !== "OR-2101") {
    throw new Error("未决植株阻断时园区编号被部分改写");
  }
  const blockedTrees = await api(
    `/trees?plot_id=${encodeURIComponent(plot.id)}`,
  );
  const blockedCodes = blockedTrees.items.map((item) => item.code).sort();
  if (JSON.stringify(blockedCodes) !== JSON.stringify(["OR-2101-T01", "XX-9001-T02"])) {
    throw new Error(`阻断时植株编号不符合预期：${blockedCodes.join(", ")}`);
  }

  // 2) 在页面内把未决植株归位。
  await page.locator('[data-check="recode-repair-start"]').click();
  await page.locator('[data-check="recode-repair-code"]').fill("OR-2101-T02");
  await page
    .locator('[data-check="recode-repair-form"] button[type="submit"]')
    .click();
  await page
    .locator('[data-check="toast-stack"]')
    .getByText("未决植株编号已归位")
    .waitFor();
  // 归位后自动重新预览，此时目标编号仍空闲，计划转为可执行。
  await page
    .locator('[data-check="recode-plan"] .recode-plan__banner--ok')
    .waitFor();

  // 3) 并发占用：其他请求先占用 OR-2401，执行必须被拒绝且不落库。
  await api("/plots", "PUT", {
    code: "OR-2401",
    name: "临时占位园区",
    locality: "临时地点",
    cultivar_focus: "占位品种",
    steward: "检查组",
    planting_year: 2009,
    note: "",
  });
  await page.locator('[data-check="recode-apply"]').click();
  await page
    .locator('[data-check="toast-stack"]')
    .getByText("园区编号已被其他园区使用")
    .waitFor();
  const stillOld = await api(`/plots/${encodeURIComponent(plot.id)}`);
  if (stillOld.code !== "OR-2101") {
    throw new Error("重复编号阻断时园区编号被改写");
  }

  // 4) 占位园区把编号让给 OR-2509，再整批级联到 OR-2401。
  const placeholder = (await api("/plots?q=OR-2401")).items[0];
  await api(`/plots/${placeholder.id}/code-correction`, "PUT", {
    code: "OR-2509",
    reason: "让出编号",
    revision: placeholder.revision,
  });
  await page.locator('[data-check="recode-preview"]').click();
  await page
    .locator('[data-check="recode-plan"] .recode-plan__banner--ok')
    .waitFor();
  const plannedChanges = await page.locator(".recode-changes li").count();
  if (plannedChanges !== 2) {
    throw new Error(`级联计划应覆盖两株植株，实际为 ${plannedChanges}`);
  }
  await page.locator('[data-check="recode-apply"]').click();
  await page
    .locator('[data-check="toast-stack"]')
    .getByText("编号已整体修正为 OR-2401")
    .waitFor();

  await page.locator('[data-check="plot-code-history"]').first().waitFor();
  const trees = await api(`/trees?plot_id=${encodeURIComponent(plot.id)}`);
  const finalCodes = trees.items.map((item) => item.code).sort();
  if (JSON.stringify(finalCodes) !== JSON.stringify(["OR-2401-T01", "OR-2401-T02"])) {
    throw new Error(`级联后植株编号不符合预期：${finalCodes.join(", ")}`);
  }
  const repaired = trees.items.find((item) => item.id === stray.id);
  if (
    !repaired.code_aliases?.some((alias) => alias.code === "XX-9001-T02") ||
    !repaired.code_aliases?.some((alias) => alias.code === "OR-2101-T02")
  ) {
    throw new Error("归位并级联的植株缺少完整的旧编号轨迹");
  }

  // 5) 页面身份核对面板显示通过。
  await page.locator('[data-check="identity-report-load"]').click();
  await page.locator('[data-check="identity-report"]').waitFor();
  await page
    .locator('[data-check="identity-report"]')
    .getByText("身份核对通过")
    .waitFor();
}

function rewriteTreeCode(runtimeDir, treeId, nextCode) {
  // 浏览器链路无法产生跨前缀编号，用临时库直接改写实体 JSON，
  // 模拟旧版档案导入后遗留的未决植株。
  const databasePath = join(runtimeDir, "atlas.sqlite3");
  const script = `
import json, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
row = connection.execute(
    "SELECT payload FROM entities WHERE kind = 'tree' AND id = ?",
    (sys.argv[2],),
).fetchone()
if row is None:
    raise SystemExit("missing tree")
payload = json.loads(row[0])
payload["code"] = sys.argv[3]
connection.execute(
    "UPDATE entities SET payload = ? WHERE kind = 'tree' AND id = ?",
    (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), sys.argv[2]),
)
connection.commit()
connection.close()
`;
  const result = spawnSync(
    "python3",
    ["-c", script, databasePath, treeId, nextCode],
    { encoding: "utf-8" },
  );
  if (result.status !== 0) {
    throw new Error(
      `写入未决植株失败：${result.stderr || result.stdout}`,
    );
  }
}

async function checkObservation(page) {
  const { plot, tree } = await seedCatalog("OR-2201", "东溪古梨园", "青玉梨");
  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-check="nav-observation"]').click();
  await page.locator('[data-check="season-plot"]').click();
  await page.locator(`[data-choice-value="${plot.id}"]`).click();
  await page.locator('[data-check="season-tree"]').click();
  await page.locator(`[data-choice-value="${tree.id}"]`).click();
  await page.locator('[data-check="season-year"]').fill("2026");
  await page.locator('[data-check="season-observer"]').fill("周岚");
  await page.locator('[data-check="start-season"]').click();
  await page.getByText("季节志已建立，可以开始补录阶段").waitFor();

  const entries = [
    ["bud_burst", "2026-03-14"],
    ["full_bloom", "2026-04-06"],
    ["fruit_set", "2026-04-24"],
    ["harvest", "2026-09-08"],
  ];
  for (let index = 0; index < entries.length; index += 1) {
    await page.locator('[data-check="stage-key"]').click();
    await page.locator(`[data-choice-value="${entries[index][0]}"]`).click();
    await page.locator('[data-check="stage-date"]').fill(entries[index][1]);
    await page.locator('[data-check="stage-confidence"]').click();
    await page.locator('[data-choice-value="4"]').click();
    await page.locator('[data-check="add-stage"]').click();
    await page
      .locator('[data-check="stage-entry"]')
      .nth(index)
      .waitFor();
  }
  await page.locator('[data-check="complete-season"]').click();
  await page.getByText("季节志已完成并冻结").waitFor();
  const status = await page.locator('[data-check="season-status"]').innerText();
  if (!status.includes("已完成")) {
    throw new Error("页面未显示季节志完成状态");
  }

  const observations = await api(
    `/observations?tree_id=${encodeURIComponent(tree.id)}`,
  );
  const observation = observations.items[0];
  if (
    observation?.status !== "completed" ||
    observation.entries.length !== entries.length
  ) {
    throw new Error("服务端季节志与页面操作不一致");
  }
}

async function checkComparison(page) {
  const first = await seedCompletedSeason({
    plotCode: "OR-2301",
    plotName: "西坡梨园",
    cultivar: "秋白梨",
    treeCode: "OR-2301-T01",
    season: "2026",
    dates: ["2026-03-10", "2026-04-01", "2026-04-18", "2026-09-02"],
  });
  const second = await seedCompletedSeason({
    plotCode: "OR-2302",
    plotName: "南坳梨园",
    cultivar: "蜜香梨",
    treeCode: "OR-2302-T01",
    season: "2026",
    dates: ["2026-03-15", "2026-04-05", "2026-04-22", "2026-09-07"],
  });

  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-check="nav-comparison"]').click();
  const leftLabel = await page
    .locator('[data-check="left-observation"]')
    .innerText();
  const rightLabel = await page
    .locator('[data-check="right-observation"]')
    .innerText();
  if (!leftLabel.trim() || !rightLabel.trim() || leftLabel === rightLabel) {
    throw new Error("页面未自动选择两份有效季节志");
  }
  await page.locator('[data-check="comparison-title"]').fill(
    "2026 年秋白梨与蜜香梨物候对齐",
  );
  await page.locator('[data-check="create-comparison"]').click();
  await page.getByText("对比图谱已生成并保存").waitFor();
  await page.locator('[data-check="comparison-result"]').waitFor();
  const offsetRows = page.locator('[data-check="offset-row"]');
  if ((await offsetRows.count()) !== 4) {
    throw new Error("页面未展示四个共同阶段的偏移");
  }
  const sentence = await page.locator('[data-check="comparison-sentence"]').innerText();
  if (!sentence.includes("共有 4 个阶段")) {
    throw new Error("页面比较摘要内容不完整");
  }

  const comparisons = await api("/comparisons");
  if (comparisons.items.length !== 1) {
    throw new Error("服务端未保存对比图谱");
  }
  if (comparisons.items[0].stage_offsets.length !== 4) {
    throw new Error("服务端对比阶段数不符合预期");
  }
}

async function seedCatalog(code, name, cultivar) {
  const plot = await api("/plots", "PUT", {
    code,
    name,
    locality: "山谷传统种植区",
    cultivar_focus: cultivar,
    steward: "物候档案组",
    planting_year: 2012,
    note: "用于浏览器工作流检查",
  });
  const tree = await api("/trees", "PUT", {
    plot_id: plot.id,
    code: `${code}-T01`,
    cultivar,
    rootstock: "杜梨",
    planting_year: 2012,
    status: "active",
    note: "",
  });
  const confirmed = await api(`/plots/${plot.id}/confirm`, "PUT", {
    revision: plot.revision,
  });
  return { plot: confirmed, tree };
}

async function seedCompletedSeason(config) {
  const { plot, tree } = await seedCatalog(
    config.plotCode,
    config.plotName,
    config.cultivar,
  );
  let observation = await api("/observations", "PUT", {
    tree_id: tree.id,
    season: config.season,
    observer: "对比检查组",
    note: "",
  });
  const stages = ["bud_burst", "full_bloom", "fruit_set", "harvest"];
  for (let index = 0; index < stages.length; index += 1) {
    observation = await api(
      `/observations/${observation.id}/stages`,
      "PUT",
      {
        stage: stages[index],
        observed_on: config.dates[index],
        confidence: 4,
        note: "",
        revision: observation.revision,
      },
    );
  }
  observation = await api(
    `/observations/${observation.id}/complete`,
    "PUT",
    { revision: observation.revision },
  );
  if (!plot || observation.status !== "completed") {
    throw new Error("准备比较数据失败");
  }
  return observation;
}

async function api(path, method = "GET", body) {
  const mutation = method !== "GET";
  const response = await fetch(`${API_ORIGIN}/api${path}`, {
    method,
    headers: {
      "X-Actor-Id": "local-admin",
      ...(mutation
        ? { "X-Idempotency-Key": `check-${crypto.randomUUID()}` }
        : {}),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      `API ${method} ${path} 失败：${payload.error?.message ?? response.status}`,
    );
  }
  return payload;
}

function startProcess(command, args) {
  const child = spawn(command, args, {
    cwd: ROOT,
    env: { ...process.env, CI: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.output = "";
  child.stdout.on("data", (chunk) => {
    child.output += chunk.toString();
  });
  child.stderr.on("data", (chunk) => {
    child.output += chunk.toString();
  });
  child.on("error", (error) => {
    child.output += `\n${error.message}`;
  });
  return child;
}

async function stopProcess(child) {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  const exited = await Promise.race([
    new Promise((resolveExit) => child.once("exit", () => resolveExit(true))),
    new Promise((resolveTimeout) => setTimeout(() => resolveTimeout(false), 2500)),
  ]);
  if (!exited) {
    child.kill("SIGKILL");
    console.error("检查进程未能及时退出，已强制结束。");
  }
}

async function waitForUrl(url, timeout = 20000) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 180));
  }
  const details = backend?.output || frontend?.output || "";
  throw new Error(
    `等待 ${url} 超时：${lastError instanceof Error ? lastError.message : details}`,
  );
}

function valueAfter(flag) {
  const index = process.argv.indexOf(flag);
  return index >= 0 ? process.argv[index + 1] : "";
}
