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
  } else if (workflow === "tree-status") {
    await checkTreeStatus(page);
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

async function checkTreeStatus(page) {
  // 准备：草稿园区 + 植株 + 历史季节志，随后确认园区
  const plot = await api("/plots", "PUT", {
    code: "OR-2401",
    name: "北坞恢复试验园",
    locality: "山谷传统种植区",
    cultivar_focus: "蜜梨",
    steward: "物候档案组",
    planting_year: 2011,
    note: "用于植株状态连续性检查",
  });
  const tree = await api("/trees", "PUT", {
    plot_id: plot.id,
    code: "OR-2401-T01",
    cultivar: "蜜梨",
    rootstock: "杜梨",
    planting_year: 2011,
    status: "active",
    note: "东边坡地第三株，梯田边",
  });
  let season = await api("/observations", "PUT", {
    tree_id: tree.id,
    season: "2025",
    observer: "周岚",
    note: "误关闭前的历史季节志",
  });
  const stages = ["bud_burst", "full_bloom", "fruit_set", "harvest"];
  const dates = ["2025-03-12", "2025-04-03", "2025-04-20", "2025-09-05"];
  for (let index = 0; index < stages.length; index += 1) {
    season = await api(`/observations/${season.id}/stages`, "PUT", {
      stage: stages[index],
      observed_on: dates[index],
      confidence: 4,
      note: "",
      revision: season.revision,
    });
  }
  season = await api(`/observations/${season.id}/complete`, "PUT", {
    revision: season.revision,
  });
  await api(`/plots/${plot.id}/confirm`, "PUT", { revision: plot.revision });

  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-check="nav-catalog"]').click();
  await page.locator('[data-check="tree-card"]').first().click();
  await page.locator('[data-check="tree-status-panel"]').waitFor();

  // 关闭：选择遗失，填写原因与依据
  await page.locator('[data-check="tree-status-choice-lost"]').click();
  await page
    .locator('[data-check="tree-status-reason"]')
    .fill("连续两季巡查未见萌发，现场判定枯死");
  await page
    .locator('[data-check="tree-status-evidence"]')
    .fill("2026-04-02 现场照片与管护员笔录");
  await page.locator('[data-check="tree-status-submit"]').click();
  await page.getByText("植株已标记为遗失，历史观察全部保留").waitFor();
  await expectText(
    page,
    '[data-check="tree-status-stamp"]',
    "已遗失",
    "植株详情未显示已遗失状态",
  );
  await page
    .locator('[data-check="tree-status-event-2"]')
    .getByText("连续两季巡查未见萌发")
    .waitFor();

  // 植株详情：仍只有同一株，状态为 lost
  let plotDetail = await api(`/plots/${plot.id}`);
  if (plotDetail.trees.length !== 1 || plotDetail.trees[0].id !== tree.id) {
    throw new Error("关闭后植株被新建或丢失，身份不连续");
  }
  if (plotDetail.trees[0].status !== "lost") {
    throw new Error("服务端植株状态未变为 lost");
  }
  if (plotDetail.trees[0].note !== "东边坡地第三株，梯田边") {
    throw new Error("关闭操作未传备注，却清空了原植株备注");
  }
  expectTransitionLabels(
    plotDetail.trees[0].status_history,
    [[null, "在册"], ["在册", "已遗失"]],
    "关闭后植株详情内嵌沿革",
  );

  // 观察记录：历史季节志仍指向同一植株（id 与编号均不变）
  let observations = await api(`/observations?tree_id=${encodeURIComponent(tree.id)}`);
  if (
    observations.total !== 1 ||
    observations.items[0].id !== season.id ||
    observations.items[0].tree_code !== "OR-2401-T01"
  ) {
    throw new Error("关闭后历史观察记录丢失或改挂到其他植株");
  }

  // 简报冻结遗失时点：之后恢复不改变简报中的同一株
  const brief = await api(`/plots/${plot.id}/briefs`, "PUT", {
    title: "OR-2401 北坞恢复试验园 物候档案摘编",
  });
  if (brief.tree_count !== 1 || brief.payload?.trees[0].id !== tree.id) {
    throw new Error("简报未冻结同一植株");
  }
  if (brief.payload.trees[0].status !== "lost") {
    throw new Error("简报未冻结植株遗失时点的状态");
  }

  // 恢复在册
  await page
    .locator('[data-check="tree-status-reason"]')
    .fill("复查发现根蘖重新萌发，原枯死判定为误操作");
  await page
    .locator('[data-check="tree-status-evidence"]')
    .fill("2026-05-10 复查照片、管护员签字确认");
  await page.locator('[data-check="tree-status-submit"]').click();
  await page.getByText("植株已恢复为在册，沿用同一档案与历史观察").waitFor();
  await expectText(
    page,
    '[data-check="tree-status-stamp"]',
    "在册",
    "植株详情未恢复显示在册状态",
  );

  // 沿革完整：active -> lost -> active，顺序关系可解释
  const historyApi = await api(`/trees/${encodeURIComponent(tree.id)}/status`);
  if (historyApi.total !== 3) {
    throw new Error(`状态沿革应为 3 条，实际 ${historyApi.total}`);
  }
  const chain = historyApi.items.map((event) => event.status).join("->");
  if (chain !== "active->lost->active") {
    throw new Error(`状态沿革先后关系错误：${chain}`);
  }
  if (
    historyApi.items[2].previous_status !== "lost" ||
    !historyApi.items[2].reason.includes("误操作")
  ) {
    throw new Error("恢复事件未说明来源状态与误操作原因");
  }

  // 恢复后没有凭空多出植株
  plotDetail = await api(`/plots/${plot.id}`);
  if (plotDetail.trees.length !== 1 || plotDetail.trees[0].id !== tree.id) {
    throw new Error("恢复后植株数量变化，出现了新对象");
  }
  if (plotDetail.trees[0].status !== "active") {
    throw new Error("恢复后服务端植株状态不是 active");
  }
  if (plotDetail.trees[0].note !== "东边坡地第三株，梯田边") {
    throw new Error("恢复操作未传备注，却清空了原植株备注");
  }
  expectTransitionLabels(
    plotDetail.trees[0].status_history,
    [[null, "在册"], ["在册", "已遗失"], ["已遗失", "在册"]],
    "恢复后植株详情内嵌沿革",
  );

  // 接口返回：历史沿革接口的标签同样完整
  expectTransitionLabels(
    historyApi.items.map((event) => ({
      previous_status_label: event.previous_status_label,
      status_label: event.status_label,
    })),
    [[null, "在册"], ["在册", "已遗失"], ["已遗失", "在册"]],
    "状态沿革接口",
  );

  // 再次关闭再恢复：验证第二次往返后痕迹与备注仍连续
  let current = plotDetail.trees[0];
  current = await api(`/trees/${encodeURIComponent(tree.id)}/status`, "PUT", {
    status: "retired",
    reason: "更新复壮，暂时退出常规观察",
    evidence: "2026-06-01 管护记录",
    revision: current.revision,
  });
  if (current.note !== "东边坡地第三株，梯田边") {
    throw new Error("再次关闭清空了原植株备注");
  }
  current = await api(`/trees/${encodeURIComponent(tree.id)}/status`, "PUT", {
    status: "active",
    reason: "复壮后恢复常规观察",
    evidence: "2026-09-01 复查记录",
    revision: current.revision,
  });
  if (current.status !== "active" || current.id !== tree.id) {
    throw new Error("再次恢复后植株身份或状态异常");
  }
  if (current.note !== "东边坡地第三株，梯田边") {
    throw new Error("再次恢复清空了原植株备注");
  }
  expectTransitionLabels(
    current.status_history,
    [
      [null, "在册"],
      ["在册", "已遗失"],
      ["已遗失", "在册"],
      ["在册", "已退休"],
      ["已退休", "在册"],
    ],
    "再次恢复后的完整沿革",
  );

  // 重新打开页面：只依赖植株详情内嵌沿革，必须仍按真实前后状态展示
  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-check="nav-catalog"]').click();
  await page.locator('[data-check="tree-card"]').first().click();
  await page.locator('[data-check="tree-status-panel"]').waitFor();
  const transitionTexts = await page
    .locator('[data-check="tree-status-timeline"] .status-timeline__transition')
    .allInnerTexts();
  const expectedTransitions = [
    "已退休 → 在册",
    "在册 → 已退休",
    "已遗失 → 在册",
    "在册 → 已遗失",
  ];
  for (const expected of expectedTransitions) {
    if (!transitionTexts.some((text) => text.replace(/\s/g, "").includes(expected.replace(/\s/g, "")))) {
      throw new Error(
        `重新打开页面后沿革缺少“${expected}”，实际：${JSON.stringify(transitionTexts)}`,
      );
    }
  }
  if (transitionTexts.some((text) => text.includes("建档入册"))) {
    throw new Error(
      `重新打开页面后真实状态变化退化成了建档入册：${JSON.stringify(transitionTexts)}`,
    );
  }
  const panelNote = await page.locator(".tree-status-panel__note").innerText();
  if (!panelNote.includes("东边坡地第三株")) {
    throw new Error(`重新打开页面后植株备注未保留：${panelNote}`);
  }

  // 观察记录与历史结果仍然指向同一植株
  observations = await api(`/observations?tree_id=${encodeURIComponent(tree.id)}`);
  if (
    observations.total !== 1 ||
    observations.items[0].id !== season.id ||
    observations.items[0].tree_code !== "OR-2401-T01"
  ) {
    throw new Error("恢复后历史观察记录与植株的关联被破坏");
  }
  const frozenBrief = await api(`/briefs/${brief.id}`);
  if (
    frozenBrief.payload.trees.length !== 1 ||
    frozenBrief.payload.trees[0].id !== tree.id ||
    frozenBrief.payload.trees[0].status !== "lost"
  ) {
    throw new Error("历史简报中的植株身份或冻结状态被状态变化改写");
  }

  // 恢复后可以为同一株建立新季节志
  const renewed = await api("/observations", "PUT", {
    tree_id: tree.id,
    season: "2026",
    observer: "周岚",
    note: "恢复在册后的新季节志",
  });
  if (renewed.tree_id !== tree.id || renewed.tree_code !== "OR-2401-T01") {
    throw new Error("恢复后新季节志未挂到原植株");
  }

  // 页面沿革展示五次状态记录
  const timelineRows = page.locator('[data-check^="tree-status-event-"]');
  if ((await timelineRows.count()) !== 5) {
    throw new Error("植株详情未展示完整的五次状态记录");
  }

  // 观察工作面切换到该植株时，历史季节志仍以原编号呈现
  await page.locator('[data-check="nav-observation"]').click();
  const oldSeasonItem = page
    .locator('[data-check="observation-list-item"]')
    .filter({ hasText: "OR-2401-T01" });
  if ((await oldSeasonItem.count()) < 1) {
    throw new Error("观察记录工作面找不到原植株编号的历史季节志");
  }
}

async function expectText(page, selector, expected, message) {
  const value = await page.locator(selector).innerText();
  if (!value.includes(expected)) {
    throw new Error(`${message}（实际：${value}）`);
  }
}

function expectTransitionLabels(events, expected, label) {
  const actual = events.map((event) => [
    event.previous_status_label ?? null,
    event.status_label,
  ]);
  if (actual.length !== expected.length || actual.some((row, i) => row[0] !== expected[i][0] || row[1] !== expected[i][1])) {
    throw new Error(`${label}标签不正确：${JSON.stringify(actual)}`);
  }
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
