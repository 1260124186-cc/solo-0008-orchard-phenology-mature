<script setup lang="ts">
import { History } from "@lucide/vue";
import { stageLabel } from "../domain/stages";
import type { EntryLineage, ObservationSummary } from "../domain/types";

defineProps<{
  observation: ObservationSummary;
}>();

function statusLabel(line: EntryLineage): string {
  return {
    unchanged: "沿用原结论",
    revised: "日期等已修正",
    replaced_out: "误录阶段 · 已从当前事实移除",
    replaced_in: "替换进入的正确阶段",
  }[line.status];
}
</script>

<template>
  <div class="lineage-book" data-check="lineage-book">
    <div class="lineage-book__head">
      <span>
        <History :size="14" />
        物候阶段
      </span>
      <span>完成时冻结事实</span>
      <span>当前采用事实</span>
    </div>
    <template
      v-for="line in observation.entry_lineage ?? []"
      :key="`${line.stage}-${line.status}`"
    >
      <div
        class="lineage-book__row"
        :class="'is-' + line.status.replace(/_/g, '-')"
        :data-stage="line.stage"
        :data-lineage-status="line.status"
        data-check="lineage-row"
      >
        <strong>{{ stageLabel(line.stage) }}</strong>
        <span class="lineage-book__frozen">
          <template v-if="line.frozen">
            {{ line.frozen.observed_on }}
            <small>置信 {{ line.frozen.confidence }}/5</small>
          </template>
          <template v-else>
            <small>完成时无此阶段</small>
          </template>
        </span>
        <span class="lineage-book__current">
          <template v-if="line.status === 'replaced_out'">
            <em class="lineage-book__arrow">→</em>
            <b class="lineage-book__moved">
              已替换为 {{ line.replacement_stage ? stageLabel(line.replacement_stage) : "" }}
            </b>
          </template>
          <template v-else-if="line.current">
            <em
              v-if="line.status !== 'unchanged'"
              class="lineage-book__arrow"
            >→</em>
            <b>{{ line.current.observed_on }}</b>
            <small>置信 {{ line.current.confidence }}/5</small>
          </template>
          <template v-else>
            <small>{{ statusLabel(line) }}</small>
          </template>
        </span>
      </div>
      <div
        v-if="
          line.status === 'replaced_out' ||
          line.status === 'replaced_in' ||
          line.status === 'revised'
        "
        class="lineage-book__note"
      >
        {{ statusLabel(line) }}
      </div>
    </template>
  </div>
</template>
