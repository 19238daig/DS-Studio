import type { components } from './generated';
export type Session = components['schemas']['SessionResponse'];
export type SessionMessage = components['schemas']['MessageResponse'];
export type Task = components['schemas']['TaskResponse'];
export type Telemetry = components['schemas']['TelemetryResponse'];
export type Artifact = components['schemas']['ArtifactResponse'];
export type DatabasePreview = components['schemas']['DatabasePreviewResponse'];
export type Version = components['schemas']['ReportVersionResponse'] & {
  base_number?: number;
  created_at?: string;
};
export interface Profile {
  columns: string[];
  row_count: number;
  sheet_or_table?: string;
  dtypes?: Record<string, string>;
  quality_warnings?: string[];
}
export interface Dataset {
  file_id: string;
  name: string;
  size: number;
  source: string;
  session_id?: string | null;
  relative_path: string;
  profile_status?: string | null;
  profile_error?: string | Record<string, unknown> | null;
  profile_task_id?: string | null;
  profile?: Profile | null;
  profiles?: Profile[];
}
export interface Source {
  source_id: string;
  name: string;
  configured: boolean;
  description?: string;
  scope_description?: string;
}
export interface Catalog {
  tables: {
    qualified_name: string;
    name: string;
    columns: { name: string; data_type?: string; type?: string }[];
  }[];
  scope_description?: string;
}
export interface Health {
  status: string;
  models_configured: boolean;
  worker: { ready: boolean; task_id?: string | null; task_ids?: string[] };
  streaming: { enabled: boolean };
  database: { configured: boolean };
  limits: { max_upload_bytes?: number; max_task_input_bytes?: number; max_files_per_task?: number };
}
export interface EventRecord {
  seq: number;
  type: string;
  message?: string;
  role?: string;
  target?: string;
  tool?: string;
  created_at?: string;
  [key: string]: unknown;
}
export interface StreamEvent {
  seq: number | null;
  task_id: string;
  kind: string;
  role?: string;
  data: {
    status?: string;
    text?: string;
    answer?: string;
    verified?: boolean;
    error?: string;
    stage?: string;
    [key: string]: unknown;
  };
}
export const terminal = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'interrupted',
  'waiting_user',
]);
