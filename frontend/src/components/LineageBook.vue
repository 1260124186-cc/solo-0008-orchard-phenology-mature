<script setup lang="ts">
import { History } from "@lucide/vue";
import { stageLabel } from "../domain/stages";
import type { ObservationSummary } from "../domain/types";

defineProps<{
  observation: ObservationSummary;
}>();
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
    <div
      v-for="line in observation.entry_lineage ?? []"
      :key="line.stage"
      class="lineage-book__row"
      :class="{ 'is-revised': line.revised }"
      :data-stage="line.stage"
      data-check="lineage-row"
    >
      <strong>{{ stageLabel(line.stage) }}</strong>
      <span class="lineage-book__frozen">
        {{ line.frozen.observed_on }}
        <small>置信 {{ line.frozen.confidence }}/5</small>
      </span>
      <span class="lineage-book__current">
        <template v-if="line.revised">
          <em class="lineage-book__arrow">→</em>
          <b>{{ line.current.observed_on }}</b>
          <small>置信 {{ line.current.confidence }}/5</small>
        </template>
        <template v-else>
          <small>沿用原结论</small>
        </template>
      </span>
    </div>
  </div>
</template>
