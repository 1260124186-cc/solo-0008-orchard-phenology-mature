import type {
  AbsenceReasonDefinition,
  AbsenceReasonKey,
  ObservationSummary,
  StageDefinition,
  StageEntry,
  StageResolutionState,
} from "./types";

export const STAGES: StageDefinition[] = [
  {
    key: "bud_swell",
    label: "芽膨大期",
    rank: 10,
    required_for_completion: false,
  },
  {
    key: "bud_burst",
    label: "萌芽期",
    rank: 20,
    required_for_completion: true,
  },
  {
    key: "first_bloom",
    label: "初花期",
    rank: 30,
    required_for_completion: false,
  },
  {
    key: "full_bloom",
    label: "盛花期",
    rank: 40,
    required_for_completion: true,
  },
  {
    key: "petal_fall",
    label: "落瓣期",
    rank: 50,
    required_for_completion: false,
  },
  {
    key: "fruit_set",
    label: "坐果期",
    rank: 60,
    required_for_completion: true,
  },
  {
    key: "fruit_growth",
    label: "果实膨大期",
    rank: 70,
    required_for_completion: false,
  },
  {
    key: "harvest",
    label: "采收期",
    rank: 80,
    required_for_completion: true,
  },
  {
    key: "leaf_fall",
    label: "落叶期",
    rank: 90,
    required_for_completion: false,
  },
];

export const STAGE_BY_KEY = Object.fromEntries(
  STAGES.map((stage) => [stage.key, stage]),
) as Record<string, StageDefinition>;

export const ABSENCE_REASONS: AbsenceReasonDefinition[] = [
  {
    key: "unobserved",
    label: "未观察到",
    description: "本季没有观察到该阶段，不代表阶段未发生；必需阶段仍不能完成。",
    resolves_completion: false,
  },
  {
    key: "not_applicable",
    label: "当年不适用",
    description: "有依据表明该阶段本季确实不发生，依据不少于 10 个字，可作为完成依据。",
    resolves_completion: true,
  },
  {
    key: "pending_verification",
    label: "仍在核实",
    description: "观察结果尚待核实；必需阶段核实完成前不能完成季节志。",
    resolves_completion: false,
  },
];

export const ABSENCE_REASON_BY_KEY = Object.fromEntries(
  ABSENCE_REASONS.map((reason) => [reason.key, reason]),
) as Record<AbsenceReasonKey, AbsenceReasonDefinition>;

export const REQUIRED_STAGE_KEYS = STAGES.filter(
  (stage) => stage.required_for_completion,
).map((stage) => stage.key);

export function stageLabel(key: string): string {
  return STAGE_BY_KEY[key]?.label ?? key;
}

export function absenceReasonLabel(key: string): string {
  return ABSENCE_REASON_BY_KEY[key as AbsenceReasonKey]?.label ?? key;
}

export function resolutionStateLabel(state: StageResolutionState): string {
  switch (state) {
    case "observed":
      return "实际观察";
    case "not_applicable":
      return "当年不适用";
    case "unobserved":
      return "未观察到";
    case "pending_verification":
      return "仍在核实";
    case "basis_incomplete":
      return "不适用依据不足";
    case "unrecorded":
      return "未记录";
  }
}

export function sortStageEntries(entries: StageEntry[]): StageEntry[] {
  return [...entries].sort((left, right) => {
    const rank =
      (STAGE_BY_KEY[left.stage]?.rank ?? 999) -
      (STAGE_BY_KEY[right.stage]?.rank ?? 999);
    if (rank !== 0) return rank;
    return left.observed_on.localeCompare(right.observed_on);
  });
}

/** 阶段在某份季节志中的真实状态：已观察 / 不适用 / 未观察 / 待核实 / 未说明。 */
export function stageResolution(
  observation: Pick<
    ObservationSummary,
    "entries" | "absence_markers" | "status" | "completion_basis"
  >,
  stageKey: string,
): StageResolutionState {
  if (observation.entries.some((entry) => entry.stage === stageKey)) {
    return "observed";
  }
  const marker = observation.absence_markers?.find(
    (item) => item.stage === stageKey,
  );
  if (marker) {
    if (
      marker.reason === "not_applicable" &&
      marker.basis.trim().length < 10
    ) {
      return "basis_incomplete";
    }
    return marker.reason;
  }
  // 已完成的旧季节志以冻结依据为准，避免用新规则重新解释历史。
  if (observation.status === "completed" && observation.completion_basis) {
    const frozen = observation.completion_basis.stages.find(
      (item) => item.stage === stageKey,
    );
    if (frozen) return frozen.state;
  }
  return "unrecorded";
}

/** 完成进度：必需阶段中已有观察或经依据确认不适用的占比。 */
export function completedProgress(
  observation: Pick<
    ObservationSummary,
    "entries" | "absence_markers" | "status" | "completion_basis"
  >,
): number {
  const resolved = REQUIRED_STAGE_KEYS.filter(
    (key) =>
      stageResolution(observation, key) === "observed" ||
      stageResolution(observation, key) === "not_applicable",
  ).length;
  return Math.round((resolved / REQUIRED_STAGE_KEYS.length) * 100);
}
