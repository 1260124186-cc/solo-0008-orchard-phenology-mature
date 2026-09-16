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

const LEGACY_MARK_SCRIPT = `
import json, sqlite3, sys
from pathlib import Path

data_dir, observation_id = sys.argv[1], sys.argv[2]
database = sqlite3.connect(str(Path(data_dir) / "atlas.sqlite3"))
row = database.execute(
    "SELECT payload FROM entities WHERE kind = 'observation' AND id = ?",
    (observation_id,),
).fetchone()
record = json.loads(row[0])
record["status"] = "completed"
record["completed_at"] = record["updated_at"]
record.pop("absence_markers", None)
record.pop("completion_basis", None)
database.execute(
    "UPDATE entities SET payload = ? WHERE kind = 'observation' AND id = ?",
    (json.dumps(record, ensure_ascii=False, separators=(",", ":")), observation_id),
)
database.commit()
`;

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
    await checkObservation(page, runtimeDir);
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

  await page.locator('[data-check="confirm-plot"]').click();
  await page.getByText("园区档案已确认并锁定").waitFor();
  const status = await page.locator('[data-check="plot-status"]').innerText();
  if (!status.includes("已确认")) {
    throw new Error("页面未显示园区已确认状态");
  }

  const plots = await api("/plots?q=OR-2101");
  const plot = plots.items.find((item) => item.code === "OR-2101");
  if (!plot || plot.status !== "confirmed" || plot.tree_count !== 1) {
    throw new Error("服务端园区确认结果不符合预期");
  }
  const trees = await api(`/trees?plot_id=${encodeURIComponent(plot.id)}`);
  if (trees.items.length !== 1 || trees.items[0].code !== "OR-2101-T01") {
    throw new Error("服务端植株目录与页面操作不一致");
  }
}

async function checkObservation(page, runtimeDir) {
  const { plot, tree } = await seedCatalog("OR-2201", "东溪古梨园", "青玉梨");
  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-check="nav-observation"]').click();
  await startSeason(page, plot.id, tree.id, "2026", "周岚");

  const entries = [
    ["bud_burst", "2026-03-14"],
    ["full_bloom", "2026-04-06"],
    ["fruit_set", "2026-04-24"],
    ["harvest", "2026-09-08"],
  ];
  for (let index = 0; index < entries.length; index += 1) {
    await fillStage(page, entries[index][0], entries[index][1]);
  }
  await page.locator('[data-check="complete-season"]').click();
  await page.getByText("季节志已完成并冻结，完成依据已随季节志保存").waitFor();
  const status = await page.locator('[data-check="season-status"]').innerText();
  if (!status.includes("已完成")) {
    throw new Error("页面未显示季节志完成状态");
  }
  // 详情中的完成依据：四个必需阶段均有实际观察。
  const basisText = await page
    .locator('[data-check="completion-basis-text"]')
    .innerText();
  if (!basisText.includes("4 个必需阶段均有实际观察")) {
    throw new Error("详情完成依据文案与真实判断不一致");
  }
  const listLine = await page
    .locator('[data-check="observation-basis-line"]')
    .first()
    .innerText();
  if (!listLine.includes("4 项必需阶段均有观察")) {
    throw new Error(`列表完成口径不一致：${listLine}`);
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
  if (observation.completion_basis.legacy) {
    throw new Error("新完成的季节志不应被标记为旧口径");
  }

  // 第二条季节志：采收期以“当年不适用 + 依据”完成。
  const second = await seedCatalog("OR-2202", "南坡古梨园", "蜜香梨");
  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-check="nav-observation"]').click();
  await startSeason(page, second.plot.id, second.tree.id, "2026", "林九");
  for (const [stage, day] of [
    ["bud_burst", "2026-03-12"],
    ["full_bloom", "2026-04-02"],
    ["fruit_set", "2026-04-20"],
  ]) {
    await fillStage(page, stage, day);
  }
  // “未观察到”必须先暴露为阻碍，不能直接完成。
  await page.locator('[data-check="absence-stage"]').click();
  await page.locator('[data-choice-value="harvest"]').click();
  await page.locator('[data-check="absence-reason"]').click();
  await page.locator('[data-choice-value="unobserved"]').click();
  await page
    .locator('[data-check="absence-basis"]')
    .fill("九月两次到场都没有看到可采收果实");
  await page.locator('[data-check="absence-save"]').click();
  await page.locator('[data-check="absence-item"][data-stage="harvest"]').waitFor();
  await page.locator('[data-check="completion-blockers"]').waitFor();
  if (await page.locator('[data-check="complete-season"]').isEnabled()) {
    throw new Error("“未观察到”标记不应放行完成操作");
  }

  // 改为“当年不适用”并补充分依据后可以完成。
  await page.locator('[data-check="absence-item"][data-stage="harvest"] [data-check="absence-edit"]').click();
  await page.locator('[data-check="absence-reason"]').click();
  await page.locator('[data-choice-value="not_applicable"]').click();
  await page
    .locator('[data-check="absence-basis"]')
    .fill("该树花期遭冻害绝收，现场两次核查确认本年无采收期");
  await page.locator('[data-check="absence-save"]').click();
  await page
    .locator('[data-check="absence-item"][data-reason="not_applicable"]')
    .waitFor();
  const harvestNode = page.locator(
    '.stage-track__item[data-stage="harvest"]',
  );
  if ((await harvestNode.getAttribute("data-stage-state")) !== "not_applicable") {
    throw new Error("阶段轨道未把采收期标记为当年不适用");
  }
  await page.locator('[data-check="complete-season"]').click();
  await page.getByText("季节志已完成并冻结，完成依据已随季节志保存").waitFor();
  const notApplicableBasis = await page
    .locator('[data-check="completion-basis-text"]')
    .innerText();
  if (!notApplicableBasis.includes("3 个有实际观察") || !notApplicableBasis.includes("1 个")) {
    throw new Error("不适用季节志的完成依据未按真实含义表述");
  }
  const secondObserved = await api(
    `/observations?tree_id=${encodeURIComponent(second.tree.id)}`,
  );
  const secondSeason = secondObserved.items[0];
  if (
    secondSeason.completion_basis.not_applicable_required_count !== 1 ||
    secondSeason.completion_basis.observed_required_count !== 3
  ) {
    throw new Error("服务端冻结的完成依据与页面操作不一致");
  }

  // API 层兜底：仅登记“仍在核实”同样返回 412，不能绕过必需阶段。
  const third = await seedCatalog("OR-2203", "西坳梨园", "秋白梨");
  let pending = await api("/observations", "PUT", {
    tree_id: third.tree.id,
    season: "2026",
    observer: "核验组",
    note: "",
  });
  pending = await markAbsence(pending, "harvest", "pending_verification", "等待管护员确认");
  const blocked = await rawApi(
    `/observations/${pending.id}/complete`,
    "PUT",
    { revision: pending.revision },
  );
  if (blocked.status !== 412) {
    throw new Error(`仍在核实的必需阶段应拒绝完成，实际 ${blocked.status}`);
  }

  // 旧记录：直接把季节志改成特性上线前的形态（无缺失说明、无完成依据快照）。
  const legacy = await seedCatalog("OR-2204", "旧档梨园", "老品种");
  let oldSeason = await api("/observations", "PUT", {
    tree_id: legacy.tree.id,
    season: "2025",
    observer: "旧档组",
    note: "",
  });
  for (const [stage, day] of [
    ["bud_burst", "2025-03-10"],
    ["full_bloom", "2025-04-01"],
    ["fruit_set", "2025-04-18"],
    ["harvest", "2025-09-02"],
  ]) {
    oldSeason = await api(`/observations/${oldSeason.id}/stages`, "PUT", {
      stage,
      observed_on: day,
      confidence: 4,
      note: "",
      revision: oldSeason.revision,
    });
  }
  await markLegacyCompleted(runtimeDir, oldSeason.id);
  const legacyDetail = await api(`/observations/${oldSeason.id}`);
  if (
    !legacyDetail.completion_basis.legacy ||
    !legacyDetail.completion_basis.basis_text.includes("沿用当时的完成判断")
  ) {
    throw new Error("旧季节志应保持当时判断并给出旧口径解释");
  }
  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-check="nav-observation"]').click();
  const legacyLine = page
    .locator('[data-check="observation-list-item"]', {
      hasText: "2025",
    })
    .locator('[data-check="observation-basis-line"]')
    .first();
  if (!(await legacyLine.innerText()).includes("沿用当时判断")) {
    throw new Error("列表未用旧口径解释已完成的旧季节志");
  }
  await page
    .locator('[data-check="observation-list-item"]', { hasText: "2025" })
    .first()
    .click();
  const legacyPanel = await page
    .locator('[data-check="completion-basis-text"]')
    .innerText();
  if (!legacyPanel.includes("沿用当时的完成判断")) {
    throw new Error("详情未按旧口径解释历史季节志");
  }
}

async function startSeason(page, plotId, treeId, year, observer) {
  await page.locator('[data-check="season-plot"]').click();
  await page.locator(`[data-choice-value="${plotId}"]`).click();
  await page.locator('[data-check="season-tree"]').click();
  await page.locator(`[data-choice-value="${treeId}"]`).click();
  await page.locator('[data-check="season-year"]').fill(year);
  await page.locator('[data-check="season-observer"]').fill(observer);
  await page.locator('[data-check="start-season"]').click();
  await page.getByText("季节志已建立，可以开始补录阶段").waitFor();
}

async function fillStage(page, stage, observedOn) {
  await page.locator('[data-check="stage-key"]').click();
  await page.locator(`[data-choice-value="${stage}"]`).click();
  await page.locator('[data-check="stage-date"]').fill(observedOn);
  await page.locator('[data-check="stage-confidence"]').click();
  await page.locator('[data-choice-value="4"]').click();
  await page.locator('[data-check="add-stage"]').click();
  await page
    .locator(`.stage-book__row[data-stage="${stage}"]`)
    .waitFor();
}

async function markAbsence(observation, stage, reason, basis) {
  return api(`/observations/${observation.id}/absences`, "PUT", {
    stage,
    reason,
    basis,
    revision: observation.revision,
  });
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

async function rawApi(path, method = "GET", body) {
  const response = await fetch(`${API_ORIGIN}/api${path}`, {
    method,
    headers: {
      "X-Actor-Id": "local-admin",
      "X-Idempotency-Key": `check-${crypto.randomUUID()}`,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  return { status: response.status, payload };
}

function markLegacyCompleted(dataDir, observationId) {
  const result = spawnSync(
    "python3",
    ["-", dataDir, observationId],
    {
      cwd: ROOT,
      input: LEGACY_MARK_SCRIPT,
      encoding: "utf-8",
    },
  );
  if (result.status !== 0) {
    throw new Error(`旧记录改写失败：${result.stderr || result.stdout}`);
  }
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
