export type Quality = "GOOD" | "UNCERTAIN" | "BAD" | "STALE";
export type Vec3 = [number, number, number];

export interface TankState {
  name: string;
  level_m: number | null;
  temperature_c: number | null;
  inventory_t_estimated: number | null;
  fill_fraction: number | null;
  capacity_m3: number;
  diameter_m: number;
  height_m: number;
  position: Vec3;
  quality: Quality;
  alarm: string | null;
  density_15c_lab: number;
}

export interface PipeState {
  name: string;
  from: string;
  to: string;
  flow_m3h: number | null;
  flow_inferred: boolean;
  flowing: boolean;
  pressure_bar: number | null;
  path: Vec3[];
  diameter_mm: number;
  quality: Quality;
  meter: string | null;
  pressure_sensor: string | null;
}

export interface PumpState {
  name: string;
  status: "RUNNING" | "STARTING" | "STOPPING" | "STOPPED" | "UNKNOWN";
  speed_rpm: number | null;
  power_kw: number | null;
  discharge_bar: number | null;
  position: Vec3;
  quality: Quality;
}

export interface ValveState {
  name: string;
  position_pct: number | null;
  command: "OPEN" | "CLOSED";
  role: string;
  pipeline: string;
  position: Vec3;
  quality: Quality;
}

export interface SensorState {
  asset_id: string;
  type: string;
  parameter: string;
  value: number | null;
  unit: string;
  quality: Quality;
  ts: number | null;
}

export interface Alert {
  asset_id: string;
  severity: "INFO" | "WARNING" | "CRITICAL";
  message: string;
  event_id?: string;
  investigation_id?: string | null;
  top_hypothesis?: string | null;
}

export interface SimStatus {
  state: string;
  run_id: string | null;
  speed: number;
  steps_per_second?: number;
  scenario_id?: string;
  sim_time_s?: number;
  sim_timestamp?: string;
  active_transfer?: { transfer_id: string; route: string; state: string; setpoint_m3h: number } | null;
  pending_transfers?: { transfer_id: string; route: string; start_s: number }[];
  active_fault_count?: number;
  conservation_error_kg?: number;
}

export interface BalanceSummary {
  t1: number;
  facility_residual_t: number;
  facility_sigma_t?: number;
  chi2: number;
  dof: number;
  p_value: number;
  max_abs_z: number;
  z: Record<string, number>;
  anomaly: boolean;
  route: string | null;
}

export interface TwinState {
  mode: "SIMULATION" | "PLANT";
  timestamp: number | null;
  simulation: SimStatus | null;
  tanks: Record<string, TankState>;
  pipelines: Record<string, PipeState>;
  pumps: Record<string, PumpState>;
  valves: Record<string, ValveState>;
  sensors: Record<string, SensorState>;
  junctions: Record<string, { position: Vec3; name: string }>;
  alerts: Alert[];
  balance: BalanceSummary | null;
}

export interface Hypothesis {
  hypothesis_id: string;
  cause?: string;
  family: string;
  target: string | null;
  category: string;
  label: string;
  calibrated_probability: number;
  magnitude: Record<string, unknown>;
}

export interface Check {
  action_id: string;
  action: string;
  target: string | null;
  description: string;
  reason?: string;
  rationale?: string;
  expected_information_gain_bits: number;
  cost: number;
  duration_h: number;
  requires_isolation: boolean;
  available?: boolean;
  safety_risk?: number;
}

export interface EvidenceItem {
  id: string;
  type: string;
  source: string;
  summary: string;
  values: Record<string, unknown>;
}

export interface Report {
  investigation_id: string;
  status: string;
  diagnosis_status: string | null;
  period: { start: string; end: string; t0: number; t1: number };
  observed_residual_tonnes: number | null;
  residual_uncertainty_tonnes: number | null;
  estimated_physical_loss_tonnes: number | null;
  physical_loss_interval_tonnes: [number, number] | null;
  residual_decomposition_tonnes: Record<string, number> | null;
  category_probabilities: Record<string, number> | null;
  candidate_hypotheses: (Hypothesis & { cause: string })[];
  recommended_check: Check | null;
  alternative_checks: { action_id: string; expected_information_gain_bits: number; available: boolean }[];
  verifications: { action_id: string; outcome: string; detail?: unknown; reviewer?: string }[];
  conflicts: string[];
  missing_evidence: string[];
  evidence: EvidenceItem[];
  narrative: string;
  narrative_source: string;
  grounding: Record<string, unknown>;
  requires_engineer_approval: boolean;
  generated_at: string;
  markdown: string;
}

export interface InvestigationSummary {
  investigation_id: string;
  event_id: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  t0: number;
  t1: number;
  assets: string[];
  top_hypothesis: string | null;
  top_probability: number | null;
  summary: string;
}

export interface InvestigationDetail extends InvestigationSummary {
  report: Report;
  steps: { step: string; ok: boolean; t: number; error?: unknown }[];
  tool_calls: { tool: string; status: string; duration_ms: number; error?: string; arguments: unknown }[];
  balance: any;
  attribution: any;
  ranked_tests: Check[] | null;
  recommended_check: Check | null;
  verifications: Report["verifications"];
  posterior: Record<string, number>;
  final: Record<string, unknown> | null;
  missing_evidence: string[];
  reviews: { reviewer: string; decision: string; verified_cause: string | null; action_id: string | null;
    outcome: string | null; comments: string; created_at: string }[];
  recommendations: { action_id: string; status: string; outcome: string | null; expected_information_gain_bits: number;
    cost: number; created_at: string }[];
  twin_validation?: TwinValidation | null;
  similar_cases?: { case_id: string; similarity: number; verified_cause: string; verification_method: string;
    residual_t: number | null }[];
}

export interface TwinReplay {
  hypothesis_id: string;
  replayed: boolean;
  reason?: string;
  fault?: { fault_type: string; target: string; start_s: number; params: Record<string, number> } | null;
  rows_compared?: number;
  chi2_per_dof?: number;
  p_value?: number;
  explained_fraction?: number | null;
  observed_facility_t?: number;
  twin_facility_t?: number;
  consistent?: boolean;
  per_window?: { window: number; observed_t: number; twin_t: number }[];
}

export interface TwinValidation {
  method: string;
  windows: number;
  transfers_replayed: string[];
  results: TwinReplay[];
  best_supported: string | null;
  seconds: number;
}
