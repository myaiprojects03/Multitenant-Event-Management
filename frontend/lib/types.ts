export type Role = "attendee" | "exhibitor" | "organizer";

export interface DemoPersona {
  email: string;
  persona_label: string | null;
  full_name: string;
  tenant_name: string;
  event_id: string;
  event_name: string;
  role: Role;
  exhibitor_name: string | null;
}

export interface Session {
  access_token: string;
  expires_in: number;
  user_id: string;
  full_name: string;
  email: string;
  tenant_id: string;
  tenant_name: string;
  event_id: string;
  event_name: string;
  role: Role;
  exhibitor_id: string | null;
  exhibitor_name: string | null;
  issued_at: number;
}

export interface Citation {
  ref: number;
  entity_type: string;
  entity_id: string;
  title: string;
  snippet: string;
}

export interface AssistantAnswer {
  answer: string;
  route: string;
  citations: Citation[];
  action: string | null;
  data: unknown;
  cached: boolean;
  latency_ms: number;
}

export interface Chart {
  type: string;
  x: string | null;
  y: string[];
}

export interface NlSqlResponse {
  question: string;
  status: string;
  sql: string | null;
  explanation: string;
  summary: string;
  key_points: string[];
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
  chart: Chart | null;
  repaired: boolean;
  error: string | null;
  cached: boolean;
  latency_ms: number;
}

export interface AnomalyFlag {
  kind: string;
  severity: string;
  title: string;
  metric: string;
  value: number;
  baseline: number | null;
  detail: string;
  entity_id: string | null;
}

export interface AnomalyReport {
  flags: AnomalyFlag[];
  explanation: string;
  cached: boolean;
}

export type RecType = "peers" | "sessions" | "speakers" | "exhibitors" | "prospects";

export interface Recommendation {
  target_type: "peer" | "session" | "speaker" | "exhibitor" | "prospect";
  target_id: string;
  title: string;
  subtitle: string;
  category: string | null;
  score: number;
  reason: string;
  facts: string[];
  data: Record<string, unknown>;
}

export interface RecommendationList {
  type: RecType;
  items: Recommendation[];
  phrased: boolean;
  message: string | null;
}

export interface Me {
  user_id: string;
  full_name: string;
  email: string;
  event_name: string;
  role: Role;
  networking_opt_in: boolean | null;
  exhibitor_matching_opt_in: boolean | null;
}

export interface ConnectionRow {
  id: string;
  status: string;
  created_at: string;
  direction: "outgoing" | "incoming";
  other_user_id: string;
  display_name: string | null;
  job_title: string | null;
  contact_email: string | null;
}

export interface MeetingRow {
  id: string;
  status: string;
  initiated_by: "attendee" | "exhibitor";
  created_at: string;
  message: string | null;
  exhibitor_name: string;
  booth: string | null;
  attendee_name: string | null;
}

export interface HistoryEntry {
  at: string;
  from?: string | null;
  to?: string;
  actor_role?: string | null;
  note?: string;
}

export interface ApprovalRow {
  id: string;
  action_type: string;
  status: string;
  required_role: Role;
  escalation_level: number;
  draft: Record<string, unknown>;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  expires_at: string;
  created_at: string;
}

export interface ActionRow {
  id: string;
  stage: number;
  channel: "email" | "platform_api" | "publish";
  recipient: string | null;
  subject: string | null;
  status: "queued" | "retrying" | "sent" | "dead_letter" | "cancelled";
  attempts: number;
  last_error: string | null;
  next_attempt_at: string;
  provider_message_id: string | null;
  sent_at: string | null;
}

export interface WorkflowDetail {
  id: string;
  kind: string;
  status: string;
  owner_user_id: string;
  owner_exhibitor_id: string | null;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error: string | null;
  history: HistoryEntry[];
  created_at: string;
  updated_at: string;
  approvals: ApprovalRow[];
  actions: ActionRow[];
  action_counts: Record<string, number>;
}

export interface WorkflowRow {
  id: string;
  kind: string;
  status: string;
  error: string | null;
  action_type: string | null;
  approval_chain: string[] | null;
  created_at: string;
  updated_at: string;
}

export interface QueueRow {
  id: string;
  workflow_id: string;
  kind: string;
  action_type: string;
  required_role: Role;
  escalation_level: number;
  draft: Record<string, unknown>;
  expires_at: string;
  created_at: string;
  flags: string[] | null;
  approval_chain: string[] | null;
}

export interface FollowUpMessage {
  lead_id: string;
  recipient_user_id: string;
  to_name: string;
  to: string;
  score: number;
  tier: string;
  reasons: string[];
  subject: string;
  body: string;
  next_step?: string;
  flags: string[];
  edited?: boolean;
}

export interface ProgramSession {
  id: string;
  title: string;
  kind: string;
  starts_at: string;
  ends_at: string;
  room: string | null;
  room_capacity: number | null;
  bookmarks: number;
  attended: number;
}

export interface RouteStats {
  route_type: string;
  calls: number;
  errors: number;
  cache_hits: number;
  p50_ms: number | null;
  p95_ms: number | null;
  avg_tokens: number | null;
}

export interface TelemetrySummary {
  window_minutes: number;
  calls: number;
  errors: number;
  cache_hit_rate: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  p50_uncached_ms: number | null;
  p50_cached_ms: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  avg_tokens_per_call: number | null;
  cost_usd: number;
  by_route: RouteStats[];
  by_model: Record<string, number>;
}

export interface UsageRow {
  created_at: string;
  request_id: string;
  user_role: string | null;
  route_type: string;
  model: string | null;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  cache_hit: boolean;
  status: string;
  error: string | null;
}

export interface SentMessage {
  id: string;
  to: string;
  subject: string | null;
  body: string | null;
  workflow_id: string;
  sent_at: string;
}
