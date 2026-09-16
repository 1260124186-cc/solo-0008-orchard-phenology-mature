import {
  entryInterval,
  intervalDefinitelyBefore,
} from "./datePrecision";
import { STAGE_BY_KEY } from "./stages";
import type {
  DatePrecision,
  ObservationSummary,
  PlotSummary,
  StageEntry,
  TreeRecord,
} from "./types";

export interface FieldIssue {
  field: string;
  message: string;
}

export function validatePlotDraft(plot: Partial<PlotSummary>): FieldIssue[] {
  const issues: FieldIssue[] = [];
  const code = String(plot.code ?? "").trim().toUpperCase();
  if (!/^[A-Z]{2,6}-\d{3,4}$/.test(code)) {
    issues.push({
      field: "code",
      message: "园区编号应为两个到六个字母、连字符和三到四位数字",
    });
  }
  if (!String(plot.name ?? "").trim()) {
    issues.push({ field: "name", message: "请填写园区名称" });
  }
  if (!String(plot.locality ?? "").trim()) {
    issues.push({ field: "locality", message: "请填写地点描述" });
  }
  if (!String(plot.cultivar_focus ?? "").trim()) {
    issues.push({ field: "cultivar_focus", message: "请填写重点品种" });
  }
  if (!String(plot.steward ?? "").trim()) {
    issues.push({ field: "steward", message: "请填写档案责任人或机构" });
  }
  const year = Number(plot.planting_year);
  if (!Number.isInteger(year) || year < 1800 || year > new Date().getFullYear() + 2) {
    issues.push({ field: "planting_year", message: "起始种植年份超出范围" });
  }
  return issues;
}

export function validateTreeDraft(tree: Partial<TreeRecord>): FieldIssue[] {
  const issues: FieldIssue[] = [];
  if (!String(tree.code ?? "").trim()) {
    issues.push({ field: "code", message: "请填写植株编号" });
  }
  if (!String(tree.cultivar ?? "").trim()) {
    issues.push({ field: "cultivar", message: "请填写品种名" });
  }
  const year = Number(tree.planting_year);
  if (!Number.isInteger(year) || year < 1800) {
    issues.push({ field: "planting_year", message: "请填写有效定植年份" });
  }
  return issues;
}

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const DATE_PRECISIONS: readonly DatePrecision[] = [
  "day",
  "range",
  "on_or_before",
  "on_or_after",
];

function parseIsoDate(value: string): Date | null {
  if (!DATE_PATTERN.test(value)) return null;
  const parts = value.split("-").map(Number);
  const parsed = new Date(parts[0], parts[1] - 1, parts[2]);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function sameDayIso(value: Date): string {
  const month = `${value.getMonth() + 1}`.padStart(2, "0");
  const day = `${value.getDate()}`.padStart(2, "0");
  return `${value.getFullYear()}-${month}-${day}`;
}

export function stageDraftInterval(
  entry: Partial<StageEntry>,
): { start: string | null; end: string | null } | null {
  const precision: DatePrecision = entry.precision ?? "day";
  if (!entry.observed_on || !DATE_PATTERN.test(entry.observed_on)) return null;
  if (precision === "range") {
    if (
      !entry.observed_end_on ||
      !DATE_PATTERN.test(entry.observed_end_on) ||
      entry.observed_end_on < entry.observed_on
    ) {
      return null;
    }
    return { start: entry.observed_on, end: entry.observed_end_on };
  }
  if (precision === "on_or_before") {
    return { start: null, end: entry.observed_on };
  }
  if (precision === "on_or_after") {
    return { start: entry.observed_on, end: null };
  }
  return { start: entry.observed_on, end: entry.observed_on };
}

export function validateStageDraft(
  entry: Partial<StageEntry>,
  existing: readonly StageEntry[],
  season: string,
): FieldIssue[] {
  const issues: FieldIssue[] = [];
  if (!entry.stage || !STAGE_BY_KEY[entry.stage]) {
    issues.push({ field: "stage", message: "请选择物候阶段" });
  }
  const precision: DatePrecision = entry.precision ?? "day";
  if (!DATE_PRECISIONS.includes(precision)) {
    issues.push({ field: "precision", message: "请选择日期精度" });
  }
  if (!entry.observed_on || !DATE_PATTERN.test(entry.observed_on)) {
    issues.push({ field: "observed_on", message: "请选择观察日期" });
  }
  if (precision === "range") {
    if (!entry.observed_end_on || !DATE_PATTERN.test(entry.observed_end_on)) {
      issues.push({ field: "observed_end_on", message: "请补全区间结束日期" });
    } else if (
      entry.observed_on &&
      DATE_PATTERN.test(entry.observed_on) &&
      entry.observed_end_on < entry.observed_on
    ) {
      issues.push({
        field: "observed_end_on",
        message: "结束日期不能早于开始日期",
      });
    }
  }
  if (entry.observed_on && DATE_PATTERN.test(entry.observed_on)) {
    const observed = parseIsoDate(entry.observed_on);
    const windowStart = parseIsoDate(seasonWindow(season).start);
    const windowEnd = parseIsoDate(seasonWindow(season).end);
    if (observed !== null && windowStart !== null && windowEnd !== null) {
      if (observed < windowStart || observed > windowEnd) {
        issues.push({
          field: "observed_on",
          message: "观察日期超出该季节允许窗口",
        });
      }
      if (precision === "range" && entry.observed_end_on) {
        const end = parseIsoDate(entry.observed_end_on);
        if (end !== null && (end < windowStart || end > windowEnd)) {
          issues.push({
            field: "observed_end_on",
            message: "区间结束日期超出该季节允许窗口",
          });
        }
      }
    }
  }
  if (existing.some((item) => item.stage === entry.stage)) {
    issues.push({ field: "stage", message: "该阶段已经存在" });
  }
  const confidence = Number(entry.confidence);
  if (!Number.isInteger(confidence) || confidence < 1 || confidence > 5) {
    issues.push({ field: "confidence", message: "置信度须在 1 到 5 之间" });
  }
  const candidateInterval = stageDraftInterval(entry);
  if (candidateInterval) {
    const candidate = [
      ...existing
        .filter((item) => STAGE_BY_KEY[item.stage])
        .map((item) => ({
          stage: item.stage,
          interval: entryInterval(item),
        })),
      { stage: entry.stage ?? "", interval: candidateInterval },
    ].sort(
      (left, right) =>
        STAGE_BY_KEY[left.stage].rank - STAGE_BY_KEY[right.stage].rank,
    );
    for (let index = 1; index < candidate.length; index += 1) {
      const previous = candidate[index - 1];
      const current = candidate[index];
      if (intervalDefinitelyBefore(previous.interval, current.interval)) {
        issues.push({
          field: "observed_on",
          message: `${STAGE_BY_KEY[current.stage].label}不可能早于${STAGE_BY_KEY[previous.stage].label}`,
        });
        break;
      }
    }
  }
  return issues;
}

export function seasonWindow(season: string): { start: string; end: string } {
  const year = Number(season);
  return { start: sameDayIsoDate(year - 1, 9, 3), end: sameDayIsoDate(year + 1, 2, 31) };
}

function sameDayIsoDate(year: number, month: number, day: number): string {
  return sameDayIso(new Date(year, month, day));
}

export function missingRequiredStages(observation: ObservationSummary): string[] {
  const present = new Set(observation.entries.map((entry) => entry.stage));
  return Object.values(STAGE_BY_KEY)
    .filter((stage) => stage.required_for_completion && !present.has(stage.key))
    .map((stage) => stage.label);
}

export function canCompareObservations(
  left: Pick<ObservationSummary, "id" | "status" | "season" | "entries"> | null,
  right: Pick<ObservationSummary, "id" | "status" | "season" | "entries"> | null,
): boolean {
  if (!left || !right || left.id === right.id) return false;
  if (left.status !== "completed" || right.status !== "completed") return false;
  if (left.season !== right.season) return false;
  const leftStages = new Set(left.entries.map((entry) => entry.stage));
  return right.entries.some((entry) => leftStages.has(entry.stage));
}

export function formatOffset(value: number): string {
  if (value === 0) return "同日";
  return value > 0 ? `晚 ${value} 天` : `早 ${Math.abs(value)} 天`;
}

export function formatTimestamp(value: string | null): string {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
