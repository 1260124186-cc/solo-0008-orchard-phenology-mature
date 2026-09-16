import type { DatePrecision, StageEntry } from "./types";

export interface DateInterval {
  start: string | null;
  end: string | null;
}

export const DATE_PRECISION_CHOICES: readonly {
  value: DatePrecision;
  label: string;
  hint: string;
}[] = [
  { value: "day", label: "单日", hint: "观察发生在某一确定日期" },
  { value: "range", label: "日期区间", hint: "观察发生在起止日期之间" },
  {
    value: "on_or_before",
    label: "不晚于",
    hint: "只记得最晚边界，可能更早",
  },
  {
    value: "on_or_after",
    label: "不早于",
    hint: "只记得最早边界，可能更晚",
  },
];

export const DATE_PRECISION_LABEL: Record<DatePrecision, string> = {
  day: "单日",
  range: "日期区间",
  on_or_before: "不晚于",
  on_or_after: "不早于",
};

function precisionOf(
  entry: Pick<StageEntry, "precision"> | { precision?: DatePrecision | null },
): DatePrecision {
  // 历史单日记录没有 precision 字段，保持单日含义。
  return entry.precision ?? "day";
}

/** 把条目还原为可能区间；开放端为 null，绝不补一个虚构日期。 */
export function entryInterval(entry: {
  observed_on: string;
  observed_end_on?: string | null;
  precision?: DatePrecision | null;
}): DateInterval {
  switch (precisionOf(entry)) {
    case "range":
      return { start: entry.observed_on, end: entry.observed_end_on ?? null };
    case "on_or_before":
      return { start: null, end: entry.observed_on };
    case "on_or_after":
      return { start: entry.observed_on, end: null };
    default:
      return { start: entry.observed_on, end: entry.observed_on };
  }
}

/** 人类可读的观察时间描述，例如“2026-03-10 至 2026-03-14”“不晚于 2026-04-01”。 */
export function formatObservedDate(entry: {
  observed_on: string;
  observed_end_on?: string | null;
  precision?: DatePrecision | null;
}): string {
  const precision = precisionOf(entry);
  if (precision === "range") {
    return `${entry.observed_on} 至 ${entry.observed_end_on ?? entry.observed_on}`;
  }
  if (precision === "on_or_before") {
    return `不晚于 ${entry.observed_on}`;
  }
  if (precision === "on_or_after") {
    return `不早于 ${entry.observed_on}`;
  }
  return entry.observed_on;
}

/** 只有当前阶段最晚可能日仍早于前一阶段最早可能日时才判定倒退。 */
export function intervalDefinitelyBefore(
  previous: DateInterval,
  current: DateInterval,
): boolean {
  if (previous.start === null || current.end === null) return false;
  return current.end < previous.start;
}

function signedDays(value: number): string {
  if (value === 0) return "0";
  return value > 0 ? `+${value}` : `${value}`;
}

/** 偏移区间描述，例如“晚 2～5 天”“早不少于 4 天”“方向待定”。 */
export function formatOffsetRange(bounds: {
  minimum: number | null;
  maximum: number | null;
  exact: boolean;
  value?: number;
}): string {
  if (bounds.exact && bounds.minimum !== null && bounds.minimum === bounds.maximum) {
    return formatExactOffset(bounds.minimum);
  }
  const { minimum, maximum } = bounds;
  if (minimum !== null && maximum !== null) {
    if (minimum === maximum) return formatExactOffset(minimum);
    if (minimum >= 0 && maximum > 0) {
      return minimum === 0
        ? `同日至晚 ${maximum} 天`
        : `晚 ${minimum}～${maximum} 天`;
    }
    if (maximum <= 0 && minimum < 0) {
      return maximum === 0
        ? `早 ${Math.abs(minimum)} 天内至同日`
        : `早 ${Math.abs(maximum)}～${Math.abs(minimum)} 天`;
    }
    if (minimum < 0 && maximum > 0) {
      return `早 ${Math.abs(minimum)} 天至晚 ${maximum} 天`;
    }
  }
  if (minimum !== null) {
    if (minimum > 0) return `晚不少于 ${minimum} 天`;
    if (minimum === 0) return "不早于同日";
    return `偏移不小于 ${signedDays(minimum)} 天`;
  }
  if (maximum !== null) {
    if (maximum < 0) return `早不少于 ${Math.abs(maximum)} 天`;
    if (maximum === 0) return "不晚于同日";
    return `偏移不大于 ${signedDays(maximum)} 天`;
  }
  return "方向待定";
}

export function formatExactOffset(value: number): string {
  if (value === 0) return "同日";
  return value > 0 ? `晚 ${value} 天` : `早 ${Math.abs(value)} 天`;
}
