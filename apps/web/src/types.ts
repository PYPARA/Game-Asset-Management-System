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
  /** Publication state is independent from review/content state. */
  publicationStatus?: string;
  /** Optional author metadata; local projects may not provide this field. */
  createdBy?: string;
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
  sourcePath?: string;
  sourceMissing?: boolean;
  sourceDriftStatus?: "in_sync" | "candidate" | "rejected" | "superseded" | "historical" | "missing" | "invalid";
  recipeId?: string;
  sceneInfo?: {
    chapterAssetId: string | null;
    chapterTitle: string | null;
    assigned: boolean;
    required: number;
    ready: number;
    missing: number;
    ratio: number;
  };
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

export interface NarrativeCoverage {
  required: number;
  ready: number;
  candidate: number;
  planned: number;
  missing: number;
  ratio: number;
}

export interface NarrativeRequirement {
  id: string;
  asset_key: string;
  asset_id: string | null;
  title: string;
  kind: AssetKind;
  subtype: string;
  role: string;
  prompt: string;
  width: number;
  height: number;
  transparent: boolean;
  target_path: string | null;
  order: number;
  status: "ready" | "candidate" | "planned" | "missing";
  rendition_id: string | null;
}

export interface NarrativeGraphNode {
  asset_id: string;
  key: string;
  title: string;
  kind: AssetKind;
  subtype: string;
  status: "ready" | "candidate" | "planned" | "missing";
  rendition_id: string | null;
  role: string;
}

export interface NarrativeGraphEdge {
  id: string;
  source_asset_id: string;
  target_asset_id: string;
  relation_type: string;
  metadata: Record<string, unknown>;
}

export interface NarrativeScene {
  asset_id: string;
  key: string;
  title: string;
  chapter_asset_id: string | null;
  order: number;
  revision_id: string | null;
  revision_status: string | null;
  content: Record<string, unknown>;
  requirements: NarrativeRequirement[];
  coverage: NarrativeCoverage;
  graph: { nodes: NarrativeGraphNode[]; edges: NarrativeGraphEdge[] };
}

export interface NarrativeChapter {
  asset_id: string;
  key: string;
  title: string;
  order: number;
  scene_ids: string[];
  revision_id: string | null;
  revision_status: string | null;
}

export interface NarrativeMap {
  project_id: string;
  project_name: string;
  chapters: NarrativeChapter[];
  scenes: NarrativeScene[];
  unassigned_scene_ids: string[];
  coverage: NarrativeCoverage;
}

export interface NarrativeMaterializeResult {
  project_id: string;
  scene_asset_id: string;
  asset_ids: string[];
  created_asset_ids: string[];
  requirement_ids: string[];
}

export interface ProviderProfile {
  id: string;
  name: string;
  baseUrl: string;
  allowPrivateNetwork: boolean;
  credentialMode: ProviderCredentialMode;
  modelDiscoveryMode: ProviderModelDiscoveryMode;
  modelsPath: string;
}

export type ProviderModelModality = "text" | "image" | "video" | "audio";
export type ProviderCredentialMode = "required" | "optional" | "none";
export type ProviderModelDiscoveryMode = "auto" | "manual";

export interface ProviderModelRecord {
  id: string;
  modalities: ProviderModelModality[];
  classification: "provider" | "heuristic" | "manual" | "unknown";
  available: boolean;
  enabled: boolean;
}

export interface ProviderDefaultRoute {
  provider_profile_id: string;
  model: string;
}

export interface ProviderModelsSync {
  state: "never" | "synced" | "empty" | "manual_required" | "error";
  checked_at: string | null;
  endpoint: string | null;
  status_code: number | null;
  message: string | null;
  hint: string | null;
  request_id: string | null;
}

export interface ProviderDefaults {
  text: ProviderDefaultRoute | null;
  image: ProviderDefaultRoute | null;
  video: ProviderDefaultRoute | null;
  audio: ProviderDefaultRoute | null;
  max_concurrency: number;
  max_transport_retries: number;
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
  reference_path?: string;
  metadata?: Record<string, unknown>;
}

export type GenerationAssetProposalMode = "existing" | "new";
export type GenerationReferenceRole = "primary" | "supporting";

export interface GenerationAssetProposal {
  mode: GenerationAssetProposalMode;
  asset_id?: string | null;
  key?: string | null;
  kind?: AssetKind | null;
  subtype?: string | null;
  title?: string | null;
  schema_ref?: string | null;
  tags: string[];
  metadata: Record<string, unknown>;
}

export interface GenerationReferenceProposal {
  asset_id: string;
  role: GenerationReferenceRole;
  reason: string;
  revision_id?: string | null;
  rendition_id?: string | null;
  sha256?: string | null;
}

export interface GenerationTaskProposal {
  id: string;
  asset: GenerationAssetProposal;
  kind: GenerationTaskKind;
  prompt: string;
  provider_profile_id?: string | null;
  model?: string | null;
  schema?: Record<string, unknown> | null;
  depends_on: string[];
  width?: number | null;
  height?: number | null;
  max_bytes?: number | null;
  transparent: boolean;
  reference_task_id?: string | null;
  references: GenerationReferenceProposal[];
  target_path?: string | null;
  candidate_path?: string | null;
  locked_fields: string[];
  metadata: Record<string, unknown>;
}

export interface GenerationPlanningDraft {
  version: number;
  title: string;
  summary: string;
  tasks: GenerationTaskProposal[];
  questions: string[];
  assumptions: string[];
  warnings: GenerationPlanningWarning[];
  settings: {
    provider_profile_id?: string | null;
    extra_call_budget?: number;
    max_paid_remediation_rounds?: number;
    max_transport_retries?: number;
    max_concurrency?: number;
    route_defaults?: Partial<Record<ProviderModelModality, ProviderDefaultRoute | null>>;
    [key: string]: unknown;
  };
  context_hash?: string | null;
}

export interface GenerationPlanningWarning {
  code: string;
  message: string;
}

export type GenerationConversationEventType =
  | "user.message"
  | "turn.started"
  | "assistant.delta"
  | "assistant.message"
  | "reasoning.summary"
  | "tool.started"
  | "tool.progress"
  | "tool.result"
  | "usage.updated"
  | "draft.updated"
  | "turn.completed"
  | "turn.failed"
  | "agent.unavailable"
  | "user_input.requested"
  | "user_input.resolved"
  | "user_input.fallback"
  | "user_input.cancelled"
  | "conversation.confirmed";

export interface GenerationConversationEvent {
  sequence: number;
  id: string;
  session_id: string;
  event_type: GenerationConversationEventType | string;
  thread_id: string | null;
  turn_id: string | null;
  data: Record<string, unknown>;
  created_at: string;
}

export interface GenerationConversation {
  id: string;
  project_id: string;
  plan_id: string | null;
  thread_id: string | null;
  purpose: "generation_planning" | string;
  title: string | null;
  agent_model: string | null;
  status: string;
  context_hash: string;
  seed_asset_ids: string[];
  context: Record<string, unknown>;
  draft: GenerationPlanningDraft;
  draft_hash: string | null;
  draft_version: number;
  budget_limit: number;
  budget_used: number;
  turn_count: number;
  diagnostic_reason: string | null;
  stop_reason: string | null;
  usage: Record<string, unknown>;
  last_error: GenerationConversationError | null;
  pending_input?: GenerationInputRequest | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  archived_at: string | null;
}

export interface GenerationInputQuestion {
  id: string;
  header: string;
  question: string;
  options: Array<{ label: string; description: string }> | null;
  isOther?: boolean;
  isSecret?: boolean;
}

export interface GenerationInputRequest {
  id: string;
  turn_id: string;
  response_mode: "resume_turn" | "new_turn";
  status: "pending" | "resolved" | "cancelled";
  questions: GenerationInputQuestion[];
  answers: Record<string, { answers: string[] }>;
  auto_resolution_ms: number | null;
  fallback_reason: string | null;
}

export interface GenerationConversationError {
  error_code?: string;
  reason?: string;
  message?: string;
  retryable?: boolean;
  field_path?: string;
  validation_message?: string;
  [key: string]: unknown;
}

export interface GenerationConversationSummary {
  id: string;
  project_id: string;
  plan_id: string | null;
  title: string | null;
  agent_model: string | null;
  status: string;
  turn_count: number;
  budget_limit: number;
  budget_used: number;
  seed_asset_ids: string[];
  error_summary: GenerationConversationError | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  archived_at: string | null;
}

export interface GenerationAgentModel {
  id: string;
  name: string;
  is_default?: boolean;
  input_modalities?: string[];
}

export interface GenerationAgentCapabilities {
  available: boolean;
  interactive_user_input?: boolean;
  adapter: string;
  version: string;
  models: GenerationAgentModel[];
  diagnostic: {
    code?: string;
    message?: string;
    hint?: string;
    [key: string]: unknown;
  };
}

export interface GenerationConversationMessageResult {
  turn_id: string;
  status: string;
  next_sequence: number;
}

export interface GenerationConversationConfirmResult {
  conversation: GenerationConversation;
  plan: GenerationPlanRun;
  jobs: GenerationJobRun[];
  created_assets: Array<Record<string, unknown>>;
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
  credential_mode?: ProviderCredentialMode;
  model_discovery_mode?: ProviderModelDiscoveryMode;
  models_path?: string | null;
  is_active: boolean;
  models: ProviderModelRecord[];
  models_refreshed_at: string | null;
  models_sync?: ProviderModelsSync;
  model_catalog_api_version: number;
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
  purpose?: string;
  title?: string | null;
  context_hash: string;
  context_path: string | null;
  context: Record<string, unknown>;
  draft?: Record<string, unknown>;
  draft_hash?: string | null;
  draft_version?: number;
  sandbox: Record<string, unknown>;
  allowed_actions: string[];
  writable_allowlist: string[];
  budget_limit: number;
  budget_used: number;
  turn_count?: number;
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
