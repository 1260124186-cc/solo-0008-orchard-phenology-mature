<script setup lang="ts">
import { Check, Circle, HelpCircle, MinusCircle, SearchX } from "@lucide/vue";
import { STAGES, stageLabel, stageResolution } from "../domain/stages";
import type { ObservationSummary, StageResolutionState } from "../domain/types";

const props = withDefaults(
  defineProps<{
    observation?: ObservationSummary | null;
    compact?: boolean;
  }>(),
  {
    observation: null,
    compact: false,
  },
);

function stateFor(stage: string): StageResolutionState {
  if (!props.observation) return "unrecorded";
  return stageResolution(props.observation, stage);
}

const stateCopy: Record<StageResolutionState, string> = {
  observed: "已观察",
  not_applicable: "当年不适用",
  unobserved: "未观察到",
  pending_verification: "仍在核实",
  basis_incomplete: "不适用依据不足",
  unrecorded: "未记录",
};
</script>

<template>
  <div class="stage-track" :class="{ 'is-compact': compact }">
    <div
      v-for="stage in STAGES"
      :key="stage.key"
      class="stage-track__item"
      :class="`is-${stateFor(stage.key)}`"
      :data-stage="stage.key"
      :data-stage-state="stateFor(stage.key)"
    >
      <div class="stage-track__node">
        <Check v-if="stateFor(stage.key) === 'observed'" :size="13" :stroke-width="3" />
        <MinusCircle v-else-if="stateFor(stage.key) === 'not_applicable'" :size="11" />
        <SearchX
          v-else-if="['unobserved', 'basis_incomplete'].includes(stateFor(stage.key))"
          :size="11"
        />
        <HelpCircle v-else-if="stateFor(stage.key) === 'pending_verification'" :size="11" />
        <Circle v-else :size="8" fill="currentColor" />
      </div>
      <div class="stage-track__copy">
        <strong>{{ stageLabel(stage.key) }}</strong>
        <small>
          <template v-if="stateFor(stage.key) === 'observed'">
            {{ observation?.entry_map[stage.key]?.observed_on }} ·
            置信 {{ observation?.entry_map[stage.key]?.confidence }}
          </template>
          <template v-else>{{ stateCopy[stateFor(stage.key)] }}</template>
          <template v-if="stage.required_for_completion"> · 完成所需</template>
        </small>
      </div>
    </div>
  </div>
</template>
