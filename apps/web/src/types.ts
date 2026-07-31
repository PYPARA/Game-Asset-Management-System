export type ReviewStatus = "pending" | "approved" | "rejected" | "generating";

export type AssetKind = "content" | "design" | "entity" | "media" | "production";

export type AssetPreview =
  | { kind: "image"; images: string[]; label: string }
  | { kind: "content"; label: string; meta: string; summary: string }
  | { kind: "placeholder"; label: string; detail: string };

export interface LinkedMedia {
  assetId: string;
  key: string;
  name: string;
  subtype: string;
  images: string[];
}

export interface RelatedAsset {
  assetId: string;
  key: string;
  name: string;
  kind: AssetKind;
  subtype: string;
  relationType: string;
  direction: "outgoing" | "incoming";
}

export interface ProjectSummary {
  id: string;
  name: string;
  path: string;
  branch?: string;
  assetCount: number;
  thumbnail: string;
}

export interface AssetRevision {
  id: string;
  sequence: number;
  label: string;
  image: string;
  format: "json" | "markdown" | "media";
  content: unknown;
  createdAt: string;
  author: string;
  resolution: string;
  fileSize: string;
  status: "candidate" | "approved" | "rejected" | "superseded";
}

export interface QACheck {
  id: string;
  label: string;
  result: string;
  passed: boolean;
}

export interface GameAsset {
  id: string;
  candidateRevisionId?: string;
  approvedRevisionId?: string;
  reviewReady?: boolean;
  reviewBlockReason?: string;
  detailsLoaded: boolean;
  key: string;
  schemaRef?: string;
  name: string;
  kind: AssetKind;
  category: string;
  subtype: string;
  subtypeLabel: string;
  tags: string[];
  reviewStatus: ReviewStatus;
  productionStage: string;
  timeAccuracy: "known" | "unknown";
  preview: AssetPreview;
  qaPassed: number;
  qaTotal: number;
  updatedAt: string;
  updatedLabel: string;
  updatedBy: string;
  thumbnails: string[];
  linkedMedia: LinkedMedia[];
  relatedAssets: RelatedAsset[];
  revisionFormat: "json" | "markdown" | "media";
  revisionContent: unknown;
  revisions: AssetRevision[];
  prompt: string;
  negativePrompt: string;
  model: string;
  recipe: string;
  seed: string;
  qa: QACheck[];
}

export interface JobSummary {
  id: string;
  planId?: string;
  taskId?: string;
  name: string;
  status:
    | "queued"
    | "running"
    | "completed"
    | "paused"
    | "awaiting_user"
    | "credentials_locked"
    | "qa_failed";
  progress: number;
  completed: number;
  total: number;
  passed: number;
  previewImages: string[];
  outputPath: string;
}

export interface WorkbenchPayload {
  project: ProjectSummary;
  assets: GameAsset[];
  job: JobSummary;
}

export interface ProviderProfile {
  id: string;
  name: string;
  baseUrl: string;
  textModel: string;
  imageModel: string;
  quality: "low" | "medium" | "high";
  concurrency: number;
  retries: number;
  allowPrivateNetwork: boolean;
}

export type ProviderModelModality = "text" | "image";

export interface ProviderModelRecord {
  id: string;
  modalities: ProviderModelModality[];
  classification: "provider" | "heuristic" | "manual" | "unknown";
  available: boolean;
}

export interface ProviderDefaultRoute {
  provider_profile_id: string;
  model: string;
}

export interface ProviderDefaults {
  text: ProviderDefaultRoute | null;
  image: ProviderDefaultRoute | null;
  updated_at: string | null;
}

export type GenerationTaskKind = "text" | "image" | "image_edit";

export interface GenerationTaskInput {
  id: string;
  kind: GenerationTaskKind;
  asset_id: string;
  prompt: string;
  provider_profile_id?: string;
  model?: string;
  schema?: Record<string, unknown>;
  depends_on: string[];
  width?: number;
  height?: number;
  max_bytes?: number;
  transparent?: boolean;
  reference_task_id?: string;
  target_path?: string;
}

export interface GenerationPlanInput {
  project_id: string;
  provider_profile_id?: string;
  name: string;
  tasks: GenerationTaskInput[];
  extra_call_budget: number;
  max_paid_remediation_rounds: number;
  max_transport_retries: number;
  max_concurrency: number;
}

export interface GenerationProviderProfile {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  text_model: string;
  image_model: string;
  quality: "low" | "medium" | "high";
  concurrency: number;
  max_retries: number;
  allow_private_network: boolean;
  pricing: Record<string, number> | null;
  is_active: boolean;
  models: ProviderModelRecord[];
  models_refreshed_at: string | null;
  is_unlocked: boolean;
}

export interface GenerationPlanRun {
  id: string;
  project_id: string;
  provider_profile_id: string;
  name: string;
  status: string;
  tasks: GenerationTaskInput[];
  estimated_calls: number;
  estimated_cost: number | null;
  suggested_extra_calls: number;
  extra_call_budget: number;
  extra_calls_used: number;
  actual_calls: number;
  actual_cost: number;
  max_paid_remediation_rounds: number;
  max_transport_retries: number;
  max_concurrency: number;
  confirmed_at: string | null;
  created_at: string;
}

export interface GenerationJobRun {
  id: string;
  plan_id: string;
  project_id: string;
  provider_profile_id: string;
  task_id: string;
  task_kind: GenerationTaskKind;
  request: GenerationTaskInput;
  status: string;
  stage: string;
  progress: number;
  result_revision_id: string | null;
  error_category: string | null;
  error_message: string | null;
  attempt_count: number;
  paid_remediation_rounds: number;
  lease_owner: string | null;
  lease_expires_at: string | null;
  heartbeat_at: string | null;
  resolved_request: Record<string, unknown>;
  provider_snapshot: Record<string, unknown>;
  pending_action_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface GenerationAttemptRun {
  id: string;
  job_id: string;
  number: number;
  status: string;
  phase: string;
  purpose: string;
  idempotency_key: string | null;
  request_id: string | null;
  error_category: string | null;
  error_message: string | null;
  output_hash: string | null;
  billable: boolean;
  estimated_cost: number | null;
  started_at: string;
  completed_at: string | null;
}

export interface ProductionFindingRun {
  id: string;
  job_id: string;
  revision_id: string | null;
  code: string;
  severity: string;
  blocking: boolean;
  evidence: Array<Record<string, unknown>>;
  suggested_action: string;
  occurrence: number;
  resolved_at: string | null;
  created_at: string;
}

export interface RunEvidenceItem {
  id: string;
  job_id: string | null;
  revision_id: string | null;
  kind: string;
  label: string;
  path: string | null;
  media_type: string | null;
  sha256: string | null;
  byte_size: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface RemediationRun {
  id: string;
  job_id: string;
  action: string;
  strategy: string;
  status: string;
  reason: string;
  parameters: Record<string, unknown>;
  finding_ids: string[];
  expected_additional_calls: number;
  actual_additional_calls: number;
  created_at: string;
  completed_at: string | null;
}

export interface AgentSessionRun {
  id: string;
  project_id: string;
  plan_id: string | null;
  job_id: string | null;
  asset_id: string | null;
  thread_id: string | null;
  adapter: string;
  adapter_version: string | null;
  schema_version: number;
  status: string;
  context_hash: string;
  context_path: string | null;
  context: Record<string, unknown>;
  sandbox: Record<string, unknown>;
  allowed_actions: string[];
  writable_allowlist: string[];
  budget_limit: number;
  budget_used: number;
  diagnostic_reason: string | null;
  result: Record<string, unknown>;
  stop_reason: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface RunEventItem {
  id: string;
  sequence: number;
  job_id: string | null;
  attempt_id: string | null;
  event_type: string;
  stage: string | null;
  data: Record<string, unknown>;
  causation_id: string | null;
  created_at: string;
}

export interface RunInspection {
  plan: GenerationPlanRun;
  jobs: GenerationJobRun[];
  attempts: GenerationAttemptRun[];
  findings: ProductionFindingRun[];
  evidence: RunEvidenceItem[];
  actions: RemediationRun[];
  events: RunEventItem[];
  agent_sessions?: AgentSessionRun[];
  agent_events?: Array<{
    sequence: number;
    id: string;
    session_id: string;
    project_id: string;
    plan_id: string | null;
    job_id: string | null;
    asset_id: string | null;
    event_type: string;
    thread_id: string | null;
    turn_id: string | null;
    data: Record<string, unknown>;
    created_at: string;
  }>;
  changesets?: Array<Record<string, unknown>>;
}

export interface ReleaseSummary {
  id: string;
  project_id: string;
  name: string;
  manifest_version: number;
  manifest_path: string;
  manifest_hash: string;
  snapshot_hash: string | null;
  asset_count: number;
  created_at: string;
}

export interface ExportConfig {
  project_id: string;
  project: Record<string, unknown>;
  local: { game_root?: string | null } & Record<string, unknown>;
}

export interface ExportPreview {
  project_id: string;
  release_id: string;
  manifest_version: number;
  manifest_hash: string;
  snapshot_hash: string;
  game_root?: string;
  checkout_fingerprint?: string;
  previous_release_id?: string | null;
  issues: string[];
  blocking: boolean;
  no_op?: boolean;
  assets?: Array<Record<string, unknown>>;
  export_config?: Record<string, unknown>;
  changes: Array<Record<string, unknown>>;
  validation_commands?: Array<Record<string, unknown>>;
}

export interface DeliveryRecord {
  id: string;
  project_id: string;
  release_id: string;
  release_manifest_hash: string;
  snapshot_hash: string;
  checkout_fingerprint: string;
  display_path: string;
  status: string;
  previous_release_id: string | null;
  files: Array<Record<string, unknown>>;
  validation_results: Array<Record<string, unknown>>;
  rollback: Record<string, unknown> | null;
  created_at: string;
}
