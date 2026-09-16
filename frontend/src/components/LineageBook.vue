<script setup lang="ts">
import { ArrowRight, History } from "@lucide/vue";
import { stageLabel } from "../domain/stages";
import type { EntryLineage, ObservationSummary } from "../domain/types";

defineProps<{
  observation: ObservationSummary;
}>();

function statusText(line: EntryLineage): string {
  return {
    unchanged: "沿用原结论",
    revised: "日期等已修正",
    replaced_out: "误录阶段 · 已从当前事实移除",
    replaced_transit: "中间阶段 · 已替换进入又被后续勘误替换",
    replaced_in: "替换进入的最终正确阶段",
  }[line.status];
}
</script>

<template>
  <section class="lineage-block">
    <div
      v-if="observation.replacement_chain && observation.replacement_chain.length"
      class="replacement-flow"
      data-check="replacement-flow"
    >
      <h5><History :size="14" /> 阶段替换链</h5>
      <ol>
        <li
          v-for="hop in observation.replacement_chain"
          :key="`${hop.seq}-${hop.from_stage}-${hop.to_stage}`"
          class="replacement-flow__hop"
          :class="{ 'is-final': hop.to_still_current }"
          :data-hop-seq="hop.seq"
        >
          <span class="replacement-flow__seq">第 {{ hop.seq }} 跳</span>
          <span class="replacement-flow__from">{{ stageLabel(hop.from_stage) }}</span>
          <ArrowRight :size="14" />
          <span class="replacement-flow__to">{{ stageLabel(hop.to_stage) }}</span>
          <span class="replacement-flow__date">{{ hop.observed_on }}</span>
          <em v-if="hop.to_still_current" class="replacement-flow__final">当前轨道保留</em>
          <em v-else class="replacement-flow__transit">已继续替换</em>
        </li>
      </ol>
    </div>

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
              <small>完成时无此阶段（替换进入）</small>
            </template>
          </span>
          <span class="lineage-book__current">
            <template v-if="line.status === 'replaced_out'">
              <em class="lineage-book__arrow">→</em>
              <b class="lineage-book__moved">
                已替换为
                {{ line.replacement_stage ? stageLabel(line.replacement_stage) : "" }}
              </b>
            </template>
            <template v-else-if="line.status === 'replaced_transit'">
              <em class="lineage-book__arrow">→</em>
              <b class="lineage-book__moved">
                来自
                {{ line.replaced_from_stage ? stageLabel(line.replaced_from_stage) : "" }}
                ，又替换为
                {{ line.replacement_stage ? stageLabel(line.replacement_stage) : "" }}
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
              <small>{{ statusText(line) }}</small>
            </template>
          </span>
        </div>
        <div
          v-if="line.status !== 'unchanged'"
          class="lineage-book__note"
        >
          {{ statusText(line) }}
        </div>
      </template>
    </div>
  </section>
</template>
