<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from "vue";
import {
  ArrowLeftRight,
  CalendarCheck2,
  GitBranch,
  GitCompareArrows,
  History,
  MoveHorizontal,
  RefreshCcw,
} from "@lucide/vue";
import EmptyState from "../components/EmptyState.vue";
import ChoiceField from "../components/ChoiceField.vue";
import StageTrack from "../components/StageTrack.vue";
import {
  canCompareObservations,
  formatOffset,
  formatTimestamp,
} from "../domain/rules";
import type { ComparisonSummary } from "../domain/types";
import { useWorkspace } from "../app/workspace";
import { ApiError } from "../services/api";

const workspace = useWorkspace();
const selectedComparison = workspace.selectedComparison;
const form = reactive({
  leftId: "",
  rightId: "",
  title: "",
  supersedesId: "",
});
const supersedePrompt = ref<ComparisonSummary | null>(null);

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
    label:
      `${item.season} · ${item.tree_code} · ${item.cultivar}` +
      (item.has_corrections
        ? `（已勘误 ${item.current_correction_seq} 次）`
        : ""),
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

const currentComparisons = computed(() =>
  workspace.state.comparisons.filter(
    (item) => item.basis_status === "current",
  ),
);
const historicalComparisons = computed(() =>
  workspace.state.comparisons.filter(
    (item) => item.basis_status === "superseded",
  ),
);

const programmaticSetup = ref(false);

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
  () => [form.leftId, form.rightId],
  () => {
    if (programmaticSetup.value) return;
    form.supersedesId = "";
    supersedePrompt.value = null;
  },
);

watch(
  () => [
    form.leftId,
    form.rightId,
    leftObservation.value?.cultivar,
    rightObservation.value?.cultivar,
  ],
  () => {
    if (leftObservation.value && rightObservation.value && !form.supersedesId) {
      form.title = `${leftObservation.value.season} 年 ${leftObservation.value.cultivar} 与 ${rightObservation.value.cultivar} 物候对齐`;
    }
  },
);

async function createComparisonGuarded() {
  if (!comparisonReady.value) return;
  const payload: Record<string, unknown> = {
    title: form.title,
    left_observation_id: form.leftId,
    right_observation_id: form.rightId,
  };
  if (form.supersedesId) {
    payload.supersedes_comparison_id = form.supersedesId;
  }
  try {
    const result = await workspace.createComparison(payload);
    workspace.pushNotice(
      "success",
      form.supersedesId
        ? "新版对比图谱已生成，旧图谱保留为历史事实"
        : "对比图谱已生成并保存",
    );
    if (result) {
      form.supersedesId = "";
      supersedePrompt.value = null;
    }
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.failure.code === "comparison_basis_superseded"
    ) {
      const existingId = String(
        error.failure.details.existing_comparison_id ?? "",
      );
      supersedePrompt.value =
        workspace.state.comparisons.find((item) => item.id === existingId) ??
        null;
      form.supersedesId = existingId;
      workspace.pushNotice(
        "error",
        "源季节志已有勘误，旧图谱基于冻结事实，不能悄悄重算；请显式生成新版图谱。",
        6000,
      );
    } else {
      workspace.pushNotice(
        "error",
        error instanceof Error ? error.message : "生成对比图谱失败",
      );
    }
  }
}

function renewFrom(comparison: ComparisonSummary) {
  programmaticSetup.value = true;
  form.leftId = comparison.left_observation_id;
  form.rightId = comparison.right_observation_id;
  form.supersedesId = comparison.id;
  supersedePrompt.value = comparison;
  form.title = `${comparison.title}（勘误新版）`;
  void nextTick(() => {
    programmaticSetup.value = false;
  });
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
        只比较双方共同记录到的阶段；缺失项跳过，不进行推断。比较始终基于当前采用的事实，生成时冻结所依据的勘误世代。
      </p>

      <form class="comparison-form" @submit.prevent="createComparisonGuarded">
        <div class="comparison-selectors">
          <label>
            <span>左侧季节志</span>
            <ChoiceField
              v-model="form.leftId"
              data-check="left-observation"
              :choices="leftChoices"
              placeholder="请选择季节志"
            />
            <small v-if="leftObservation?.has_corrections" class="selector-note">
              <GitBranch :size="12" />
              已采用 {{ leftObservation.current_correction_seq }} 次勘误，比较将使用修正后事实
            </small>
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
            <small v-if="rightObservation?.has_corrections" class="selector-note">
              <GitBranch :size="12" />
              已采用 {{ rightObservation.current_correction_seq }} 次勘误，比较将使用修正后事实
            </small>
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

        <div
          v-if="supersedePrompt"
          class="supersede-notice"
          data-check="supersede-notice"
        >
          <History :size="17" />
          <div>
            <strong>发现同对季节志的历史图谱《{{ supersedePrompt.title }}》</strong>
            <p>
              它生成于 {{ formatTimestamp(supersedePrompt.created_at) }}，冻结的是勘误前事实，系统不会改写它。
              确认后将生成接续的新版图谱，旧版标记为历史事实。
            </p>
          </div>
        </div>

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
          {{ form.supersedesId ? "生成接续新版图谱" : "生成对比图谱" }}
        </button>
      </form>

      <div class="comparison-index">
        <div v-if="currentComparisons.length" class="comparison-index__group">
          <h4>当前有效图谱（{{ currentComparisons.length }}）</h4>
          <button
            v-for="comparison in currentComparisons"
            :key="comparison.id"
            type="button"
            class="comparison-index__item is-current"
            :class="{
              'is-active': workspace.state.selectedComparisonId === comparison.id,
            }"
            data-check="comparison-index-current"
            @click="workspace.selectComparison(comparison.id)"
          >
            <strong>{{ comparison.title }}</strong>
            <small>{{ comparison.season }} · {{ formatTimestamp(comparison.created_at) }}</small>
          </button>
        </div>
        <div v-if="historicalComparisons.length" class="comparison-index__group">
          <h4>
            <History :size="14" />
            历史图谱（{{ historicalComparisons.length }} · 已被勘误超越）
          </h4>
          <button
            v-for="comparison in historicalComparisons"
            :key="comparison.id"
            type="button"
            class="comparison-index__item is-historical"
            :class="{
              'is-active': workspace.state.selectedComparisonId === comparison.id,
            }"
            data-check="comparison-index-historical"
            @click="workspace.selectComparison(comparison.id)"
          >
            <strong>{{ comparison.title }}</strong>
            <small>{{ comparison.season }} · {{ formatTimestamp(comparison.created_at) }}</small>
          </button>
        </div>
      </div>
    </div>

    <div v-if="selectedComparison" class="comparison-result" data-check="comparison-result">
      <header class="comparison-result__header">
        <div>
          <span class="eyebrow">
            {{
              selectedComparison.basis_status === "current"
                ? "当前有效图谱"
                : "历史图谱（事实已被勘误超越）"
            }}
          </span>
          <h2>{{ selectedComparison.title }}</h2>
          <p>{{ formatTimestamp(selectedComparison.created_at) }}</p>
        </div>
        <span
          class="comparison-seal"
          :class="{
            'is-superseded': selectedComparison.basis_status === 'superseded',
          }"
        >
          <component
            :is="
              selectedComparison.basis_status === 'current'
                ? CalendarCheck2
                : History
            "
            :size="18"
          />
          {{ selectedComparison.season }}
        </span>
      </header>

      <div
        v-if="selectedComparison.basis_status === 'superseded'"
        class="basis-banner basis-banner--historical"
        data-check="comparison-superseded-banner"
      >
        <History :size="17" />
        <div>
          <strong>以下偏移是生成时冻结的历史结论，系统未因后续勘误改写它。</strong>
          <p>
            至少一侧季节志在图谱生成后采用了新勘误；当前事实请对照下方季节轨道，或生成接续新版图谱。
          </p>
        </div>
        <button
          type="button"
          class="button button--primary button--compact"
          data-check="renew-comparison"
          @click="renewFrom(selectedComparison)"
        >
          <RefreshCcw :size="14" />
          用当前事实生成新版
        </button>
      </div>

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
          <span>左侧日期（冻结）</span>
          <span>右侧日期（冻结）</span>
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

      <p class="track-caption">
        <GitBranch :size="14" />
        下方为两侧季节志的当前事实轨道（含已采纳勘误），与上方冻结日期对照即可分辨历史与现状。
      </p>
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
</template>
