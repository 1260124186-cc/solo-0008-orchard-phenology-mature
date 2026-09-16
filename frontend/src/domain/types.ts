export type WorkspaceKey = "catalog" | "observation" | "comparison" | "brief";

export type PlotStatus = "draft" | "confirmed";
export type TreeStatus = "active" | "retired" | "lost";
export type ObservationStatus = "open" | "completed";

export interface StageDefinition {
  key: string;
  label: string;
  rank: number;
  required_for_completion: boolean;
}

export interface PlotSummary {
  id: string;
  code: string;
  name: string;
  locality: string;
  cultivar_focus: string;
  steward: string;
  planting_year: number;
  note: string;
  status: PlotStatus;
  revision: number;
  created_at: string;
  updated_at: string;
  confirmed_at: string | null;
  tree_count: number;
}

export interface PlotDetail extends PlotSummary {
  trees: readonly TreeRecord[];
}

export interface TreeRecord {
  id: string;
  plot_id: string;
  code: string;
  cultivar: string;
  rootstock: string;
  planting_year: number;
  status: TreeStatus;
  note: string;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface StageEntry {
  id: string;
  stage: string;
  observed_on: string;
  confidence: number;
  note: string;
  created_at: string;
}

export interface EntryLineage {
  stage: string;
  frozen: {
    observed_on: string;
    confidence: number;
    note: string;
  };
  current: {
    observed_on: string;
    confidence: number;
    note: string;
  };
  revised: boolean;
  correction_id: string | null;
}

export interface ObservationSummary {
  id: string;
  tree_id: string;
  plot_id: string;
  tree_code: string;
  cultivar: string;
  season: string;
  observer: string;
  note: string;
  status: ObservationStatus;
  revision: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  stage_count: number;
  entry_map: Record<
    string,
    {
      observed_on: string;
      confidence: number;
      note: string;
    }
  >;
  entries: readonly StageEntry[];
  frozen_entries?: readonly StageEntry[];
  current_entries?: readonly StageEntry[];
  current_correction_id: string | null;
  current_correction_seq: number;
  has_corrections: boolean;
  proposed_correction_count: number;
  resolved_correction_count: number;
  entry_lineage?: readonly EntryLineage[];
}

export type CorrectionStatus =
  | "proposed"
  | "adopted"
  | "rejected"
  | "withdrawn";

export interface CorrectionChange {
  stage: string;
  observed_on?: string;
  confidence?: number;
  note?: string;
}

export interface CorrectionSummary {
  id: string;
  observation_id: string;
  season: string;
  tree_id: string;
  plot_id: string;
  tree_code: string;
  cultivar: string;
  reason: string;
  changes: readonly CorrectionChange[];
  status: CorrectionStatus;
  revision: number;
  proposed_by: string;
  proposed_at: string;
  supersedes_correction_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  adopted_at: string | null;
  adoption_seq: number | null;
  decision_note: string;
  created_at: string;
  updated_at: string;
}

export interface ObservationBasis {
  observation_id: string;
  current_correction_id: string | null;
}

export type BasisStatus = "current" | "superseded";

export interface StageOffset {
  stage: string;
  label: string;
  rank: number;
  left_date: string;
  right_date: string;
  offset_days: number;
  confidence_gap: number;
}

export interface ComparisonSummary {
  id: string;
  title: string;
  season: string;
  left_observation_id: string;
  right_observation_id: string;
  left_label: string;
  right_label: string;
  stage_offsets: readonly StageOffset[];
  summary: {
    title: string;
    common_stage_count: number;
    average_offset_days: number;
    minimum_offset_days: number;
    maximum_offset_days: number;
    earliest_stage: string;
    latest_stage: string;
    direction: string;
    stability: string;
    sentence: string;
  };
  left_basis: ObservationBasis | null;
  right_basis: ObservationBasis | null;
  supersedes_comparison_id: string | null;
  superseded_by_id: string | null;
  basis_status: BasisStatus;
  created_at: string;
}

export interface SupersededObservation {
  observation_id: string;
  frozen_correction_id: string | null;
  current_correction_id: string | null;
  season: string;
  tree_code: string;
}

export interface BriefSummary {
  id: string;
  title: string;
  plot_id: string;
  plot_code: string;
  plot_name: string;
  season_count: number;
  tree_count: number;
  observation_count: number;
  state_revision: number;
  created_at: string;
  basis_status: BasisStatus;
  superseded_observation_count: number;
  superseded_observations?: readonly SupersededObservation[];
  payload?: {
    plot: PlotSummary;
    trees: readonly TreeRecord[];
    observations: readonly ObservationSummary[];
  };
}

export interface ListResponse<T> {
  items: T[];
  total?: number;
  state_revision?: number;
}

export interface ApiErrorPayload {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
}

export interface ApiFailure {
  code: string;
  message: string;
  details: Record<string, unknown>;
  status: number;
}
