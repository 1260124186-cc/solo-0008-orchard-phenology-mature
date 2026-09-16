<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { Plus } from "@lucide/vue";
import { validateStageDraft } from "../domain/rules";
import { DATE_PRECISION_CHOICES } from "../domain/datePrecision";
import { STAGES } from "../domain/stages";
import type {
  DatePrecision,
  ObservationSummary,
  StageEntry,
} from "../domain/types";
import ChoiceField from "./ChoiceField.vue";

const props = defineProps<{
  observation: Pick<ObservationSummary, "entries" | "season">;
}>();

const emit = defineEmits<{
  add: [payload: Record<string, unknown>];
}>();

const today = new Date().toISOString().slice(0, 10);
const form = reactive({
  stage: "",
  precision: "day" as DatePrecision,
  observed_on: today,
  observed_end_on: "",
  confidence: 4,
  note: "",
});
const errors = ref<string[]>([]);
const stageChoices = computed(() =>
  STAGES.map((stage) => ({
    value: stage.key,
    label: `${stage.label}${stage.required_for_completion ? " · 完成所需" : ""}`,
    disabled: props.observation.entries.some(
      (entry) => entry.stage === stage.key,
    ),
  })),
);
const precisionChoices = DATE_PRECISION_CHOICES.map((choice) => ({
  value: choice.value,
  label: choice.label,
}));
const activePrecisionHint = computed(
  () =>
    DATE_PRECISION_CHOICES.find((choice) => choice.value === form.precision)
      ?.hint ?? "",
);
const needsEndDate = computed(() => form.precision === "range");

function submit() {
  const draft: Partial<StageEntry> = {
    stage: form.stage,
    precision: form.precision,
    observed_on: form.observed_on,
    confidence: form.confidence,
    note: form.note,
  };
  if (needsEndDate.value) {
    draft.observed_end_on = form.observed_end_on;
  }
  const issues = validateStageDraft(
    draft,
    props.observation.entries,
    props.observation.season,
  );
  errors.value = issues.map((issue) => issue.message);
  if (issues.length > 0) return;
  const payload: Record<string, unknown> = {
    stage: form.stage,
    precision: form.precision,
    observed_on: form.observed_on,
    confidence: form.confidence,
    note: form.note,
  };
  if (needsEndDate.value) {
    payload.observed_end_on = form.observed_end_on;
  }
  emit("add", payload);
  form.stage = "";
  form.precision = "day";
  form.observed_end_on = "";
  form.note = "";
}
</script>

<template>
  <form class="stage-entry-form" @submit.prevent="submit">
    <div class="form-grid form-grid--four">
      <label>
        <span>物候阶段</span>
        <ChoiceField
          v-model="form.stage"
          data-check="stage-key"
          :choices="stageChoices"
          placeholder="请选择阶段"
        />
      </label>
      <label>
        <span>日期精度</span>
        <ChoiceField
          v-model="form.precision"
          data-check="stage-precision"
          :choices="precisionChoices"
        />
      </label>
      <label>
        <span>{{ needsEndDate ? "开始日期" : "观察日期" }}</span>
        <input v-model="form.observed_on" data-check="stage-date" type="date" />
      </label>
      <label v-if="needsEndDate">
        <span>结束日期</span>
        <input
          v-model="form.observed_end_on"
          data-check="stage-end-date"
          type="date"
        />
      </label>
      <label>
        <span>置信度</span>
        <ChoiceField
          v-model="form.confidence"
          data-check="stage-confidence"
          :choices="[1, 2, 3, 4, 5].map((value) => ({
            value,
            label: `${value} / 5`,
          }))"
        />
      </label>
      <label class="stage-entry-form__note">
        <span>现场说明</span>
        <input v-model="form.note" maxlength="300" placeholder="可选" />
      </label>
    </div>
    <p class="stage-entry-form__hint">{{ activePrecisionHint }}</p>
    <div v-if="errors.length" class="form-errors">
      <span v-for="error in errors" :key="error">{{ error }}</span>
    </div>
    <button type="submit" class="button button--secondary" data-check="add-stage">
      <Plus :size="16" />
      补录阶段
    </button>
  </form>
</template>
