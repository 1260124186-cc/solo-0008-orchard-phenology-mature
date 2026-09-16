<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { CircleSlash, PencilLine } from "@lucide/vue";
import ChoiceField from "./ChoiceField.vue";
import { validateAbsenceDraft } from "../domain/rules";
import { ABSENCE_REASONS, STAGES, absenceReasonLabel, stageLabel } from "../domain/stages";
import type { AbsenceMarker, AbsenceReasonKey, ObservationSummary } from "../domain/types";

const props = defineProps<{
  observation: ObservationSummary;
}>();

const emit = defineEmits<{
  save: [payload: { stage: string; reason: AbsenceReasonKey; basis: string }];
  remove: [stage: string];
}>();

const blank = () => ({
  stage: "",
  reason: "unobserved" as AbsenceReasonKey,
  basis: "",
});
const form = reactive(blank());
const editingStage = ref<string | null>(null);
const errors = ref<string[]>([]);

const observedStages = computed(
  () => new Set(props.observation.entries.map((entry) => entry.stage)),
);
const markedStages = computed(
  () => new Set(props.observation.absence_markers.map((marker) => marker.stage)),
);

const stageChoices = computed(() =>
  STAGES.map((stage) => ({
    value: stage.key,
    label: `${stage.label}${stage.required_for_completion ? " · 完成所需" : ""}${
      markedStages.value.has(stage.key) ? " · 已有缺失说明" : ""
    }`,
    disabled:
      observedStages.value.has(stage.key) ||
      (markedStages.value.has(stage.key) && stage.key !== editingStage.value),
  })),
);

const reasonChoices = ABSENCE_REASONS.map((reason) => ({
  value: reason.key,
  label: reason.label,
}));

const selectedReason = computed(() =>
  ABSENCE_REASONS.find((reason) => reason.key === form.reason),
);

function markerFor(stage: string): AbsenceMarker | undefined {
  return props.observation.absence_markers.find((item) => item.stage === stage);
}

function startEdit(marker: AbsenceMarker) {
  editingStage.value = marker.stage;
  form.stage = marker.stage;
  form.reason = marker.reason;
  form.basis = marker.basis;
  errors.value = [];
}

function resetForm() {
  Object.assign(form, blank());
  editingStage.value = null;
  errors.value = [];
}

function submit() {
  const issues = validateAbsenceDraft(
    form,
    // 编辑同一阶段时，该阶段的旧标记不应被视为“重复登记”。
    props.observation.absence_markers.filter(
      (marker) => marker.stage !== editingStage.value,
    ),
    props.observation.entries,
  );
  errors.value = issues.map((issue) => issue.message);
  if (issues.length) return;
  emit("save", {
    stage: form.stage,
    reason: form.reason,
    basis: form.basis.trim(),
  });
  resetForm();
}

watch(
  () => props.observation.revision,
  () => {
    // 保存成功后服务端返回新版本，退出编辑态。
    if (editingStage.value && !markedStages.value.has(editingStage.value)) {
      resetForm();
    }
  },
);
</script>

<template>
  <section class="absence-panel" data-check="absence-panel">
    <header class="absence-panel__heading">
      <CircleSlash :size="17" />
      <div>
        <h4>缺失阶段说明</h4>
        <p>区分「未观察到」「当年不适用」「仍在核实」，并写明依据。只有附充分依据的“当年不适用”能作为必需阶段的完成依据。</p>
      </div>
    </header>

    <ul v-if="observation.absence_markers.length" class="absence-list" data-check="absence-list">
      <li
        v-for="marker in observation.absence_markers"
        :key="marker.id"
        class="absence-list__item"
        :class="`is-${marker.reason}`"
        data-check="absence-item"
        :data-stage="marker.stage"
        :data-reason="marker.reason"
      >
        <div>
          <strong>{{ stageLabel(marker.stage) }}</strong>
          <span class="absence-list__reason">{{ absenceReasonLabel(marker.reason) }}</span>
        </div>
        <p>{{ marker.basis }}</p>
        <div class="absence-list__actions">
          <button
            type="button"
            class="text-button"
            data-check="absence-edit"
            @click="startEdit(marker)"
          >
            <PencilLine :size="13" />
            修改
          </button>
          <button
            type="button"
            class="text-button text-button--danger"
            data-check="absence-remove"
            @click="emit('remove', marker.stage)"
          >
            移除说明
          </button>
        </div>
      </li>
    </ul>

    <form class="absence-form" @submit.prevent="submit">
      <div class="form-grid form-grid--three">
        <label>
          <span>缺失阶段</span>
          <ChoiceField
            v-model="form.stage"
            data-check="absence-stage"
            :choices="stageChoices"
            placeholder="请选择阶段"
            :disabled="editingStage !== null"
          />
        </label>
        <label>
          <span>缺失原因</span>
          <ChoiceField
            v-model="form.reason"
            data-check="absence-reason"
            :choices="reasonChoices"
          />
        </label>
        <label class="absence-form__basis">
          <span>
            判断依据
            <small v-if="selectedReason">
              （{{ selectedReason.resolves_completion ? "至少 10 个字" : "至少 4 个字" }}）
            </small>
          </span>
          <input
            v-model="form.basis"
            data-check="absence-basis"
            maxlength="300"
            placeholder="例如：4 月两次巡查均未见开花，结合品种特性判断本年不适用"
          />
        </label>
      </div>
      <p v-if="selectedReason" class="absence-form__hint" data-check="absence-hint">
        {{ selectedReason.description }}
      </p>
      <div v-if="errors.length" class="form-errors">
        <span v-for="error in errors" :key="error">{{ error }}</span>
      </div>
      <div class="absence-form__actions">
        <button
          v-if="editingStage"
          type="button"
          class="button button--ghost"
          @click="resetForm"
        >
          取消修改
        </button>
        <button type="submit" class="button button--secondary" data-check="absence-save">
          <CircleSlash :size="16" />
          {{ editingStage ? "保存修改" : "登记缺失说明" }}
        </button>
      </div>
    </form>
  </section>
</template>
