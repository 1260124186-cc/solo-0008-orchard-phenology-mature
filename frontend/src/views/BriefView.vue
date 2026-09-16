<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import {
  BookLock,
  BookOpenText,
  Building2,
  Download,
  FileCheck2,
  GitBranch,
  History,
  Leaf,
  Trees,
} from "@lucide/vue";
import EmptyState from "../components/EmptyState.vue";
import ChoiceField from "../components/ChoiceField.vue";
import { formatTimestamp } from "../domain/rules";
import { stageLabel } from "../domain/stages";
import { api } from "../services/api";
import type { BriefSummary } from "../domain/types";
import { useWorkspace } from "../app/workspace";

const workspace = useWorkspace();
const form = reactive({
  plotId: "",
  title: "",
});
const latest = ref<BriefSummary | null>(null);
const briefLibrary = ref<BriefSummary[]>([]);

const confirmedPlots = computed(() =>
  workspace.state.plots.filter((plot) => plot.status === "confirmed"),
);
const plotChoices = computed(() =>
  confirmedPlots.value.map((plot) => ({
    value: plot.id,
    label: `${plot.code} · ${plot.name}`,
  })),
);

async function refreshLibrary() {
  const response = (await api.listBriefs(
    form.plotId ? { plot_id: form.plotId } : undefined,
  )) as { items: BriefSummary[] };
  briefLibrary.value = response.items;
}

function updateTitle(value?: string | number) {
  if (value !== undefined) form.plotId = String(value);
  const plot = confirmedPlots.value.find((item) => item.id === form.plotId);
  if (plot) form.title = `${plot.code} ${plot.name} 物候档案摘编`;
  void refreshLibrary();
}

async function createBrief() {
  if (!form.plotId || !form.title.trim()) return;
  const result = await workspace.runAction(
    () => workspace.createBrief(form.plotId, form.title),
    "编研简报已生成并冻结",
  );
  if (result) {
    latest.value = result as BriefSummary;
    await refreshLibrary();
  }
}

async function openBrief(brief: BriefSummary) {
  const detail = (await api.getBrief(brief.id)) as BriefSummary;
  latest.value = detail;
}

function downloadBrief() {
  if (!latest.value?.payload) return;
  const payload = latest.value.payload;
  const lines = [
    latest.value.title,
    "",
    `园区：${payload.plot.code} · ${payload.plot.name}`,
    `地点：${payload.plot.locality}`,
    `重点品种：${payload.plot.cultivar_focus}`,
    `责任人或机构：${payload.plot.steward}`,
    `生成时间：${formatTimestamp(latest.value.created_at)}`,
    `快照修订：${latest.value.state_revision}`,
    `事实口径：${latest.value.basis_status === "current" ? "与当前勘误链一致" : "生成时冻结的历史事实"}`,
    "",
    "植株清单",
    ...payload.trees.map(
      (tree) =>
        `- ${tree.code}｜${tree.cultivar}｜砧木 ${tree.rootstock}｜${tree.planting_year} 年`,
    ),
    "",
    "季节志摘要（完成时事实及已采纳勘误，冻结于简报生成时点）",
    ...payload.observations.flatMap((observation) => [
      `${observation.season} 年 · ${observation.tree_code} · ${observation.cultivar}` +
        (observation.has_corrections
          ? `（含 ${observation.current_correction_seq} 次已采纳勘误）`
          : ""),
      ...observation.entries.map(
        (entry) =>
          `  ${stageLabel(entry.stage)}：${entry.observed_on}（置信 ${entry.confidence}/5）`,
      ),
    ]),
  ];
  const blob = new Blob([lines.join("\n")], {
    type: "text/plain;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${payload.plot.code}-物候档案摘编.txt`;
  anchor.click();
  URL.revokeObjectURL(url);
}

const currentBriefs = computed(() =>
  briefLibrary.value.filter((brief) => brief.basis_status === "current"),
);
const historicalBriefs = computed(() =>
  briefLibrary.value.filter((brief) => brief.basis_status === "superseded"),
);
</script>

<template>
  <section class="brief-surface">
    <div class="brief-composer">
      <div class="section-heading">
        <div>
          <span class="eyebrow">档案冻结</span>
          <h2>生成编研简报</h2>
        </div>
        <BookOpenText :size="22" />
      </div>
      <p class="section-lead">
        简报固定园区、植株和已完成季节志在生成时点的内容，后续修订不会改写既有简报。
      </p>

      <form class="brief-form" @submit.prevent="createBrief">
        <label>
          <span>已确认园区</span>
          <ChoiceField
            v-model="form.plotId"
            data-check="brief-plot"
            :choices="plotChoices"
            placeholder="请选择园区"
            @update:model-value="updateTitle"
          />
        </label>
        <label>
          <span>简报标题</span>
          <input v-model="form.title" data-check="brief-title" maxlength="100" />
        </label>
        <button
          type="submit"
          class="button button--primary"
          data-check="create-brief"
          :disabled="!form.plotId || !form.title.trim()"
        >
          <FileCheck2 :size="17" />
          生成冻结简报
        </button>
      </form>

      <div class="brief-method">
        <h3>摘编口径</h3>
        <ul>
          <li>只纳入状态为已完成的季节志，草稿不会进入简报。</li>
          <li>植株清单按编号排序，季节志按年份和编号排序。</li>
          <li>每条物候记录保留观察日期与置信度，不做跨年推断。</li>
          <li>简报冻结生成时点的勘误世代；之后新采纳的勘误不会改写旧简报，只在其封面上标注“已被超越”。</li>
        </ul>
      </div>

      <div v-if="briefLibrary.length" class="brief-library" data-check="brief-library">
        <div v-if="currentBriefs.length" class="brief-library__group">
          <h4><FileCheck2 :size="14" /> 当前简报</h4>
          <button
            v-for="brief in currentBriefs"
            :key="brief.id"
            type="button"
            class="brief-library__item is-current"
            data-check="brief-library-current"
            @click="openBrief(brief)"
          >
            <strong>{{ brief.title }}</strong>
            <small>{{ formatTimestamp(brief.created_at) }} · 修订 {{ brief.state_revision }}</small>
          </button>
        </div>
        <div v-if="historicalBriefs.length" class="brief-library__group">
          <h4><History :size="14" /> 历史简报（季节志已有新勘误）</h4>
          <button
            v-for="brief in historicalBriefs"
            :key="brief.id"
            type="button"
            class="brief-library__item is-historical"
            data-check="brief-library-historical"
            @click="openBrief(brief)"
          >
            <strong>{{ brief.title }}</strong>
            <small>
              {{ formatTimestamp(brief.created_at) }} ·
              {{ brief.superseded_observation_count }} 份季节志已有新结论
            </small>
          </button>
        </div>
      </div>
    </div>

    <div v-if="latest?.payload" class="brief-document" data-check="brief-document">
      <header class="brief-document__header">
        <div class="brief-document__mark">
          <Leaf :size="24" />
        </div>
        <span>果园物候图谱 · 编研简报</span>
        <h2>{{ latest.title }}</h2>
        <p>
          生成于 {{ formatTimestamp(latest.created_at) }} · 快照修订
          {{ latest.state_revision }}
        </p>
        <span
          class="brief-basis"
          :class="
            latest.basis_status === 'current' ? 'is-current' : 'is-historical'
          "
          data-check="brief-basis"
        >
          <BookLock v-if="latest.basis_status === 'superseded'" :size="14" />
          <FileCheck2 v-else :size="14" />
          {{
            latest.basis_status === "current"
              ? "事实口径：与当前勘误链一致"
              : `历史快照：${latest.superseded_observation_count} 份季节志已采用新勘误，本简报内容保持冻结`
          }}
        </span>
      </header>

      <div
        v-if="latest.basis_status === 'superseded'"
        class="basis-banner basis-banner--historical"
        data-check="brief-superseded-banner"
      >
        <History :size="17" />
        <div>
          <strong>这是生成时点的冻结档案，不会被后续勘误改写。</strong>
          <p>
            下列季节志在简报生成后有了新结论：
            {{
              latest.superseded_observations
                ?.map((item) => `${item.season} · ${item.tree_code}`)
                .join("、")
            }}；需要当前口径时请生成新版简报。
          </p>
        </div>
      </div>

      <div class="brief-document__meta">
        <div>
          <Building2 :size="17" />
          <span>
            <small>园区</small>
            <strong>{{ latest.payload.plot.code }} · {{ latest.payload.plot.name }}</strong>
          </span>
        </div>
        <div>
          <Trees :size="17" />
          <span>
            <small>植株与季节志</small>
            <strong>
              {{ latest.tree_count }} 株 / {{ latest.observation_count }} 份
            </strong>
          </span>
        </div>
      </div>

      <section class="brief-document__section">
        <h3>植株目录</h3>
        <div class="brief-tree-list">
          <div v-for="tree in latest.payload.trees" :key="tree.id">
            <strong>{{ tree.code }}</strong>
            <span>{{ tree.cultivar }}</span>
            <small>{{ tree.rootstock }} · {{ tree.planting_year }} 年</small>
          </div>
        </div>
      </section>

      <section class="brief-document__section">
        <h3>季节志摘要</h3>
        <div
          v-for="observation in latest.payload.observations"
          :key="observation.id"
          class="brief-season"
        >
          <header>
            <strong>{{ observation.season }} · {{ observation.tree_code }}</strong>
            <span>{{ observation.cultivar }} / 观察者 {{ observation.observer }}</span>
            <em
              v-if="observation.has_corrections"
              class="brief-season__corrected"
              data-check="brief-season-corrected"
            >
              <GitBranch :size="12" />
              冻结时已含 {{ observation.current_correction_seq }} 次勘误
            </em>
          </header>
          <div class="brief-season__entries">
            <span v-for="entry in observation.entries" :key="entry.id">
              {{ stageLabel(entry.stage) }}
              <strong>{{ entry.observed_on }}</strong>
            </span>
          </div>
        </div>
      </section>

      <footer class="brief-document__footer">
        <span>本地档案快照，不替代农业技术鉴定。</span>
        <button type="button" class="button button--ghost" @click="downloadBrief">
          <Download :size="16" />
          下载文本
        </button>
      </footer>
    </div>

    <EmptyState
      v-else
      title="简报将在生成后显示"
      description="选择已确认园区并生成简报，即可查看冻结的植株与季节志摘要。"
    />
  </section>
</template>
