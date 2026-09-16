<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  History,
  RotateCcw,
  ShieldAlert,
} from "@lucide/vue";
import { formatTimestamp, treeStatusLabel } from "../domain/rules";
import type { TreeRecord, TreeStatus, TreeStatusEvent } from "../domain/types";
import { useWorkspace } from "../app/workspace";

const props = defineProps<{
  tree: TreeRecord;
}>();

const workspace = useWorkspace();

const CLOSING_OPTIONS: { value: TreeStatus; label: string; hint: string }[] = [
  { value: "retired", label: "标记为已退休", hint: "植株存活但退出常规观察，如更新复壮、移栽他处" },
  { value: "lost", label: "标记为已遗失", hint: "植株死亡、砍伐或现场无法再找到" },
];

const form = ref({
  target: "retired" as TreeStatus,
  reason: "",
  evidence: "",
});
const errors = ref<string[]>([]);
const history = ref<readonly TreeStatusEvent[]>(
  props.tree.status_history ?? [],
);
const historyLoaded = ref<boolean>(Boolean(props.tree.status_history?.length));
const historyLoading = ref(false);
const submitting = ref(false);

const isActive = computed(() => props.tree.status === "active");
const timeline = computed(() =>
  [...history.value].sort((left, right) => right.sequence - left.sequence),
);

function resetForm() {
  form.value = {
    target: isActive.value ? "retired" : "active",
    reason: "",
    evidence: "",
  };
  errors.value = [];
}

resetForm();

watch(
  () => props.tree.id,
  () => {
    history.value = props.tree.status_history ?? [];
    historyLoaded.value = Boolean(props.tree.status_history?.length);
    resetForm();
  },
);

async function loadHistory() {
  if (historyLoading.value) return;
  historyLoading.value = true;
  try {
    const result = await workspace.loadTreeStatusHistory(props.tree.id);
    history.value = result.items;
    historyLoaded.value = true;
  } catch (error) {
    workspace.pushNotice(
      "error",
      error instanceof Error ? error.message : "状态沿革读取失败",
    );
  } finally {
    historyLoading.value = false;
  }
}

function validate(): boolean {
  const issues: string[] = [];
  const reason = form.value.reason.trim();
  const evidence = form.value.evidence.trim();
  if (reason.length < 2) issues.push("请填写状态改变的原因（至少两个字）");
  if (reason.length > 300) issues.push("原因最多 300 个字符");
  if (evidence.length < 2) issues.push("请填写判定依据，如现场核查、照片或记录来源");
  if (evidence.length > 300) issues.push("依据最多 300 个字符");
  errors.value = issues;
  return issues.length === 0;
}

async function submit() {
  if (submitting.value) return;
  if (!validate()) return;
  submitting.value = true;
  const message = isActive.value
    ? form.value.target === "lost"
      ? "植株已标记为遗失，历史观察全部保留"
      : "植株已退休，历史观察全部保留"
    : "植株已恢复为在册，沿用同一档案与历史观察";
  const result = await workspace.runAction(
    () =>
      workspace.changeTreeStatus(props.tree, {
        status: form.value.target,
        reason: form.value.reason.trim(),
        evidence: form.value.evidence.trim(),
      }),
    message,
  );
  submitting.value = false;
  if (result !== null) {
    historyLoaded.value = false;
    await loadHistory();
    resetForm();
  }
}
</script>

<template>
  <aside class="tree-status-panel" data-check="tree-status-panel">
    <header class="tree-status-panel__header">
      <div>
        <span class="eyebrow">植株状态档案</span>
        <strong data-check="tree-status-name">
          {{ tree.code }} · {{ tree.cultivar }}
        </strong>
        <small>{{ tree.rootstock }} / {{ tree.planting_year }} 年定植</small>
      </div>
      <span
        class="status-stamp"
        :class="`is-tree-${tree.status}`"
        data-check="tree-status-stamp"
      >
        {{ treeStatusLabel(tree.status) }}
      </span>
    </header>

    <p class="tree-status-panel__note">{{ tree.note || "没有补充说明。" }}</p>

    <form class="tree-status-form" @submit.prevent="submit">
      <p class="tree-status-form__lead">
        <ShieldAlert v-if="isActive" :size="16" />
        <RotateCcw v-else :size="16" />
        <span v-if="isActive">
          结束在册属于长期状态变化，请记录原因与判定依据。
        </span>
        <span v-else>
          误操作或复查后可恢复在册；恢复不会新建植株，历史观察继续指向本株。
        </span>
      </p>

      <label v-if="isActive" class="tree-status-targets">
        <span>改变为</span>
        <span class="tree-status-targets__options">
          <button
            v-for="option in CLOSING_OPTIONS"
            :key="option.value"
            type="button"
            class="status-choice"
            :class="{ 'is-selected': form.target === option.value }"
            :data-check="`tree-status-choice-${option.value}`"
            @click="form.target = option.value"
          >
            {{ option.label }}
          </button>
        </span>
        <small class="tree-status-targets__hint">
          {{ CLOSING_OPTIONS.find((option) => option.value === form.target)?.hint }}
        </small>
      </label>

      <label>
        <span>{{ isActive ? "改变原因" : "恢复原因" }}</span>
        <textarea
          v-model="form.reason"
          rows="2"
          maxlength="300"
          data-check="tree-status-reason"
          :placeholder="
            isActive
              ? '例如：连续两年巡查未见萌发，疑似枯死'
              : '例如：复查发现根蘖重新萌发，原枯死判定为误判'
          "
        />
      </label>
      <label>
        <span>依据来源</span>
        <textarea
          v-model="form.evidence"
          rows="2"
          maxlength="300"
          data-check="tree-status-evidence"
          placeholder="例如：2026-05-10 现场照片、管护员签字笔录"
        />
      </label>

      <div v-if="errors.length" class="form-errors">
        <span v-for="error in errors" :key="error">{{ error }}</span>
      </div>

      <button
        type="submit"
        class="button"
        :class="isActive ? 'button--danger' : 'button--primary'"
        data-check="tree-status-submit"
        :disabled="submitting"
      >
        <ShieldAlert v-if="isActive" :size="16" />
        <RotateCcw v-else :size="16" />
        {{ isActive ? "记录状态改变" : "恢复为在册" }}
      </button>
    </form>

    <section class="tree-status-history">
      <header>
        <h4>
          <History :size="15" />
          状态变化沿革
        </h4>
        <button
          type="button"
          class="text-button"
          data-check="tree-status-history-refresh"
          @click="loadHistory"
        >
          {{ historyLoading ? "读取中…" : "刷新痕迹" }}
        </button>
      </header>
      <p class="tree-status-history__continuity">
        所有季节志、对比图谱与简报均通过同一植株编号关联本株，状态变化不会改名或另立对象。
      </p>
      <ol v-if="timeline.length" class="status-timeline" data-check="tree-status-timeline">
        <li
          v-for="event in timeline"
          :key="event.sequence"
          class="status-timeline__item"
          :class="`is-${event.status}`"
          :data-check="`tree-status-event-${event.sequence}`"
        >
          <div class="status-timeline__head">
            <span class="status-timeline__badge">
              {{ event.status_label ?? treeStatusLabel(event.status) }}
            </span>
            <small>{{ formatTimestamp(event.changed_at) }}</small>
          </div>
          <p class="status-timeline__transition">
            <template v-if="event.previous_status_label">
              {{ event.previous_status_label }} → {{ event.status_label ?? treeStatusLabel(event.status) }}
            </template>
            <template v-else>建档入册</template>
          </p>
          <p class="status-timeline__reason">{{ event.reason }}</p>
          <p class="status-timeline__evidence">依据：{{ event.evidence }}</p>
          <small class="status-timeline__actor">
            记录人 {{ event.actor_id }} · 第 {{ event.sequence }} 次状态记录
          </small>
        </li>
      </ol>
      <p v-else class="tree-status-history__empty">
        尚未载入状态沿革，点击“刷新痕迹”查看完整记录。
      </p>
    </section>
  </aside>
</template>
