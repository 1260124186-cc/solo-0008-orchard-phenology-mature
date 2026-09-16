#!/usr/bin/env node

import { spawn } from "node:child_process";
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
  backend = startBackend(runtimeDir);
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
    { stage: "bud_burst", precision: "day", date: "2026-03-14" },
    {
      stage: "full_bloom",
      precision: "range",
      date: "2026-04-04",
      end: "2026-04-08",
    },
    { stage: "fruit_set", precision: "on_or_after", date: "2026-04-20" },
    { stage: "harvest", precision: "day", date: "2026-09-08" },
  ];
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    await page.locator('[data-check="stage-key"]').click();
    await page.locator(`[data-choice-value="${entry.stage}"]`).click();
    await page.locator('[data-check="stage-precision"]').click();
    await page.locator(`[data-choice-value="${entry.precision}"]`).click();
    await page.locator('[data-check="stage-date"]').fill(entry.date);
    if (entry.end) {
      await page.locator('[data-check="stage-end-date"]').fill(entry.end);
    }
    await page.locator('[data-check="stage-confidence"]').click();
    await page.locator('[data-choice-value="4"]').click();
    await page.locator('[data-check="add-stage"]').click();
    await page
      .locator('[data-check="stage-entry"]')
      .nth(index)
      .waitFor();
  }
  // 区间精度必须在页面以“起 至 止”展示，而不是被替换成某个单日。
  const bloomRow = page
    .locator('[data-check="stage-entry"]')
    .nth(1)
    .innerText();
  if (!bloomRow.includes("2026-04-04 至 2026-04-08")) {
    throw new Error("页面未保留区间精度的不确定范围");
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
  assertEntryPrecision(observation.entries, {
    bud_burst: { precision: "day", observed_on: "2026-03-14" },
    full_bloom: {
      precision: "range",
      observed_on: "2026-04-04",
      observed_end_on: "2026-04-08",
    },
    fruit_set: { precision: "on_or_after", observed_on: "2026-04-20" },
    harvest: { precision: "day", observed_on: "2026-09-08" },
  });

  // 恢复验证：使用同一数据目录重启后端，精度字段不能在恢复后丢失。
  await stopProcess(backend);
  backend = startBackend(runtimeDir);
  await waitForUrl(`${API_ORIGIN}/api/health`);
  const recovered = await api(
    `/observations?tree_id=${encodeURIComponent(tree.id)}`,
  );
  const recoveredObservation = recovered.items[0];
  assertEntryPrecision(recoveredObservation.entries, {
    bud_burst: { precision: "day", observed_on: "2026-03-14" },
    full_bloom: {
      precision: "range",
      observed_on: "2026-04-04",
      observed_end_on: "2026-04-08",
    },
    fruit_set: { precision: "on_or_after", observed_on: "2026-04-20" },
    harvest: { precision: "day", observed_on: "2026-09-08" },
  });
}

function assertEntryPrecision(entries, expected) {
  const byStage = Object.fromEntries(entries.map((entry) => [entry.stage, entry]));
  for (const [stage, shape] of Object.entries(expected)) {
    const entry = byStage[stage];
    if (!entry) throw new Error(`服务端缺少阶段 ${stage}`);
    if (entry.precision !== shape.precision) {
      throw new Error(
        `阶段 ${stage} 精度应为 ${shape.precision}，实际为 ${entry.precision}`,
      );
    }
    if (entry.observed_on !== shape.observed_on) {
      throw new Error(
        `阶段 ${stage} 日期应为 ${shape.observed_on}，实际为 ${entry.observed_on}`,
      );
    }
    const expectedEnd = shape.observed_end_on ?? null;
    if ((entry.observed_end_on ?? null) !== expectedEnd) {
      throw new Error(
        `阶段 ${stage} 结束日期应为 ${expectedEnd}，实际为 ${entry.observed_end_on ?? null}`,
      );
    }
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
  const exactComparison = comparisons.items[0];
  if (exactComparison.stage_offsets.length !== 4) {
    throw new Error("服务端对比阶段数不符合预期");
  }
  // 单日记录的历史比较保持原含义：精确偏移、平均偏移为确定值。
  // 四个必需阶段按 rank 20/40/60/80 排列，历史单日偏移应为 5/4/4/5。
  const expectedOffsets = { bud_burst: 5, full_bloom: 4, fruit_set: 4, harvest: 5 };
  for (const row of exactComparison.stage_offsets) {
    if (row.offset_days !== expectedOffsets[row.stage]) {
      throw new Error(`单日历史比较的精确偏移被改变：${row.stage}`);
    }
  }
  if (exactComparison.summary.average_offset_days !== 4.5) {
    throw new Error("单日历史比较的平均偏移应保持 4.5 天");
  }

  // 不确定精度比较：盛花期改为区间，偏移必须保留范围而不是虚构中点。
  const uncertainLeft = await seedCompletedSeason({
    plotCode: "OR-2303",
    plotName: "北沟梨园",
    cultivar: "青梨",
    treeCode: "OR-2303-T01",
    season: "2026",
    dates: ["2026-03-10", "2026-04-01", "2026-04-18", "2026-09-02"],
    stageOverrides: {
      full_bloom: {
        precision: "range",
        observed_on: "2026-04-01",
        observed_end_on: "2026-04-03",
      },
    },
  });
  const uncertainRight = await seedCompletedSeason({
    plotCode: "OR-2304",
    plotName: "东岭梨园",
    cultivar: "酥梨",
    treeCode: "OR-2304-T01",
    season: "2026",
    dates: ["2026-03-15", "2026-04-05", "2026-04-22", "2026-09-07"],
    stageOverrides: {
      full_bloom: {
        precision: "range",
        observed_on: "2026-04-05",
        observed_end_on: "2026-04-09",
      },
    },
  });
  const uncertain = await api("/comparisons", "PUT", {
    title: "区间盛花期对齐",
    left_observation_id: uncertainLeft.id,
    right_observation_id: uncertainRight.id,
  });
  const bloom = uncertain.stage_offsets.find(
    (row) => row.stage === "full_bloom",
  );
  if (bloom.offset_exact !== false || "offset_days" in bloom) {
    throw new Error("区间比较不得给出虚构的精确偏移");
  }
  if (bloom.offset_min_days !== 2 || bloom.offset_max_days !== 8) {
    throw new Error(
      `区间盛花期偏移范围应为 +2 至 +8 天，实际为 ${bloom.offset_min_days} 至 ${bloom.offset_max_days}`,
    );
  }
  if (bloom.left_end_date !== "2026-04-03" || bloom.right_end_date !== "2026-04-09") {
    throw new Error("区间比较未保留两侧结束日期");
  }
  if (uncertain.summary.average_offset_days !== null) {
    throw new Error("含区间阶段时平均偏移必须为空，不能取中点");
  }
  const exactRows = uncertain.stage_offsets.filter((row) => row.offset_exact);
  if (exactRows.length !== 3 || uncertain.summary.exact_stage_count !== 3) {
    throw new Error("其余单日阶段仍应给出精确偏移");
  }

  // 恢复验证：重启后端后，精确与不确定两类比较都保持原结构。
  await stopProcess(backend);
  backend = startBackend(runtimeDir);
  await waitForUrl(`${API_ORIGIN}/api/health`);
  const recoveredComparisons = await api("/comparisons");
  if (recoveredComparisons.items.length !== 2) {
    throw new Error("恢复后对比图谱数量不一致");
  }
  const recoveredExact = recoveredComparisons.items.find(
    (item) => item.title === exactComparison.title,
  );
  const recoveredUncertain = recoveredComparisons.items.find(
    (item) => item.title === uncertain.title,
  );
  if (!recoveredExact || !recoveredUncertain) {
    throw new Error("恢复后找不到原有对比图谱");
  }
  if (recoveredExact.summary.average_offset_days !== 4.5) {
    throw new Error("恢复后单日比较的平均偏移被改变");
  }
  const recoveredBloom = recoveredUncertain.stage_offsets.find(
    (row) => row.stage === "full_bloom",
  );
  if (
    recoveredBloom.offset_exact !== false ||
    recoveredBloom.offset_min_days !== 2 ||
    recoveredBloom.offset_max_days !== 8
  ) {
    throw new Error("恢复后区间偏移范围被丢弃或改写");
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
    const stage = stages[index];
    const override = config.stageOverrides?.[stage];
    const stagePayload = override
      ? { stage, confidence: 4, note: "", ...override }
      : { stage, observed_on: config.dates[index], confidence: 4, note: "" };
    observation = await api(
      `/observations/${observation.id}/stages`,
      "PUT",
      {
        ...stagePayload,
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

function startBackend(runtimeDir) {
  return startProcess("python3", [
    "scripts/run_server.py",
    "--port",
    "8765",
    "--data-dir",
    runtimeDir,
  ]);
}

function startProcess(command, args) {  const child = spawn(command, args, {
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
