<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import {
  BookLock,
  CircleCheckBig,
  CircleSlash,
  GitBranch,
  History,
  PencilLine,
  Undo2,
  XCircle,
} from "@lucide/vue";
import ChoiceField from "./ChoiceField.vue";
import { formatTimestamp } from "../domain/rules";
import { stageLabel } from "../domain/stages";
import type {
  CorrectionSummary,
  ObservationSummary,
  StageEntry,
} from "../domain/types";
import { useWorkspace } from "../app/workspace";

const props = defineProps<{
  observation: ObservationSummary;
}>();

const workspace = useWorkspace();
const proposing = ref(false);
const rejectTarget = ref<CorrectionSummary | null>(null);
const form = reactive({
  stage: "",
  observedOn: "",
  confidence: "4",
  note: "",
  reason: "",
});
const formErrors = ref<string[]>([]);

const frozenIndex = computed(() => {
  const map = new Map<string, StageEntry>();
  for (const entry of props.observation.frozen_entries ?? []) {
    map.set(entry.stage, entry);
  }
  return map;
});

const stageChoices = computed(() =>
  Array.from(frozenIndex.value.values()).map((entry) => ({
    value: entry.stage,
    label: `${stageLabel(entry.stage)}（原 ${entry.observed_on}）`,
  })),
);

const confidenceChoices = [
  { value: "1", label: "1 · 存疑" },
  { value: "2", label: "2" },
  { value: "3", label: "3" },
  { value: "4", label: "4" },
  { value: "5", label: "5 · 确认" },
];

const ledger = computed(() =>
  workspace
    .correctionsForObservation(props.observation.id)
    .slice()
    .sort((left, right) => left.proposed_at.localeCompare(right.proposed_at)),
);

const adoptedChain = computed(() =>
  ledger.value.filter((item) => item.status === "adopted"),
);
const pendingCorrections = computed(() =>
  ledger.value.filter((item) => item.status === "proposed"),
);
const resolvedCorrections = computed(() =>
  ledger.value.filter((item) =>
    item.status === "rejected" || item.status === "withdrawn",
  ),
);

function openProposal() {
  proposing.value = true;
  form.stage = stageChoices.value[0]?.value ?? "";
  const entry = frozenIndex.value.get(form.stage);
  form.observedOn = entry?.observed_on ?? "";
  form.confidence = String(entry?.confidence ?? 4);
  form.note = entry?.note ?? "";
  form.reason = "";
  formErrors.value = [];
}

function syncOriginal() {
  const entry = frozenIndex.value.get(form.stage);
  if (entry) {
    form.observedOn = entry.observed_on;
    form.confidence = String(entry.confidence);
    form.note = entry.note;
  }
}

async function submitProposal() {
  formErrors.value = [];
  if (!form.stage) formErrors.value.push("请选择需要修正的阶段");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(form.observedOn)) {
    formErrors.value.push("请填写合法的修正日期");
  }
  if (form.reason.trim().length < 4) {
    formErrors.value.push("请用至少四个字说明修正依据");
  }
  if (formErrors.value.length) return;
  const result = await workspace.runAction(
    () =>
      workspace.createCorrection({
        observation_id: props.observation.id,
        reason: form.reason,
        changes: [
          {
            stage: form.stage,
            observed_on: form.observedOn,
            confidence: Number(form.confidence),
            note: form.note,
          },
        ],
      }),
    "勘误已提交，等待受控采纳",
  );
  if (result) {
    proposing.value = false;
  }
}

async function adopt(correction: CorrectionSummary) {
  await workspace.runAction(
    () => workspace.decideCorrection("adopt", correction),
    "勘误已采纳：当前事实更新，原始结论保留为历史",
  );
}

async function withdraw(correction: CorrectionSummary) {
  await workspace.runAction(
    () => workspace.decideCorrection("withdraw", correction),
    "勘误已撤回",
  );
}

const rejectNote = ref("");
async function confirmReject() {
  const correction = rejectTarget.value;
  if (!correction) return;
  if (rejectNote.value.trim().length < 2) {
    workspace.pushNotice("error", "请填写至少两个字的拒绝理由");
    return;
  }
  const note = rejectNote.value;
  rejectTarget.value = null;
  rejectNote.value = "";
  await workspace.runAction(
    () => workspace.decideCorrection("reject", correction, note),
    "勘误已拒绝，原始事实保持不变",
  );
}

function statusLabel(status: CorrectionSummary["status"]): string {
  return {
    proposed: "待决",
    adopted: "已采纳",
    rejected: "已拒绝",
    withdrawn: "已撤回",
  }[status];
}

function describeChange(change: CorrectionSummary["changes"][number]): string {
  const parts: string[] = [];
  if (change.observed_on) parts.push(`日期 → ${change.observed_on}`);
  if (change.confidence !== undefined) {
    parts.push(`置信 → ${change.confidence}/5`);
  }
  if (change.note !== undefined) {
    parts.push(`说明 → ${change.note || "（清空）"}`);
  }
  return parts.join(" · ");
}
</script>

<template>
  <section class="correction-ledger" data-check="correction-ledger">
    <header class="correction-ledger__head">
      <div>
        <span class="eyebrow">受控勘误</span>
        <h4>
          <History :size="16" />
          当前事实与历史事实
        </h4>
      </div>
      <button
        v-if="!proposing"
        type="button"
        class="button button--ghost button--compact"
        data-check="open-correction"
        @click="openProposal"
      >
        <PencilLine :size="15" />
        提出勘误
      </button>
    </header>

    <p class="correction-ledger__explain">
      <BookLock :size="14" />
      完成时的结论已封存；勘误采纳后只改变“当前事实”，原始日期、既有图谱和简报均不会被改写。
    </p>

    <form
      v-if="proposing"
      class="correction-form"
      data-check="correction-form"
      @submit.prevent="submitProposal"
    >
      <label>
        <span>修正阶段</span>
        <ChoiceField
          v-model="form.stage"
          data-check="correction-stage"
          :choices="stageChoices"
          @update:model-value="syncOriginal"
        />
      </label>
      <div class="form-grid form-grid--two">
        <label>
          <span>修正后的观察日期</span>
          <input
            v-model="form.observedOn"
            data-check="correction-date"
            type="date"
          />
        </label>
        <label>
          <span>置信度</span>
          <ChoiceField
            v-model="form.confidence"
            data-check="correction-confidence"
            :choices="confidenceChoices"
          />
        </label>
      </div>
      <label>
        <span>修正说明（可选）</span>
        <input
          v-model="form.note"
          data-check="correction-note"
          maxlength="300"
          placeholder="现场补充说明"
        />
      </label>
      <label>
        <span>修正依据</span>
        <textarea
          v-model="form.reason"
          data-check="correction-reason"
          rows="2"
          maxlength="300"
          placeholder="例如：核对现场原始台账后确认日期登记偏早"
        />
      </label>
      <div v-if="formErrors.length" class="form-errors">
        <span v-for="error in formErrors" :key="error">{{ error }}</span>
      </div>
      <div class="correction-form__actions">
        <button
          type="button"
          class="button button--ghost"
          @click="proposing = false"
        >
          取消
        </button>
        <button
          type="submit"
          class="button button--primary"
          data-check="submit-correction"
        >
          提交勘误
        </button>
      </div>
    </form>

    <div
      v-if="observation.has_corrections"
      class="correction-chain"
      data-check="correction-chain"
    >
      <div class="correction-chain__title">
        <GitBranch :size="14" />
        已采纳勘误链（第 {{ adoptedChain.length }} 代当前事实）
      </div>
      <ol>
        <li
          v-for="(correction, index) in adoptedChain"
          :key="correction.id"
          class="correction-item is-adopted"
          data-check="correction-adopted-item"
        >
          <header>
            <CircleCheckBig :size="15" />
            <strong>第 {{ index + 1 }} 次修正</strong>
            <span>{{ formatTimestamp(correction.adopted_at) }}</span>
          </header>
          <p class="correction-item__reason">{{ correction.reason }}</p>
          <p
            v-for="change in correction.changes"
            :key="change.stage"
            class="correction-item__change"
          >
            {{ stageLabel(change.stage) }}：{{ describeChange(change) }}
          </p>
          <small>{{ correction.proposed_by }} 提出 · {{ correction.decided_by }} 采纳</small>
        </li>
      </ol>
    </div>

    <div v-if="pendingCorrections.length" class="correction-pending">
      <div class="correction-chain__title">待决勘误（尚未影响任何事实）</div>
      <article
        v-for="correction in pendingCorrections"
        :key="correction.id"
        class="correction-item is-pending"
        data-check="correction-pending-item"
      >
        <header>
          <CircleSlash :size="15" />
          <strong>{{ stageLabel(correction.changes[0]?.stage) }} 勘误</strong>
          <span>{{ statusLabel(correction.status) }}</span>
        </header>
        <p class="correction-item__reason">{{ correction.reason }}</p>
        <p
            v-for="change in correction.changes"
            :key="change.stage"
            class="correction-item__change"
          >
            {{ stageLabel(change.stage) }}：{{ describeChange(change) }}
          </p>
        <small>{{ correction.proposed_by }} 提交于 {{ formatTimestamp(correction.proposed_at) }}</small>
        <div class="correction-item__actions">
          <button
            type="button"
            class="button button--primary button--compact"
            data-check="adopt-correction"
            @click="adopt(correction)"
          >
            <CircleCheckBig :size="14" />
            采纳为当前事实
          </button>
          <button
            type="button"
            class="button button--ghost button--compact"
            data-check="reject-correction"
            @click="rejectTarget = correction; rejectNote = ''"
          >
            <XCircle :size="14" />
            拒绝
          </button>
          <button
            type="button"
            class="button button--ghost button--compact"
            data-check="withdraw-correction"
            @click="withdraw(correction)"
          >
            <Undo2 :size="14" />
            撤回
          </button>
        </div>
      </article>
    </div>

    <div
      v-if="resolvedCorrections.length"
      class="correction-resolved"
      data-check="correction-resolved"
    >
      <details>
        <summary>已拒绝或撤回的勘误（{{ resolvedCorrections.length }}）</summary>
        <article
          v-for="correction in resolvedCorrections"
          :key="correction.id"
          class="correction-item is-resolved"
        >
          <header>
            <XCircle :size="14" />
            <strong>{{ statusLabel(correction.status) }}</strong>
            <span>{{ formatTimestamp(correction.decided_at) }}</span>
          </header>
          <p class="correction-item__reason">{{ correction.reason }}</p>
          <p v-if="correction.decision_note" class="correction-item__change">
            理由：{{ correction.decision_note }}
          </p>
        </article>
      </details>
    </div>

    <div
      v-if="rejectTarget"
      class="reject-dialog"
      data-check="reject-dialog"
      role="dialog"
    >
      <label>
        <span>拒绝理由</span>
        <input
          v-model="rejectNote"
          data-check="reject-note"
          maxlength="300"
          placeholder="说明不采纳的依据"
        />
      </label>
      <div class="correction-form__actions">
        <button
          type="button"
          class="button button--ghost"
          @click="rejectTarget = null"
        >
          取消
        </button>
        <button
          type="button"
          class="button button--primary"
          data-check="confirm-reject"
          @click="confirmReject"
        >
          确认拒绝
        </button>
      </div>
    </div>
  </section>
</template>
