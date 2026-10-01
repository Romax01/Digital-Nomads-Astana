// Типы единого состояния станции (формируется backend, см. backend/app/services/view.py).

export type Dict<T> = Record<string, T>;

export interface Meta {
  model_time: string; real_time: string; speed: number; running: boolean; scenario: string; scenario_title: string;
  station_config: string; state_version: number; seed: number; station_id: string; station_name: string;
  timezone: string; is_demo: boolean;
}

export interface TrackState {
  id: string; number: string; label: string; kind: string; park_id: string; useful_length_m: number | null;
  status: "free" | "occupied" | "unknown" | "contradictory" | "closed" | "reserved"; status_label: string;
  data_state: string; data_state_label: string; data_message?: string; data_observed_at?: string;
  device_id?: string; observed?: string | null; occupant_train_id?: string | null; occupant_number?: string | null;
  closure?: { incident_id: string; title: string; until?: string | null } | null;
  next_reservation?: { start: string; end: string; purpose: string } | null; conflict_ids: string[];
}

export interface TrainPos { path: number[][]; head_s: number; head_end: number; body: number; moving: boolean; speed: number; waiting?: boolean; op_id?: string; kind?: string }

export interface TrainState {
  id: string; number: string; kind: string; priority: number; status: string; status_label: string; wagons: number;
  length_m: number | null; track_id: string | null; origin?: string; destination?: string;
  scheduled_arrival?: string; expected_arrival?: string; scheduled_departure?: string; expected_departure?: string;
  delay_min: number; waiting_reason?: string | null; current_op?: { id: string; kind: string; label: string } | null;
  next_op?: { id: string; kind: string; label: string; start: string } | null; faulty_wagons: string[];
  wagon_kinds: Dict<number>; pos: TrainPos | null; transfer_request_id?: string | null; conflict_ids: string[];
  consist?: { loco_length_m: number; source: string; groups: { kind: string; length_m: number | null; count: number; loaded: boolean; faulty: boolean }[] };
}

/** Положение поезда на уровне «Сеть» (backend: view.py::network_view). */
export interface NetTrain {
  train_id: string; number: string; kind: string; wagons: number; length_m: number | null; delay_min: number;
  phase: "at_origin" | "on_section" | "waiting_entry" | "at_station" | "arrived"; phase_label: string;
  station_id?: string; section_id?: string; track_id?: string; track_no?: number; reverse?: boolean;
  from?: string; to?: string; frac?: number; frac_rate?: number; frac_max?: number; eta?: string;
}

export interface NetworkLive {
  source: string; trains: Dict<NetTrain>;
  stations: Dict<{ id: string; trains_here: number; inbound: number; conflicts?: number; critical?: number;
    free_rd_tracks?: number; rd_tracks?: number; restriction?: { title: string; until?: string | null } | null }>;
}

/** Статическая модель сети (GET /api/v1/network): метры ENU. */
export interface NetworkStatic {
  source: string; note: string; main_station_id: string;
  projection: { type: string; lat0: number; lon0: number; units: string };
  stations: { id: string; name: string; detail: "detailed" | "simplified"; is_main: boolean; lon: number; lat: number;
    x_m: number; y_m: number; axis: number[]; axis_deg: number; half_length_m: number; throats: { west: number[]; east: number[] };
    simplified?: { receiving_tracks?: number; max_train_length_m?: number; processing_min?: number; locomotives_available?: number; accepts?: string[]; travel_min?: number } }[];
  sections: { id: string; name: string; from: string; to: string; from_throat: string; to_throat: string; tracks_count: number;
    length_m: number; length_source: "data" | "geodesic"; max_speed_kmh?: number; physical_spacing_m?: number | null;
    centerline: number[][]; geometry_length_m: number; source: string;
    tracks: { id: string; no: number; vis_offset_m: number; direction: string; points: number[][] }[] }[];
}

export interface OperationState {
  id: string; train_id: string | null; train_number: string | null; kind: string; kind_label: string; seq: number;
  track_id: string | null; from_track_id: string | null; status: string; status_label: string;
  planned_start: string; planned_end: string; forecast_start: string; forecast_end: string;
  actual_start?: string | null; actual_end?: string | null; resource_ids: string[]; route_nodes: string[];
  reserved: boolean; duration_min: number; extra_delay_min: number; note?: string | null; priority: number;
  delay_min: number; conflict_ids: string[];
}

export interface ResourceState {
  id: string; kind: string; kind_label: string; name: string; zone: string | null; status: string; status_label: string;
  current_op: string | null; pos: { x: number; y: number; moving: boolean } | null; conflict_ids: string[];
}

export interface Conflict {
  id: string; type: string; severity: "critical" | "high" | "medium" | "low"; severity_label: string; title: string;
  explanation: string; objects: { type: string; id: string; label: string }[]; operations: string[];
  start?: string | null; end?: string | null; detected_at: string;
}

export interface Recommendation {
  id: string; kind: string; title: string; reason: string; affected: { type: string; id?: string; label: string }[];
  action: any; effect: any; computed_at: string; computed_real_at: string; based_on_version: number;
  status: string; status_label: string;
}

export interface IndexState {
  value: number | null; category: string; category_label: string;
  quality: { level: string; coverage: number; label: string; missing: string[] };
  components: Dict<{ key: string; title: string; unit: string; source: string; raw: number | null; score: number | null; explanation: string; window_min: number }>;
  factors: { key: string; title: string; loss_points: number; explanation: string }[];
  computed_at: string; computed_real_at: string; config_version: number; formula: string;
  weights: Dict<number>; thresholds: Dict<number>; trend?: { t: string; m: string; v: number | null }[];
}

export interface Alert { id: string; severity: string; object_type: string; object_id: string; device_id?: string; message: string; affected_operations: string[]; data_state: string }

export interface ViewState {
  meta: Meta; tracks: Dict<TrackState>; trains: Dict<TrainState>; operations: Dict<OperationState>;
  resources: Dict<ResourceState>; incidents: Dict<any>; conflicts: Dict<Conflict>; recommendations: Dict<Recommendation>;
  requests: Dict<any>; alerts: Dict<Alert>; switches: Dict<any>; index: IndexState | null; plan: any; kpi: any;
  network?: NetworkLive | null;
}

export interface Topology {
  station: { id: string; name: string; timezone: string; is_demo: boolean; note?: string; config_id: string };
  nodes: { id: string; kind: string; name: string; x: number; y: number; side?: string }[];
  tracks: { id: string; number: string; name: string; kind: string; park_id: string; useful_length_m: number | null; points: number[][]; zone_id?: string | null; from_node: string; to_node: string }[];
  connections: { id: string; from_node: string; to_node: string; kind: string; track_id?: string | null; points: number[][] }[];
  zones: { id: string; name: string; kind: string; track_ids: string[]; x: number; y: number; params: any }[];
  parks: { id: string; name: string; kind: string }[];
  devices: { id: string; name: string; kind: string; object_id: string; x: number; y: number; source_mode: string }[];
  bounds: { min_x: number; max_x: number; min_y: number; max_y: number };
  geometry?: { schema_scale_u_per_m: number; turnout_radius_u?: number; turnout_deg?: number };
  layout?: { lane_gap: number; x_entry_west: number; x_entry_east: number };
}

export type Selection = { type: "track" | "train" | "resource" | "switch" | "operation" | "conflict" | "device" | "zone" | "incident" | "station" | "section"; id: string } | null;
