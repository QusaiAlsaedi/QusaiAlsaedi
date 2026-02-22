export interface DashboardSummary {
  endpoints: { total: number; up: number; down: number; unknown: number };
  servers: { total: number; healthy: number; warning: number; critical: number };
  hl7: { pending: number; stuck_30s: number; acked_1h: number; nacked_1h: number; timeout_1h: number };
  alerts: { open_critical: number; open_warning: number; open_info: number };
  last_updated: string;
}

export interface Endpoint {
  id: string;
  name: string;
  host: string;
  port: number;
  protocol: string;
  check_interval_seconds: number;
  timeout_seconds: number;
  tags: string[];
  metadata: Record<string, string>;
  enabled: boolean;
  last_status: string | null;
  last_checked_at: string | null;
  last_latency_ms: number | null;
  created_at: string;
}

export interface ProbeOut {
  id: string;
  endpoint_id: string;
  endpoint_name: string;
  host: string;
  port: number;
  probed_at: string;
  status: string;
  latency_ms: number | null;
  error_message: string | null;
}

export interface StuckMessage {
  id: string;
  message_control_id: string;
  message_type: string;
  sending_application: string | null;
  receiving_application: string | null;
  detected_at: string;
  age_seconds: number;
  age_human: string;
  server_name: string | null;
  flow_name: string | null;
}

export interface Alert {
  id: string;
  alert_type: string;
  severity: string;
  title: string;
  description: string | null;
  reason_code: string | null;
  status: string;
  created_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  target_type: string | null;
  target_id: string | null;
  metadata: Record<string, any>;
  bite_text?: string | null;
}

export interface ServerSummary {
  server_name: string;
  last_seen: string | null;
  cpu_percent: number | null;
  memory_percent: number | null;
  disk_percent: number | null;
  services: Record<string, string>;
  platform: string | null;
  status: string;  // healthy | warning | critical | unknown
}
