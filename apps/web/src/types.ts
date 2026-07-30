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

export type GenerationTaskKind = "text" | "image" | "image_edit";

export interface GenerationTaskInput {
  id: string;
  kind: GenerationTaskKind;
  asset_id: string;
  prompt: string;
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
  provider_profile_id: string;
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
  text_model: string;
  image_model: string;
  concurrency: number;
  pricing: Record<string, number> | null;
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
}
