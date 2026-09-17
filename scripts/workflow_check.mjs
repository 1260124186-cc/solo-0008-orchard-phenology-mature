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
  console.error("用法：node scripts/workflow_check.mjs --workflow catalog|observe|compare|cohort");
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
    acceptDownloads: true,
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
  } else if (workflow === "cohort") {
    await checkCohort(page);
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

async function checkCohort(page) {
  const { tree } = await seedCatalog("OR-2401", "北沟梨园", "雪花梨");
  await seedSeason(tree, "2023", [
    "2023-03-12",
    "2023-04-02",
    "2023-04-20",
    "2023-09-01",
  ]);
  await seedSeason(tree, "2026", [
    "2026-03-16",
    "2026-04-06",
    "2026-04-25",
    "2026-09-06",
  ]);
  await seedOpenSeason(tree, "2025", ["2025-03-15"]);

  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-check="nav-cohort"]').click();
  await page.locator('[data-check="cohort-tree"]').click();
  await page.locator(`[data-choice-value="${tree.id}"]`).click();
  await page.locator('[data-check="cohort-start"]').fill("2023");
  await page.locator('[data-check="cohort-end"]').fill("2026");
  await page.locator('[data-check="create-cohort"]').click();
  await page.getByText("多年队列已生成并保存").waitFor();
  await page.locator('[data-check="cohort-result"]').waitFor();

  const sentence = await page
    .locator('[data-check="cohort-sentence"]')
    .innerText();
  if (
    !sentence.includes("纳入 2 年") ||
    !sentence.includes("排除 1 年") ||
    !sentence.includes("缺失 1 年")
  ) {
    throw new Error("多年队列摘要未解释纳入、排除与缺失数量");
  }

  const yearRows = page.locator('[data-check="cohort-year-row"]');
  if ((await yearRows.count()) !== 4) {
    throw new Error("页面未展示四个年份的明细行");
  }
  const rowTexts = await yearRows.allInnerTexts();
  const rowOf = (season) =>
    rowTexts.find((text) => text.includes(`${season}`)) ?? "";
  if (!rowOf("2023").includes("纳入") || !rowOf("2026").includes("纳入")) {
    throw new Error("已完成年份未标记为纳入");
  }
  if (!rowOf("2024").includes("缺失")) {
    throw new Error("无记录年份未标记为缺失");
  }
  if (!rowOf("2025").includes("排除") || !rowOf("2025").includes("记录不可用")) {
    throw new Error("未完成季节志未标记为排除");
  }

  const stageRows = await page
    .locator('[data-check="cohort-stage-row"]')
    .allInnerTexts();
  const budBurst = stageRows.find((text) => text.includes("萌芽期")) ?? "";
  if (!budBurst.includes("第 73 天") || budBurst.includes("2025")) {
    throw new Error("阶段序列把缺失年份计入了统计");
  }
  const budSwell = stageRows.find((text) => text.includes("芽膨大期")) ?? "";
  if (!budSwell.includes("不计算")) {
    throw new Error("数据不足的阶段被强行求平均");
  }

  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 8000 }),
    page.locator('[data-check="export-cohort"]').click(),
  ]);
  if (!download.suggestedFilename().includes("多年队列")) {
    throw new Error("导出文件名不符合预期");
  }

  const cohorts = await api("/cohorts");
  if (cohorts.items.length !== 1) {
    throw new Error("服务端未保存多年队列");
  }
  const item = cohorts.items[0];
  if (
    item.summary.included_count !== 2 ||
    item.summary.excluded_count !== 1 ||
    item.summary.missing_count !== 1
  ) {
    throw new Error("比较列表的纳入统计不符合预期");
  }
  const detail = await api(`/cohorts/${item.id}`);
  const year2024 = detail.years.find((year) => year.season === "2024");
  const year2025 = detail.years.find((year) => year.season === "2025");
  if (year2024?.reason_code !== "missing_year") {
    throw new Error("详情未把无记录年份标记为年份缺失");
  }
  if (year2025?.reason_code !== "record_incomplete") {
    throw new Error("详情未把未完成季节志标记为记录不可用");
  }
  const series = detail.stage_series.find((stage) => stage.stage === "bud_burst");
  if (
    !series?.comparable ||
    series.points.some((point) => point.season === "2024" || point.season === "2025")
  ) {
    throw new Error("阶段序列混入了缺失或排除年份");
  }
  const exportDoc = await api(`/cohorts/${item.id}/export`);
  if (
    !exportDoc.content.includes("不按零值计入") ||
    !exportDoc.content.includes("记录不可用") ||
    !exportDoc.content.includes("2024｜缺失｜")
  ) {
    throw new Error("导出文本未解释纳入标准");
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
  const observation = await seedSeason(tree, config.season, config.dates);
  if (!plot || observation.status !== "completed") {
    throw new Error("准备比较数据失败");
  }
  return observation;
}

async function seedSeason(tree, season, dates) {
  let observation = await seedOpenSeason(tree, season, dates);
  observation = await api(
    `/observations/${observation.id}/complete`,
    "PUT",
    { revision: observation.revision },
  );
  return observation;
}

async function seedOpenSeason(tree, season, dates) {
  let observation = await api("/observations", "PUT", {
    tree_id: tree.id,
    season,
    observer: "对比检查组",
    note: "",
  });
  const stages = ["bud_burst", "full_bloom", "fruit_set", "harvest"];
  for (let index = 0; index < dates.length; index += 1) {
    observation = await api(
      `/observations/${observation.id}/stages`,
      "PUT",
      {
        stage: stages[index],
        observed_on: dates[index],
        confidence: 4,
        note: "",
        revision: observation.revision,
      },
    );
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
