<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import {
  CalendarRange,
  Download,
  History,
  ListChecks,
} from "@lucide/vue";
import EmptyState from "../components/EmptyState.vue";
import ChoiceField from "../components/ChoiceField.vue";
import { formatTimestamp } from "../domain/rules";
import type { CohortYearStatus } from "../domain/types";
import { useWorkspace } from "../app/workspace";

const workspace = useWorkspace();
const selectedCohort = workspace.selectedCohort;
const form = reactive({
  treeId: "",
  seasonStart: "",
  seasonEnd: "",
  title: "",
});

const YEAR_STATUS_LABEL: Record<CohortYearStatus, string> = {
  included: "纳入",
  excluded: "排除",
  missing: "缺失",
};

const treeChoices = computed(() => {
  const seen = new Map<string, string>();
  for (const item of workspace.state.observations) {
    if (!seen.has(item.tree_id)) {
      seen.set(item.tree_id, `${item.tree_code} · ${item.cultivar}`);
    }
  }
  return [...seen.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((left, right) => left.label.localeCompare(right.label, "zh-CN"));
});

const treeSeasons = computed(() =>
  workspace.state.observations
    .filter((item) => item.tree_id === form.treeId)
    .map((item) => item.season)
    .sort(),
);

const includedPreview = computed(() => {
  if (!form.treeId || !form.seasonStart || !form.seasonEnd) return 0;
  return workspace.state.observations.filter(
    (item) =>
      item.tree_id === form.treeId &&
      item.status === "completed" &&
      item.season >= form.seasonStart &&
      item.season <= form.seasonEnd,
  ).length;
});

const rangeValid = computed(
  () =>
    /^\d{4}$/.test(form.seasonStart) &&
    /^\d{4}$/.test(form.seasonEnd) &&
    form.seasonStart <= form.seasonEnd,
);

const cohortReady = computed(
  () =>
    Boolean(form.treeId) &&
    rangeValid.value &&
    includedPreview.value > 0 &&
    Boolean(form.title.trim()),
);

watch(
  treeChoices,
  (choices) => {
    if (!form.treeId && choices[0]) form.treeId = choices[0].value;
  },
  { immediate: true },
);

watch(
  () => form.treeId,
  () => {
    const seasons = treeSeasons.value;
    if (seasons.length > 0) {
      form.seasonStart = seasons[0];
      form.seasonEnd = seasons[seasons.length - 1];
    }
    updateTitle();
  },
);

watch(
  () => [form.seasonStart, form.seasonEnd],
  () => updateTitle(),
);

function updateTitle() {
  const label = treeChoices.value.find(
    (choice) => choice.value === form.treeId,
  )?.label;
  if (label && form.seasonStart && form.seasonEnd) {
    form.title = `${label} ${form.seasonStart}—${form.seasonEnd} 年物候队列`;
  }
}

async function createCohort() {
  if (!cohortReady.value) return;
  await workspace.runAction(
    () =>
      workspace.createCohort({
        title: form.title.trim(),
        tree_id: form.treeId,
        season_start: form.seasonStart,
        season_end: form.seasonEnd,
      }),
    "多年队列已生成并保存",
  );
}

async function exportCohort() {
  const current = selectedCohort.value;
  if (!current) return;
  const result = await workspace.runAction(
    () => workspace.exportCohort(current.id),
    "队列文本已导出",
  );
  if (!result) return;
  const blob = new Blob([result.content], { type: result.media_type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = result.filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function formatPoints(points: { season: string; observed_on: string }[]): string {
  if (!points.length) return "—";
  return points
    .map((point) => `${point.season}·${point.observed_on.slice(5)}`)
    .join("，");
}
</script>

<template>
  <section class="cohort-surface">
    <div class="cohort-composer">
      <div class="section-heading">
        <div>
          <span class="eyebrow">跨年比较</span>
          <h2>选择同一植株与年份范围</h2>
        </div>
        <History :size="22" />
      </div>
      <p class="section-lead">
        只统计已完成的季节志；缺失年份不补零，排除年份不参与平均。
      </p>

      <form class="cohort-form" @submit.prevent="createCohort">
        <label>
          <span>比较对象（同一植株）</span>
          <ChoiceField
            v-model="form.treeId"
            data-check="cohort-tree"
            :choices="treeChoices"
            placeholder="请选择植株"
          />
        </label>
        <div class="form-grid form-grid--two">
          <label>
            <span>起始年份</span>
            <input
              v-model="form.seasonStart"
              data-check="cohort-start"
              maxlength="4"
              inputmode="numeric"
            />
          </label>
          <label>
            <span>结束年份</span>
            <input
              v-model="form.seasonEnd"
              data-check="cohort-end"
              maxlength="4"
              inputmode="numeric"
            />
          </label>
        </div>
        <label>
          <span>队列标题</span>
          <input v-model="form.title" data-check="cohort-title" maxlength="100" />
        </label>
        <div
          v-if="form.treeId && rangeValid && includedPreview === 0"
          class="precondition-note"
        >
          该年份范围内没有已完成的季节志，无法建立多年队列。
        </div>
        <button
          type="submit"
          class="button button--primary"
          data-check="create-cohort"
          :disabled="!cohortReady"
        >
          <CalendarRange :size="17" />
          生成多年队列
        </button>
      </form>

      <div class="brief-method">
        <h3>纳入口径</h3>
        <ul>
          <li>纳入：年份内存在已完成的季节志，参与跨年统计。</li>
          <li>排除：季节志未完成，记录不可用，不参与平均。</li>
          <li>缺失：该年份没有季节志记录，不按零值计入。</li>
          <li>不足两个可比年份的阶段不计算均值。</li>
        </ul>
      </div>

      <div v-if="workspace.state.cohorts.length" class="cohort-index">
        <div class="section-heading section-heading--compact">
          <h3>已保存队列</h3>
          <ListChecks :size="17" />
        </div>
        <button
          v-for="cohort in workspace.state.cohorts"
          :key="cohort.id"
          type="button"
          class="cohort-index__item"
          :class="{ 'is-active': cohort.id === workspace.state.selectedCohortId }"
          data-check="cohort-item"
          @click="workspace.selectCohort(cohort.id)"
        >
          <span class="cohort-index__title">{{ cohort.title }}</span>
          <span class="cohort-index__counts">
            纳入 {{ cohort.summary.included_count }} · 排除
            {{ cohort.summary.excluded_count }} · 缺失
            {{ cohort.summary.missing_count }}
          </span>
        </button>
      </div>
    </div>

    <div v-if="selectedCohort" class="cohort-result" data-check="cohort-result">
      <header class="comparison-result__header">
        <div>
          <span class="eyebrow">已保存队列</span>
          <h2>{{ selectedCohort.title }}</h2>
          <p>
            {{ selectedCohort.tree_label }} ·
            {{ selectedCohort.season_start }}—{{ selectedCohort.season_end }} 年 ·
            生成于 {{ formatTimestamp(selectedCohort.created_at) }}
          </p>
        </div>
        <span class="comparison-seal">
          <History :size="16" />
          跨年队列
        </span>
      </header>

      <p class="comparison-sentence" data-check="cohort-sentence">
        {{ selectedCohort.summary.sentence }}
      </p>

      <div class="comparison-stats">
        <div>
          <strong>{{ selectedCohort.summary.included_count }}</strong>
          <span>纳入年份</span>
        </div>
        <div>
          <strong>{{ selectedCohort.summary.excluded_count }}</strong>
          <span>排除年份</span>
        </div>
        <div>
          <strong>{{ selectedCohort.summary.missing_count }}</strong>
          <span>缺失年份</span>
        </div>
        <div>
          <strong>{{ selectedCohort.summary.comparable_stage_count }}</strong>
          <span>可比阶段</span>
        </div>
      </div>

      <h3 class="cohort-section-title">年度明细</h3>
      <div class="cohort-years">
        <div class="cohort-years__head">
          <span>年份</span>
          <span>结论</span>
          <span>原因</span>
        </div>
        <div
          v-for="year in selectedCohort.years"
          :key="year.season"
          class="cohort-years__row"
          data-check="cohort-year-row"
        >
          <strong>{{ year.season }}</strong>
          <span
            class="year-pill"
            :class="`is-${year.status}`"
          >
            {{ YEAR_STATUS_LABEL[year.status] }}
          </span>
          <span>{{ year.reason }}</span>
        </div>
      </div>

      <h3 class="cohort-section-title">阶段序列（仅统计纳入年份）</h3>
      <div class="cohort-stages">
        <div class="cohort-stages__head">
          <span>阶段</span>
          <span>记录点</span>
          <span>平均</span>
          <span>趋势</span>
          <span>阶段缺失</span>
        </div>
        <div
          v-for="stage in selectedCohort.stage_series"
          :key="stage.stage"
          class="cohort-stages__row"
          data-check="cohort-stage-row"
        >
          <strong>{{ stage.label }}</strong>
          <span>{{ formatPoints(stage.points) }}</span>
          <span>
            {{
              stage.comparable && stage.average_season_day !== null
                ? `第 ${stage.average_season_day} 天`
                : "不计算"
            }}
          </span>
          <span>{{ stage.trend }}</span>
          <span>
            {{
              stage.missing_seasons.length
                ? `${stage.missing_seasons.join("、")} 年`
                : "—"
            }}
          </span>
        </div>
      </div>

      <footer class="cohort-result__footer">
        <span>缺失年份不按零值计入，排除年份不参与平均。</span>
        <button
          type="button"
          class="button button--ghost"
          data-check="export-cohort"
          @click="exportCohort"
        >
          <Download :size="16" />
          导出队列文本
        </button>
      </footer>
    </div>

    <EmptyState
      v-else
      title="尚无多年队列"
      description="选择同一植株与年份范围，生成跨年比较队列，逐年解释纳入、排除与缺失原因。"
    />
  </section>
</template>
