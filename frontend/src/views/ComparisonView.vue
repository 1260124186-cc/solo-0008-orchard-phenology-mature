<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import {
  ArrowLeftRight,
  CalendarCheck2,
  CalendarRange,
  Download,
  GitCompareArrows,
  ListChecks,
  MoveHorizontal,
} from "@lucide/vue";
import EmptyState from "../components/EmptyState.vue";
import ChoiceField from "../components/ChoiceField.vue";
import StageTrack from "../components/StageTrack.vue";
import { canCompareObservations, formatOffset, formatTimestamp } from "../domain/rules";
import { stageLabel } from "../domain/stages";
import type { SeriesDisposition } from "../domain/types";
import { useWorkspace } from "../app/workspace";

const workspace = useWorkspace();
const selectedComparison = workspace.selectedComparison;
const selectedSeries = workspace.selectedSeries;
const form = reactive({
  leftId: "",
  rightId: "",
  title: "",
});
const seriesForm = reactive({
  treeId: "",
  seasonFrom: "",
  seasonTo: "",
  title: "",
});

const completed = computed(() =>
  workspace.state.observations.filter((item) => item.status === "completed"),
);

const leftObservation = computed(
  () => completed.value.find((item) => item.id === form.leftId) ?? null,
);
const rightObservation = computed(
  () => completed.value.find((item) => item.id === form.rightId) ?? null,
);

const comparisonReady = computed(() =>
  canCompareObservations(leftObservation.value, rightObservation.value),
);
const observationChoices = computed(() =>
  completed.value.map((item) => ({
    value: item.id,
    label: `${item.season} · ${item.tree_code} · ${item.cultivar}`,
    disabled: item.id === form.rightId || item.id === form.leftId,
  })),
);
const leftChoices = computed(() =>
  observationChoices.value.map((item) => ({
    ...item,
    disabled: item.value === form.rightId,
  })),
);
const rightChoices = computed(() =>
  observationChoices.value.map((item) => ({
    ...item,
    disabled: item.value === form.leftId,
  })),
);

watch(
  completed,
  (items) => {
    if (!form.leftId && items[0]) form.leftId = items[0].id;
    if ((!form.rightId || form.rightId === form.leftId) && items[1]) {
      form.rightId = items[1].id;
    }
    if (!form.title && items[0] && items[1]) {
      form.title = `${items[0].season} 年 ${items[0].cultivar} 与 ${items[1].cultivar} 物候对齐`;
    }
  },
  { immediate: true },
);

watch(
  () => [form.leftId, form.rightId, leftObservation.value?.cultivar, rightObservation.value?.cultivar],
  () => {
    if (leftObservation.value && rightObservation.value) {
      form.title = `${leftObservation.value.season} 年 ${leftObservation.value.cultivar} 与 ${rightObservation.value.cultivar} 物候对齐`;
    }
  },
);

async function createComparison() {
  if (!comparisonReady.value) return;
  await workspace.runAction(
    () =>
      workspace.createComparison({
        title: form.title,
        left_observation_id: form.leftId,
        right_observation_id: form.rightId,
      }),
    "对比图谱已生成并保存",
  );
}

const seriesTreeChoices = computed(() => {
  const seen = new Map<string, string>();
  for (const item of workspace.state.observations) {
    if (!seen.has(item.tree_id)) {
      seen.set(item.tree_id, `${item.tree_code} · ${item.cultivar}`);
    }
  }
  return [...seen.entries()].map(([value, label]) => ({ value, label }));
});

const seriesIssue = computed(() => {
  if (!seriesForm.treeId) return "请选择植株。";
  if (
    !/^\d{4}$/.test(seriesForm.seasonFrom) ||
    !/^\d{4}$/.test(seriesForm.seasonTo)
  ) {
    return "起始与结束年份必须是四位数字。";
  }
  if (Number(seriesForm.seasonFrom) > Number(seriesForm.seasonTo)) {
    return "起始年份不能晚于结束年份。";
  }
  if (Number(seriesForm.seasonTo) - Number(seriesForm.seasonFrom) + 1 > 30) {
    return "多年比较的年份跨度最多 30 年。";
  }
  if (!seriesForm.title.trim()) return "请填写比较标题。";
  return "";
});
const seriesReady = computed(() => !seriesIssue.value);

watch(
  () => [seriesForm.treeId, workspace.state.observations.length],
  () => {
    if (!seriesForm.treeId && seriesTreeChoices.value[0]) {
      seriesForm.treeId = String(seriesTreeChoices.value[0].value);
    }
    const seasons = workspace.state.observations
      .filter((item) => item.tree_id === seriesForm.treeId)
      .map((item) => Number(item.season))
      .filter((value) => Number.isInteger(value))
      .sort((left, right) => left - right);
    if (seriesForm.treeId && seasons.length) {
      seriesForm.seasonFrom = String(seasons[0]);
      seriesForm.seasonTo = String(seasons[seasons.length - 1]);
    }
  },
  { immediate: true },
);

watch(
  () => [seriesForm.treeId, seriesForm.seasonFrom, seriesForm.seasonTo],
  () => {
    const choice = seriesTreeChoices.value.find(
      (item) => item.value === seriesForm.treeId,
    );
    if (choice && seriesForm.seasonFrom && seriesForm.seasonTo) {
      seriesForm.title = `${choice.label} ${seriesForm.seasonFrom}–${seriesForm.seasonTo} 年多年比较`;
    }
  },
);

async function createSeries() {
  if (!seriesReady.value) return;
  await workspace.runAction(
    () =>
      workspace.createSeries({
        title: seriesForm.title.trim(),
        tree_id: seriesForm.treeId,
        season_from: seriesForm.seasonFrom,
        season_to: seriesForm.seasonTo,
      }),
    "多年比较已生成并保存",
  );
}

function dispositionLabel(disposition: SeriesDisposition): string {
  if (disposition === "included") return "纳入";
  if (disposition === "excluded") return "排除";
  return "缺失";
}

function formatDayOfYear(value: number | null): string {
  return value === null ? "—" : `第 ${value} 天`;
}

function formatShift(value: number | null): string {
  return value === null ? "—" : formatOffset(value);
}

function exportSeries() {
  const record = selectedSeries.value;
  if (!record) return;
  const lines = [
    record.title,
    `对象：${record.tree_label}`,
    `年份范围：${record.season_from}–${record.season_to}`,
    `生成时间：${formatTimestamp(record.created_at)}`,
    "",
    "纳入标准",
    ...record.criteria.map((rule) => `- ${rule}`),
    "",
    "年份处置",
    ...record.years.map((year) => {
      const parts = [
        year.season,
        dispositionLabel(year.disposition),
        year.reason,
      ];
      if (year.stage_count !== null) parts.push(`阶段 ${year.stage_count} 个`);
      if (year.missing_stages?.length) {
        parts.push(`缺阶段：${year.missing_stages.map(stageLabel).join("、")}`);
      }
      return parts.join("｜");
    }),
    "",
    "阶段序列（仅统计纳入年份）",
    ...record.stage_series.map((row) => {
      const covered = row.covered_seasons.length
        ? row.covered_seasons.join("、")
        : "无";
      const average =
        row.average_day_of_year !== null
          ? `平均第 ${row.average_day_of_year} 天`
          : "无可统计年份";
      const shift =
        row.shift_days !== null
          ? `首末偏移 ${formatOffset(row.shift_days)}`
          : "首末偏移不适用";
      const missing = row.missing_seasons.length
        ? `缺阶段年份：${row.missing_seasons.join("、")}`
        : "无缺阶段年份";
      return `${row.label}｜覆盖 ${covered}｜${average}｜${shift}｜${missing}`;
    }),
    "",
    "汇总",
    record.summary.sentence,
  ];
  const blob = new Blob([lines.join("\n")], {
    type: "text/plain;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${record.tree_label.split(" ")[0]}-${record.season_from}-${record.season_to}-多年比较.txt`;
  anchor.click();
  URL.revokeObjectURL(url);
}
</script>

<template>
  <section class="comparison-surface">
    <div class="comparison-composer">
      <div class="section-heading">
        <div>
          <span class="eyebrow">季节对齐</span>
          <h2>选择两份已完成季节志</h2>
        </div>
        <GitCompareArrows :size="22" />
      </div>
      <p class="section-lead">
        只比较双方共同记录到的阶段；缺失项跳过，不进行推断。
      </p>

      <form class="comparison-form" @submit.prevent="createComparison">
        <div class="comparison-selectors">
          <label>
            <span>左侧季节志</span>
            <ChoiceField
              v-model="form.leftId"
              data-check="left-observation"
              :choices="leftChoices"
              placeholder="请选择季节志"
            />
          </label>
          <ArrowLeftRight :size="20" />
          <label>
            <span>右侧季节志</span>
            <ChoiceField
              v-model="form.rightId"
              data-check="right-observation"
              :choices="rightChoices"
              placeholder="请选择季节志"
            />
          </label>
        </div>
        <label>
          <span>图谱标题</span>
          <input
            v-model="form.title"
            data-check="comparison-title"
            maxlength="100"
          />
        </label>
        <div v-if="leftObservation && rightObservation && !comparisonReady" class="precondition-note">
          {{
            leftObservation.season !== rightObservation.season
              ? "两份季节志年份不同，无法对齐。"
              : "两份季节志没有共同阶段。"
          }}
        </div>
        <button
          type="submit"
          class="button button--primary"
          data-check="create-comparison"
          :disabled="!comparisonReady"
        >
          <GitCompareArrows :size="17" />
          生成对比图谱
        </button>
      </form>
    </div>

    <div v-if="selectedComparison" class="comparison-result" data-check="comparison-result">
      <header class="comparison-result__header">
        <div>
          <span class="eyebrow">已保存图谱</span>
          <h2>{{ selectedComparison.title }}</h2>
          <p>{{ formatTimestamp(selectedComparison.created_at) }}</p>
        </div>
        <span class="comparison-seal">
          <CalendarCheck2 :size="18" />
          {{ selectedComparison.season }}
        </span>
      </header>

      <p class="comparison-sentence" data-check="comparison-sentence">
        {{ selectedComparison.summary.sentence }}
      </p>

      <div class="comparison-pair">
        <div>
          <span>左侧</span>
          <strong>{{ selectedComparison.left_label }}</strong>
        </div>
        <MoveHorizontal :size="21" />
        <div>
          <span>右侧</span>
          <strong>{{ selectedComparison.right_label }}</strong>
        </div>
      </div>

      <div class="offset-book">
        <div class="offset-book__head">
          <span>阶段</span>
          <span>左侧日期</span>
          <span>右侧日期</span>
          <span>相对偏移</span>
          <span>置信差</span>
        </div>
        <div
          v-for="offset in selectedComparison.stage_offsets"
          :key="offset.stage"
          class="offset-book__row"
          data-check="offset-row"
        >
          <strong>{{ offset.label }}</strong>
          <span>{{ offset.left_date }}</span>
          <span>{{ offset.right_date }}</span>
          <span
            class="offset-pill"
            :class="{
              'is-early': offset.offset_days < 0,
              'is-late': offset.offset_days > 0,
            }"
          >
            {{ formatOffset(offset.offset_days) }}
          </span>
          <span>{{ offset.confidence_gap }}</span>
        </div>
      </div>

      <div class="comparison-stats">
        <div>
          <strong>{{ selectedComparison.summary.common_stage_count }}</strong>
          <span>共同阶段</span>
        </div>
        <div>
          <strong>{{ selectedComparison.summary.average_offset_days }}</strong>
          <span>平均偏移 / 天</span>
        </div>
        <div>
          <strong>{{ selectedComparison.summary.direction }}</strong>
          <span>总体方向</span>
        </div>
        <div>
          <strong>{{ selectedComparison.summary.stability }}</strong>
          <span>离散判断</span>
        </div>
      </div>

      <div class="comparison-tracks">
        <div>
          <span>{{ selectedComparison.left_label }}</span>
          <StageTrack
            :observation="
              completed.find(
                (item) => item.id === selectedComparison?.left_observation_id,
              ) ?? null
            "
            compact
          />
        </div>
        <div>
          <span>{{ selectedComparison.right_label }}</span>
          <StageTrack
            :observation="
              completed.find(
                (item) => item.id === selectedComparison?.right_observation_id,
              ) ?? null
            "
            compact
          />
        </div>
      </div>
    </div>

    <EmptyState
      v-else
      title="尚无对比图谱"
      description="完成两份同年季节志后，可以选择并生成确定性对齐结果。"
    />
  </section>

  <section class="series-surface">
    <div class="series-composer">
      <div class="section-heading">
        <div>
          <span class="eyebrow">多年比较</span>
          <h2>同一植株跨年份比较</h2>
        </div>
        <CalendarRange :size="22" />
      </div>
      <p class="section-lead">
        按年份范围评估同一植株的季节志：完成年份纳入、未完成排除、无记录缺失；
        缺失年份不按零值处理，不可比年份不参与平均。
      </p>

      <form class="series-form" @submit.prevent="createSeries">
        <label>
          <span>植株</span>
          <ChoiceField
            v-model="seriesForm.treeId"
            data-check="series-tree"
            :choices="seriesTreeChoices"
            placeholder="请选择植株"
          />
        </label>
        <div class="series-range">
          <label>
            <span>起始年份</span>
            <input
              v-model="seriesForm.seasonFrom"
              data-check="series-from"
              maxlength="4"
              inputmode="numeric"
            />
          </label>
          <label>
            <span>结束年份</span>
            <input
              v-model="seriesForm.seasonTo"
              data-check="series-to"
              maxlength="4"
              inputmode="numeric"
            />
          </label>
        </div>
        <label>
          <span>比较标题</span>
          <input
            v-model="seriesForm.title"
            data-check="series-title"
            maxlength="100"
          />
        </label>
        <div v-if="seriesForm.treeId && seriesIssue" class="precondition-note">
          {{ seriesIssue }}
        </div>
        <button
          type="submit"
          class="button button--primary"
          data-check="create-series"
          :disabled="!seriesReady"
        >
          <CalendarRange :size="17" />
          生成多年比较
        </button>
      </form>

      <div
        v-if="workspace.state.series.length"
        class="series-list"
        data-check="series-list"
      >
        <h3>已保存的多年比较</h3>
        <button
          v-for="item in workspace.state.series"
          :key="item.id"
          type="button"
          class="series-list__item"
          :class="{ 'is-active': item.id === workspace.state.selectedSeriesId }"
          data-check="series-list-item"
          @click="workspace.selectSeries(item.id)"
        >
          <strong>{{ item.title }}</strong>
          <span>
            {{ item.season_from }}–{{ item.season_to }} ·
            纳入 {{ item.summary.included_count }} ·
            排除 {{ item.summary.excluded_count }} ·
            缺失 {{ item.summary.missing_count }}
          </span>
        </button>
      </div>
    </div>

    <div v-if="selectedSeries" class="series-result" data-check="series-result">
      <header class="comparison-result__header">
        <div>
          <span class="eyebrow">多年比较</span>
          <h2>{{ selectedSeries.title }}</h2>
          <p>{{ formatTimestamp(selectedSeries.created_at) }}</p>
        </div>
        <span class="comparison-seal">
          <CalendarRange :size="18" />
          {{ selectedSeries.season_from }}–{{ selectedSeries.season_to }}
        </span>
      </header>

      <p class="comparison-sentence" data-check="series-sentence">
        {{ selectedSeries.summary.sentence }}
      </p>

      <div class="series-criteria" data-check="series-criteria">
        <h3>
          <ListChecks :size="16" />
          纳入标准
        </h3>
        <ul>
          <li
            v-for="rule in selectedSeries.criteria"
            :key="rule"
            data-check="series-criterion"
          >
            {{ rule }}
          </li>
        </ul>
      </div>

      <div class="series-years" data-check="series-years">
        <div class="series-years__head">
          <span>年份</span>
          <span>处置</span>
          <span>原因</span>
          <span>已录阶段</span>
        </div>
        <div
          v-for="year in selectedSeries.years"
          :key="year.season"
          class="series-years__row"
          data-check="series-year-row"
        >
          <strong>{{ year.season }}</strong>
          <span class="disposition-pill" :class="`is-${year.disposition}`">
            {{ dispositionLabel(year.disposition) }}
          </span>
          <span>{{ year.reason }}</span>
          <span>{{ year.stage_count === null ? "—" : `${year.stage_count} 个` }}</span>
        </div>
      </div>

      <div class="series-stages" data-check="series-stages">
        <div class="series-stages__head">
          <span>阶段</span>
          <span>覆盖年份</span>
          <span>平均日序</span>
          <span>首末偏移</span>
          <span>缺阶段年份</span>
        </div>
        <div
          v-for="row in selectedSeries.stage_series"
          :key="row.stage"
          class="series-stages__row"
          data-check="series-stage-row"
        >
          <strong>{{ row.label }}</strong>
          <span>
            {{ row.covered_seasons.length ? row.covered_seasons.join("、") : "—" }}
          </span>
          <span>{{ formatDayOfYear(row.average_day_of_year) }}</span>
          <span>{{ formatShift(row.shift_days) }}</span>
          <span>
            {{ row.missing_seasons.length ? row.missing_seasons.join("、") : "无" }}
          </span>
        </div>
      </div>

      <div class="comparison-stats">
        <div>
          <strong>{{ selectedSeries.summary.included_count }}</strong>
          <span>纳入年份</span>
        </div>
        <div>
          <strong>{{ selectedSeries.summary.excluded_count }}</strong>
          <span>排除年份</span>
        </div>
        <div>
          <strong>{{ selectedSeries.summary.missing_count }}</strong>
          <span>缺失年份</span>
        </div>
        <div>
          <strong>{{ selectedSeries.summary.evaluated_year_count }}</strong>
          <span>评估年份</span>
        </div>
      </div>

      <footer class="series-result__footer">
        <span>本地档案快照，缺失与排除年份不参与统计。</span>
        <button
          type="button"
          class="button button--ghost"
          data-check="export-series"
          @click="exportSeries"
        >
          <Download :size="16" />
          下载文本
        </button>
      </footer>
    </div>

    <EmptyState
      v-else
      title="尚无多年比较"
      description="选择植株与年份范围后，可以生成带纳入、排除和缺失说明的多年比较。"
    />
  </section>
</template>
