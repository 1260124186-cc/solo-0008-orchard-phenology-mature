<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  History,
  RefreshCw,
  ScanSearch,
  Wrench,
} from "@lucide/vue";
import type {
  IdentityReport,
  PlotCodeCorrectionPlan,
  PlotDetail,
} from "../domain/types";
import { ApiError } from "../services/api";
import { useWorkspace } from "../app/workspace";

const props = defineProps<{
  plot: PlotDetail;
}>();

const workspace = useWorkspace();

const form = reactive({
  code: props.plot.code,
  reason: "",
});
const plan = ref<PlotCodeCorrectionPlan | null>(null);
const applying = ref(false);
const repairTarget = ref<string | null>(null);
const repairCode = ref("");
const repairReason = ref("");
const repairError = ref("");
const report = ref<IdentityReport | null>(null);
const reportLoading = ref(false);

const pendingInPlan = computed(() => plan.value?.pending ?? []);
const pendingTreeIds = computed(
  () => new Set(pendingInPlan.value.map((item) => item.tree_id)),
);
// 未决植株已在待处理区单独展示并可就地归位，阻断列表不再重复列出。
const blockersInPlan = computed(() =>
  (plan.value?.blockers ?? []).filter(
    (item) => !(item.kind === "tree_code_unresolved" && item.tree_id && pendingTreeIds.value.has(item.tree_id)),
  ),
);

function resetForm() {
  form.code = props.plot.code;
  form.reason = "";
  plan.value = null;
  repairTarget.value = null;
  repairError.value = "";
}

async function preview() {
  if (!form.code.trim()) return;
  try {
    plan.value = await workspace.previewPlotCodeCorrection(
      props.plot.id,
      form.code.trim().toUpperCase(),
    );
  } catch (error) {
    plan.value = null;
    workspace.pushNotice("error", messageOf(error));
  }
}

async function apply() {
  if (!plan.value || !plan.value.applicable) return;
  applying.value = true;
  try {
    const result = await workspace.correctPlotCode(props.plot.id, {
      code: plan.value.new_code ?? form.code,
      reason: form.reason.trim() || "园区编号修正",
      revision: props.plot.revision,
      fingerprint: plan.value.fingerprint,
    });
    workspace.pushNotice(
      "success",
      `编号已整体修正为 ${result.new_code}，${result.trees.length} 株植株同步更新，历史比较与简报保留生成时编号`,
    );
    plan.value = null;
    form.reason = "";
  } catch (error) {
    handlePlanError(error);
  } finally {
    applying.value = false;
  }
}

function startRepair(treeId: string, currentCode: string) {
  repairTarget.value = treeId;
  const suffixIndex = currentCode.lastIndexOf("-T");
  const suffix = suffixIndex >= 0 ? currentCode.slice(suffixIndex) : "-T01";
  repairCode.value = `${props.plot.code}${suffix}`;
  repairReason.value = "";
  repairError.value = "";
}

async function submitRepair() {
  if (!repairTarget.value) return;
  // 修订号优先取计划中的未决项：未决植株可能在页面加载后才通过其他入口出现，
  // 不一定还在 props.plot.trees 中，不能因此静默放弃修复。
  const tree = props.plot.trees.find((item) => item.id === repairTarget.value);
  const pendingItem = pendingInPlan.value.find(
    (item) => item.tree_id === repairTarget.value,
  );
  const revision = tree?.revision ?? pendingItem?.tree_revision;
  if (revision === undefined) {
    repairError.value = "无法确定该植株的当前修订号，请刷新园区详情后重试";
    return;
  }
  repairError.value = "";
  try {
    await workspace.repairTreeCode(repairTarget.value, {
      code: repairCode.value.trim().toUpperCase(),
      reason: repairReason.value.trim() || "植株编号归位",
      revision,
    });
    workspace.pushNotice("success", "未决植株编号已归位，请重新预览修正计划");
    repairTarget.value = null;
    if (form.code.trim() && form.code.trim().toUpperCase() !== props.plot.code) {
      await preview();
    } else {
      plan.value = null;
    }
  } catch (error) {
    repairError.value = messageOf(error);
  }
}

async function loadReport() {
  reportLoading.value = true;
  try {
    report.value = await workspace.loadIdentityReport();
  } catch (error) {
    workspace.pushNotice("error", messageOf(error));
  } finally {
    reportLoading.value = false;
  }
}

function handlePlanError(error: unknown) {
  if (error instanceof ApiError) {
    const blockers = error.failure.details.blockers as
      | PlotCodeCorrectionPlan["blockers"]
      | undefined;
    if (Array.isArray(blockers) && blockers.length) {
      const pending = (error.failure.details.pending as
        | PlotCodeCorrectionPlan["pending"]
        | undefined) ?? blockers.filter((item) => item.kind === "tree_code_unresolved");
      plan.value = {
        ...(plan.value ?? {
          plot_id: props.plot.id,
          plot_code: props.plot.code,
          plot_revision: props.plot.revision,
          new_code: form.code,
          status: props.plot.status,
          fingerprint: "",
          changes: [],
          pending: [],
        }),
        applicable: false,
        blockers,
        pending,
      };
    }
    workspace.pushNotice("error", error.message);
    return;
  }
  workspace.pushNotice("error", messageOf(error));
}

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "操作失败，请重试";
}

function aliasLabel(alias: { code: string; changed_to: string }): string {
  return `${alias.code} → ${alias.changed_to}`;
}
</script>

<template>
  <section class="recode-panel" data-check="plot-recode-panel">
    <header class="recode-panel__header">
      <div>
        <span class="eyebrow">编号修正</span>
        <h3>园区编号变更 · 关联植株一次性级联</h3>
      </div>
      <RefreshCw :size="20" />
    </header>
    <p class="recode-panel__lead">
      修正会在同一事务内更新园区和全部在册植株编号；存在重复编号、并发修订或前缀不一致时整批不落库。
      历史比较与简报继续显示生成时编号，新旧编号通过别名轨迹互相解释。
    </p>

    <form class="recode-form" @submit.prevent="preview">
      <div class="form-grid form-grid--two">
        <label>
          <span>新园区编号</span>
          <input
            v-model="form.code"
            data-check="recode-code"
            maxlength="11"
            placeholder="例如 OR-2201"
          />
        </label>
        <label>
          <span>修正原因</span>
          <input
            v-model="form.reason"
            data-check="recode-reason"
            maxlength="200"
            placeholder="例如 录入笔误、园区编号换段"
          />
        </label>
      </div>
      <div class="form-actions">
        <button type="submit" class="button button--secondary" data-check="recode-preview">
          <ScanSearch :size="16" />
          预览级联计划
        </button>
        <button
          type="button"
          class="button button--ghost"
          data-check="recode-reset"
          @click="resetForm"
        >
          清空
        </button>
      </div>
    </form>

    <div v-if="plan" class="recode-plan" data-check="recode-plan">
      <div
        v-if="plan.applicable"
        class="recode-plan__banner recode-plan__banner--ok"
      >
        <CheckCircle2 :size="18" />
        <span>
          计划可执行：园区 {{ plan.plot_code }} 改为 {{ plan.new_code }}，
          {{ plan.changes.length }} 株植株将同步更换编号前缀。
        </span>
      </div>
      <div v-else class="recode-plan__banner recode-plan__banner--blocked">
        <AlertTriangle :size="18" />
        <span>
          计划存在 {{ plan.blockers.length }} 项阻断，修正不会写入任何新编号。
        </span>
      </div>

      <ul v-if="pendingInPlan.length" class="recode-pending" data-check="recode-pending">
        <li
          v-for="item in pendingInPlan"
          :key="item.tree_id"
          class="recode-pending__item"
        >
          <div>
            <strong>{{ item.tree_code }}</strong>
            <span>{{ item.message }}</span>
          </div>
          <button
            v-if="repairTarget !== item.tree_id && item.tree_id && item.tree_code"
            type="button"
            class="button button--ghost button--small"
            data-check="recode-repair-start"
            @click="startRepair(item.tree_id as string, item.tree_code as string)"
          >
            <Wrench :size="14" />
            修正这株
          </button>
          <form
            v-else
            class="recode-repair"
            data-check="recode-repair-form"
            @submit.prevent="submitRepair"
          >
            <input
              v-model="repairCode"
              data-check="recode-repair-code"
              maxlength="17"
            />
            <input
              v-model="repairReason"
              data-check="recode-repair-reason"
              maxlength="200"
              placeholder="归位原因（可选）"
            />
            <button type="submit" class="button button--secondary button--small">
              确认归位
            </button>
            <button
              type="button"
              class="button button--ghost button--small"
              @click="repairTarget = null"
            >
              取消
            </button>
          </form>
        </li>
      </ul>
      <p v-if="repairError" class="recode-repair__error">{{ repairError }}</p>

      <ul v-if="blockersInPlan.length" class="recode-blockers">
        <li v-for="(item, index) in blockersInPlan" :key="`${item.kind}-${index}`">
          <AlertTriangle :size="14" />
          <span>{{ item.message }}</span>
        </li>
      </ul>

      <details v-if="plan.changes.length" class="recode-changes">
        <summary>查看 {{ plan.changes.length }} 株植株的编号变化</summary>
        <ul>
          <li v-for="change in plan.changes" :key="change.tree_id">
            <code>{{ change.tree_code }}</code>
            <ArrowRight :size="13" />
            <code>{{ change.next_code }}</code>
          </li>
        </ul>
      </details>

      <button
        type="button"
        class="button button--primary"
        data-check="recode-apply"
        :disabled="!plan.applicable || applying"
        @click="apply"
      >
        <RefreshCw :size="16" />
        {{ applying ? "正在整批修正…" : "确认并整批修正" }}
      </button>
    </div>

    <div v-if="plot.code_aliases?.length" class="recode-history" data-check="plot-code-history">
      <header>
        <History :size="15" />
        <strong>园区编号轨迹</strong>
      </header>
      <ol>
        <li v-for="alias in plot.code_aliases" :key="`${alias.code}-${alias.changed_at}`">
          <code>{{ aliasLabel(alias) }}</code>
          <span>{{ alias.reason }} · {{ alias.actor_id }}</span>
        </li>
      </ol>
    </div>

    <div class="recode-identity">
      <button
        type="button"
        class="button button--ghost button--small"
        data-check="identity-report-load"
        :disabled="reportLoading"
        @click="loadReport"
      >
        <ScanSearch :size="14" />
        核对身份唯一性与历史归属
      </button>
      <div v-if="report" class="recode-identity__report" data-check="identity-report">
        <p :class="report.unique ? 'is-ok' : 'is-bad'">
          <CheckCircle2 v-if="report.unique" :size="15" />
          <AlertTriangle v-else :size="15" />
          {{
            report.unique
              ? `身份核对通过：${report.plot_count} 个园区、${report.tree_count} 株植株编号唯一，前缀一致。`
              : "身份核对发现问题，请先处理下列对象。"
          }}
        </p>
        <ul v-if="report.duplicate_plot_codes.length">
          <li v-for="item in report.duplicate_plot_codes" :key="item.code">
            园区编号 {{ item.code }} 被 {{ item.plot_ids.length }} 个园区占用
          </li>
        </ul>
        <ul v-if="report.duplicate_tree_codes.length">
          <li v-for="item in report.duplicate_tree_codes" :key="`${item.plot_id}-${item.code}`">
            同园区植株编号 {{ item.code }} 重复
          </li>
        </ul>
        <ul v-if="report.unresolved_trees.length" data-check="identity-unresolved">
          <li v-for="item in report.unresolved_trees" :key="item.tree_id">
            植株 {{ item.tree_code }} 与所属园区 {{ item.plot_code ?? "缺失" }} 前缀不一致
          </li>
        </ul>
        <details>
          <summary>
            历史结果归属（{{ report.historical_objects.length }} 条比较/简报）
          </summary>
          <ul>
            <li
              v-for="item in report.historical_objects"
              :key="`${item.kind}-${item.id}`"
              :class="item.explainable ? 'is-ok' : 'is-bad'"
            >
              <CheckCircle2 v-if="item.explainable" :size="13" />
              <AlertTriangle v-else :size="13" />
              <span v-if="item.kind === 'comparison'">
                对比图谱保留生成时编号：{{ item.left_label }} / {{ item.right_label }}
              </span>
              <span v-else>
                简报保留生成时园区编号：{{ item.plot_code }}（修订 {{ item.state_revision }}）
              </span>
            </li>
          </ul>
        </details>
      </div>
    </div>
  </section>
</template>
