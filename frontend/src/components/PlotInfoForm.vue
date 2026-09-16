<script setup lang="ts">
import { reactive, ref } from "vue";
import { PencilLine, RotateCcw, Save } from "@lucide/vue";
import { validatePlotDraft } from "../domain/rules";
import type { PlotDetail } from "../domain/types";
import { useWorkspace } from "../app/workspace";

const props = defineProps<{
  plot: PlotDetail;
}>();

const workspace = useWorkspace();
const open = ref(false);
const saving = ref(false);
const errors = ref<string[]>([]);

const form = reactive({
  name: props.plot.name,
  locality: props.plot.locality,
  cultivar_focus: props.plot.cultivar_focus,
  steward: props.plot.steward,
  planting_year: props.plot.planting_year,
  note: props.plot.note,
});

function reset() {
  form.name = props.plot.name;
  form.locality = props.plot.locality;
  form.cultivar_focus = props.plot.cultivar_focus;
  form.steward = props.plot.steward;
  form.planting_year = props.plot.planting_year;
  form.note = props.plot.note;
  errors.value = [];
}

async function submit() {
  const issues = validatePlotDraft({
    ...form,
    code: props.plot.code,
  } as Partial<PlotDetail>);
  errors.value = issues.map((issue) => issue.message);
  if (issues.length > 0) return;
  saving.value = true;
  const result = await workspace.runAction(
    () =>
      workspace.updatePlot(props.plot.id, {
        ...form,
        revision: props.plot.revision,
      }),
    "园区基础信息已更新",
  );
  saving.value = false;
  if (result) {
    open.value = false;
    errors.value = [];
  }
}
</script>

<template>
  <section class="plot-edit" data-check="plot-edit-panel">
    <button
      type="button"
      class="button button--ghost button--small plot-edit__toggle"
      data-check="plot-edit-toggle"
      @click="open = !open"
    >
      <PencilLine :size="14" />
      {{ open ? "收起基础信息编辑" : "编辑基础信息" }}
    </button>

    <form v-if="open" class="archive-form archive-form--nested" @submit.prevent="submit">
      <p class="plot-edit__hint">
        这里只修改名称、地点、品种、责任人、年份和说明；需要改园区编号请使用下方的“编号修正”面板，
        它会一次性级联关联植株，避免园区已换号而植株仍旧号。
      </p>
      <div class="form-grid form-grid--two">
        <label>
          <span>园区名称</span>
          <input v-model="form.name" data-check="plot-edit-name" maxlength="80" />
        </label>
        <label>
          <span>起始种植年份</span>
          <input
            v-model.number="form.planting_year"
            data-check="plot-edit-year"
            type="number"
            min="1800"
          />
        </label>
      </div>
      <label>
        <span>地点描述</span>
        <input v-model="form.locality" data-check="plot-edit-locality" maxlength="160" />
      </label>
      <div class="form-grid form-grid--two">
        <label>
          <span>重点品种</span>
          <input v-model="form.cultivar_focus" data-check="plot-edit-focus" maxlength="80" />
        </label>
        <label>
          <span>责任人或机构</span>
          <input v-model="form.steward" data-check="plot-edit-steward" maxlength="80" />
        </label>
      </div>
      <label>
        <span>档案说明</span>
        <textarea v-model="form.note" rows="2" maxlength="500"></textarea>
      </label>

      <div v-if="errors.length" class="form-errors" data-check="plot-edit-errors">
        <span v-for="error in errors" :key="error">{{ error }}</span>
      </div>

      <div class="form-actions">
        <button
          type="submit"
          class="button button--primary"
          data-check="plot-edit-save"
          :disabled="saving"
        >
          <Save :size="16" />
          保存基础信息
        </button>
        <button type="button" class="button button--ghost" @click="reset">
          <RotateCcw :size="15" />
          还原
        </button>
      </div>
    </form>
  </section>
</template>
