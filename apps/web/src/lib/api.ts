import type {
  AssetRevision,
  AgentSessionRun,
  GameAsset,
  GenerationJobRun,
  GenerationPlanInput,
  GenerationPlanRun,
  GenerationConversation,
  GenerationAgentCapabilities,
  GenerationConversationConfirmResult,
  GenerationConversationEvent,
  GenerationConversationMessageResult,
  GenerationConversationSummary,
  GenerationPlanningDraft,
  GenerationProviderProfile,
  DeliveryRecord,
  ExportConfig,
  ExportPreview,
  JobSummary,
  ProjectSummary,
  ReleaseSummary,
  ProviderDefaults,
  ProviderCredentialMode,
  ProviderModelDiscoveryMode,
  ProviderModelModality,
  ProviderModelRecord,
  ProviderModelsSync,
  QACheck,
  RemediationRun,
  ReviewStatus,
  RunEventItem,
  RunInspection,
  WorkbenchPayload,
  NarrativeMap,
  NarrativeMaterializeResult,
} from "../types";
import type { ProviderProfile } from "../types";
import { assetSubtypeLabel, domainLabel } from "./labels";
import { normalizeModelCapability } from "./providerModels";

const API_ROOT =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ?? "/api";

const emptyProject: ProjectSummary = {
  id: "",
  name: "尚未登记项目",
  path: "",
  assetCount: 0,
  thumbnail: "",
};

const emptyJob: JobSummary = {
  id: "no-job",
  name: "暂无生成任务",
  status: "paused",
  progress: 0,
  completed: 0,
  total: 0,
  passed: 0,
  previewImages: [],
  outputPath: "",
};

export const emptyWorkbenchPayload: WorkbenchPayload = {
  project: emptyProject,
  assets: [],
  job: emptyJob,
};

export async function fetchNarrativeMap(projectId: string): Promise<NarrativeMap> {
  return request<NarrativeMap>(
    `/projects/${encodeURIComponent(projectId)}/narrative-map`,
    { timeoutMs: 15_000 },
  );
}

export async function saveNarrativeScene(
  sceneAssetId: string,
  content: Record<string, unknown>,
  parentRevisionId: string | null,
): Promise<{ id: string }> {
  return request<{ id: string }>("/revisions", {
    method: "POST",
    body: JSON.stringify({
      asset_id: sceneAssetId,
      format: "json",
      content,
      parent_revision_id: parentRevisionId,
    }),
  });
}

export async function materializeNarrativeRequirements(
  projectId: string,
  sceneAssetId: string,
  requirementIds: string[],
): Promise<NarrativeMaterializeResult> {
  return request<NarrativeMaterializeResult>(
    `/projects/${encodeURIComponent(projectId)}/narrative-map/scenes/${encodeURIComponent(sceneAssetId)}/requirements`,
    {
      method: "POST",
      body: JSON.stringify({ requirement_ids: requirementIds }),
    },
  );
}

export async function fetchReleases(projectId: string): Promise<ReleaseSummary[]> {
  return request<ReleaseSummary[]>(`/releases?project_id=${encodeURIComponent(projectId)}`);
}

export async function fetchExportConfig(projectId: string): Promise<ExportConfig> {
  return request<ExportConfig>(`/projects/${encodeURIComponent(projectId)}/export-config`);
}

export async function updateExportConfig(projectId: string, gameRoot: string | null): Promise<ExportConfig> {
  return request<ExportConfig>(`/projects/${encodeURIComponent(projectId)}/export-config`, {
    method: "PUT",
    body: JSON.stringify({ game_root: gameRoot }),
  });
}

export async function previewExport(
  projectId: string,
  releaseId: string,
  gameRoot?: string,
): Promise<ExportPreview> {
  return request<ExportPreview>("/exports/preview", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, release_id: releaseId, game_root: gameRoot }),
  });
}

export async function applyExport(
  projectId: string,
  releaseId: string,
  gameRoot?: string,
  runCommands = true,
): Promise<DeliveryRecord> {
  return request<DeliveryRecord>("/exports/apply", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, release_id: releaseId, game_root: gameRoot, run_commands: runCommands }),
  });
}

export async function verifyExport(
  projectId: string,
  releaseId?: string,
  gameRoot?: string,
  runCommands = false,
): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("/exports/verify", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, release_id: releaseId, game_root: gameRoot, run_commands: runCommands }),
  });
}

export async function rollbackExport(
  projectId: string,
  releaseId: string,
  gameRoot?: string,
  runCommands = true,
): Promise<DeliveryRecord> {
  return request<DeliveryRecord>("/exports/rollback", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, release_id: releaseId, game_root: gameRoot, run_commands: runCommands }),
  });
}

export async function fetchDeliveries(projectId: string): Promise<DeliveryRecord[]> {
  return request<DeliveryRecord[]>(`/deliveries?project_id=${encodeURIComponent(projectId)}`);
}

export class ApiError extends Error {
  readonly status: number;
  readonly category?: string;
  readonly retryAfter?: string;
  readonly endpoint?: string;
  readonly requestId?: string;
  readonly hint?: string;

  constructor(
    message: string,
    status: number,
    category?: string,
    retryAfter?: string,
    endpoint?: string,
    requestId?: string,
    hint?: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.category = category;
    this.retryAfter = retryAfter;
    this.endpoint = endpoint;
    this.requestId = requestId;
    this.hint = hint;
  }
}

interface RequestOptions extends RequestInit {
  timeoutMs?: number;
}

interface ApiList<T> {
  items?: T[];
  data?: T[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function request<T>(path: string, init: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const { timeoutMs = 4_500, ...fetchInit } = init;
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_ROOT}${path}`, {
      ...fetchInit,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(fetchInit.body ? { "Content-Type": "application/json" } : {}),
        ...fetchInit.headers,
      },
    });

    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      let category: string | undefined;
      let endpoint: string | undefined;
      let requestId: string | undefined;
      let hint: string | undefined;
      try {
        const payload = (await response.json()) as unknown;
        if (isRecord(payload)) {
          const detail = payload.detail;
          if (typeof detail === "string") message = detail;
          if (isRecord(detail)) {
            if (typeof detail.message === "string") message = detail.message;
            if (typeof detail.category === "string") category = detail.category;
            if (typeof detail.endpoint === "string") endpoint = detail.endpoint;
            if (typeof detail.request_id === "string") requestId = detail.request_id;
            if (typeof detail.hint === "string") hint = detail.hint;
          }
        }
      } catch {
        // Some OpenAI-compatible gateways return an empty or non-JSON error body.
      }
      throw new ApiError(
        message,
        response.status,
        category,
        response.headers.get("Retry-After") ?? undefined,
        endpoint,
        requestId ?? response.headers.get("x-request-id") ?? response.headers.get("request-id") ?? undefined,
        hint,
      );
    }

    if (response.status === 204) {
      return undefined as T;
    }

    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

function unpackList<T>(payload: T[] | ApiList<T>): T[] {
  if (Array.isArray(payload)) return payload;
  return payload.items ?? payload.data ?? [];
}

function normalizeProject(value: unknown): ProjectSummary {
  if (!isRecord(value)) {
    throw new ApiError("后端返回了无效的项目记录。", 502, "invalid_response");
  }
  return {
    id: String(value.id ?? ""),
    name: String(value.name ?? "未命名项目"),
    path: String(value.root_path ?? ""),
    branch: typeof value.branch === "string" ? value.branch : undefined,
    assetCount: Number(value.asset_count ?? 0),
    thumbnail: typeof value.thumbnail === "string" ? value.thumbnail : "",
  };
}

function normalizeAsset(value: unknown): GameAsset {
  if (!isRecord(value)) {
    throw new ApiError("后端返回了无效的资产记录。", 502, "invalid_response");
  }

  const id = String(value.id ?? "");
  const key = String(value.key ?? "");
  if (!id || !key) {
    throw new ApiError("资产记录缺少 id 或稳定 key。", 502, "invalid_response");
  }

  const contentStatus = String(value.content_status ?? "candidate");
  const generationStatus = String(value.generation_status ?? "idle");
  const status = String(
    value.review_status ??
      (generationStatus === "running" || generationStatus === "queued"
        ? "generating"
        : contentStatus === "approved"
          ? "approved"
          : contentStatus === "rejected"
            ? "rejected"
            : "pending"),
  );
  const normalizedStatus: ReviewStatus = ["pending", "approved", "rejected", "generating"].includes(status)
    ? (status as ReviewStatus)
    : "pending";

  const rawKind = String(value.kind ?? "");
  if (!["content", "design", "entity", "media", "production"].includes(rawKind)) {
    throw new ApiError(`资产 ${key} 的核心类型无效。`, 502, "invalid_response");
  }
  const kind = ["content", "design", "entity", "media", "production"].includes(rawKind)
    ? (rawKind as GameAsset["kind"])
    : "media";
  const subtype = String(value.subtype ?? "未分类");
  // The API contract calls this field `metadata`; older fixtures used
  // `asset_metadata`, so accept both while keeping the normalized shape
  // stable for the workbench.
  const metadata = isRecord(value.metadata)
    ? value.metadata
    : isRecord(value.asset_metadata)
      ? value.asset_metadata
      : {};
  const subtypeKey = subtype.toLocaleLowerCase();
  const keyLower = key.toLocaleLowerCase();
  const isVisualAnchor = kind === "production" && subtypeKey === "visual_anchor";
  const revisionFormat: GameAsset["revisionFormat"] =
    kind === "media" || isVisualAnchor
      ? "media"
      : subtypeKey === "style_bible"
        ? "markdown"
        : "json";
  const category =
    kind === "content"
      ? "content"
      : kind === "design"
        ? "design"
        : kind === "production"
          ? "production"
          : kind === "entity"
            ? subtypeKey.includes("character") || subtype.includes("角色") || subtype.includes("人物")
              ? "characters"
              : subtypeKey.includes("achievement") || subtype.includes("成就")
                ? "achievements"
              : subtypeKey.includes("location") || subtype.includes("地点") || subtype.includes("场景")
                ? "locations"
                : "items"
          : subtypeKey.includes("audio") || subtype.includes("音频")
              ? "audio"
              : keyLower.startsWith("portrait.") || subtypeKey.includes("portrait") || subtype.includes("立绘")
                ? "portraits"
                : keyLower.startsWith("background.") || subtypeKey.includes("background") || subtype.includes("背景")
                  ? "backgrounds"
                  : keyLower.startsWith("cg.") || subtypeKey === "cg" || subtype.includes("CG")
                    ? "cgs"
                    : keyLower.startsWith("icon.") || subtypeKey.includes("icon") || subtype.includes("图标")
                      ? "icons"
                      : keyLower.startsWith("ending.") || subtypeKey.includes("ending") || subtype.includes("结局")
                        ? "ending-illustrations"
                        : "media-other";
  const approvedRevisionId =
    typeof value.current_revision_id === "string" ? value.current_revision_id : undefined;
  const candidateRevisionId =
    typeof value.latest_candidate_revision_id === "string"
      ? value.latest_candidate_revision_id
      : undefined;
  const reviewStatus: ReviewStatus =
    candidateRevisionId && normalizedStatus !== "generating" ? "pending" : normalizedStatus;
  const rawUpdatedAt = String(value.updated_at ?? "");
  const timeAccuracy = metadata.time_accuracy === "unknown" ? "unknown" : "known";
  const productionStage = String(
    metadata.production_stage ?? (reviewStatus === "approved" ? "approved" : "imported"),
  );
  // Publication is a separate lifecycle from candidate review. Keep the
  // backend value available to workspace filters (notably “已发布”) instead
  // of inferring it from reviewStatus, which would incorrectly include
  // approved-but-never-released assets.
  const publicationStatus = String(
    value.publication_status ?? metadata.publication_status ?? "unpublished",
  );
  // Older/local projects do not have a first-class author column. Accept the
  // metadata variants when present, but leave this undefined rather than
  // pretending every indexed asset was created by the current user.
  const createdByValue = value.created_by ?? metadata.created_by ?? metadata.author;
  const createdBy = typeof createdByValue === "string" && createdByValue.trim()
    ? createdByValue.trim()
    : undefined;
  const summary = String(metadata.preview_summary ?? "");
  const domain = typeof metadata.domain === "string" ? metadata.domain : "";
  const preview =
    kind === "content" || kind === "design"
      ? {
          kind: "content" as const,
          label: subtype === "story_arc" ? "故事弧" : subtype === "event" ? "事件" : subtype === "memorial" ? "奏折" : subtype === "ending" ? "结局" : "内容",
          meta: domain ? domainLabel(domain) : "结构化内容",
          summary: summary || "暂无摘要",
        }
      : kind === "production"
        ? {
            kind: "placeholder" as const,
            label: subtypeKey === "prompt_recipe" ? "Prompt 配方" : subtypeKey === "visual_anchor" ? "视觉锚点" : "项目规范",
            detail: "结构化文档，不是媒体文件",
          }
      : {
          kind: "placeholder" as const,
          label: kind === "entity"
            ? (subtype === "character" ? "角色实体" : subtype === "location" ? "地点实体" : subtype === "achievement" ? "成就实体" : "物品实体")
            : "媒体资产",
          detail: kind === "entity" ? "等待关联媒体预览" : "暂无可用预览",
        };

  return {
    id,
    candidateRevisionId,
    approvedRevisionId,
    reviewReady: false,
    detailsLoaded: false,
    reviewBlockReason: "正在加载真实候选修订与 QA 证据。",
    key,
    schemaRef: typeof value.schema_ref === "string" ? value.schema_ref : undefined,
    name: String(value.title ?? key),
    kind,
    category,
    subtype,
    subtypeLabel: assetSubtypeLabel(kind, subtype),
    tags: Array.isArray(value.tags) ? value.tags.map(String) : [],
    reviewStatus,
    publicationStatus,
    createdBy,
    productionStage,
    timeAccuracy,
    preview,
    updatedAt: timeAccuracy === "unknown" ? "" : rawUpdatedAt,
    updatedLabel: timeAccuracy === "unknown" ? "历史时间未知" : rawUpdatedAt || "—",
    updatedBy: String(value.updated_by ?? (timeAccuracy === "unknown" ? "历史导入" : "本机索引")),
    thumbnails: [],
    linkedMedia: [],
    relatedAssets: [],
    revisionFormat,
    revisionContent: null,
    revisions: [],
    prompt: "",
    negativePrompt: "",
    model: "unknown",
    recipe: "unknown",
    seed: "unknown",
    qaPassed: 0,
    qaTotal: 0,
    qa: [],
    sourcePath: typeof metadata.source_path === "string" ? metadata.source_path : undefined,
    sourceMissing: metadata.source_missing === true,
    sourceDriftStatus: typeof metadata.source_drift_status === "string"
      ? metadata.source_drift_status as GameAsset["sourceDriftStatus"]
      : undefined,
    recipeId: typeof metadata.recipe_id === "string" ? metadata.recipe_id : undefined,
  };
}

function normalizeJob(value: unknown): JobSummary {
  if (!isRecord(value)) {
    return emptyJob;
  }
  const rawStatus = String(value.status ?? "paused");
  const activeStatuses = new Set([
    "running",
    "output_received",
    "hard_qa",
    "semantic_qa",
    "remediating",
  ]);
  const status = [
    "queued",
    "completed",
    "paused",
    "awaiting_user",
    "credentials_locked",
    "qa_failed",
  ].includes(rawStatus)
    ? rawStatus
    : ["succeeded", "candidate_ready"].includes(rawStatus)
      ? "completed"
      : activeStatuses.has(rawStatus)
        ? "running"
      : "paused";
  const rawProgress = Number(value.progress ?? 0);
  const progress = rawProgress <= 1 ? Math.round(rawProgress * 100) : Math.round(rawProgress);
  return {
    id: String(value.id ?? "job"),
    planId: typeof value.plan_id === "string" ? value.plan_id : undefined,
    taskId: typeof value.task_id === "string" ? value.task_id : undefined,
    name: String(value.name ?? value.task_id ?? "生成任务"),
    progress,
    completed: Number(value.completed ?? (status === "completed" ? 1 : 0)),
    total: Number(value.total ?? 1),
    passed: Number(value.passed ?? (status === "completed" && value.result_revision_id ? 1 : 0)),
    status: status as JobSummary["status"],
    previewImages: Array.isArray(value.preview_images) ? value.preview_images.map(String) : [],
    outputPath: String(value.output_path ?? ""),
  };
}

interface RevisionApiRecord {
  id: string;
  asset_id: string;
  parent_revision_id?: string | null;
  sequence: number;
  format: "json" | "markdown" | "media";
  content: unknown;
  prompt_recipe?: string | null;
  provider_snapshot?: Record<string, unknown>;
  review_status?: string;
  created_at: string;
}

interface RenditionApiRecord {
  id: string;
  revision_id: string;
  media_type: string;
  width?: number | null;
  height?: number | null;
  byte_size: number;
}

interface QARunApiRecord {
  id: string;
  rendition_id: string;
  verdict: string;
  checks: Array<Record<string, unknown>>;
  created_at: string;
}

function textField(value: unknown, ...keys: string[]): string {
  if (!isRecord(value)) return "";
  for (const key of keys) {
    if (typeof value[key] === "string") return value[key] as string;
  }
  return "";
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function qaCheckLabel(name: string): string {
  const labels: Record<string, string> = {
    decodable: "文件可解码",
    width: "宽度符合规格",
    height: "高度符合规格",
    alpha: "透明通道",
    transparentCorners: "透明角",
    subjectCoverage: "主体覆盖率",
    residualColorKey: "残余色键",
    fileSize: "文件体积",
  };
  return labels[name] ?? name;
}

function normalizeChecks(runs: QARunApiRecord[]): QACheck[] {
  const latestByRendition = new Map<string, QARunApiRecord>();
  for (const run of runs) {
    if (!latestByRendition.has(run.rendition_id)) latestByRendition.set(run.rendition_id, run);
  }
  return Array.from(latestByRendition.values()).flatMap((run) =>
    run.checks.map((check, index) => {
      const name = String(check.name ?? `check-${index + 1}`);
      const value = check.message ?? check.value ?? "—";
      return {
        id: `${run.rendition_id}:${name}`,
        label: qaCheckLabel(name),
        result: typeof value === "string" ? value : JSON.stringify(value),
        passed: check.passed === true,
      };
    }),
  );
}

function renditionUrl(renditionId: string): string {
  return `${API_ROOT}/renditions/${encodeURIComponent(renditionId)}/content`;
}

function normalizeRevision(
  revision: RevisionApiRecord,
  rendition: RenditionApiRecord | undefined,
  status: AssetRevision["status"],
): AssetRevision {
  const sequence = String(revision.sequence).padStart(2, "0");
  const suffix = status === "candidate" ? "候选" : status === "approved" ? "已批准" : status === "superseded" ? "已被替代" : "已驳回";
  return {
    id: revision.id,
    parentRevisionId: revision.parent_revision_id,
    sequence: revision.sequence,
    label: `r${sequence}（${suffix}）`,
    image: rendition ? renditionUrl(rendition.id) : "",
    format: revision.format,
    content: revision.content,
    createdAt: revision.created_at || "历史时间未知",
    author: "本机用户",
    resolution:
      rendition?.width && rendition?.height ? `${rendition.width} × ${rendition.height}` : "—",
    fileSize: rendition ? formatBytes(rendition.byte_size) : "—",
    status,
  };
}

async function mapWithConcurrency<T, R>(
  values: T[],
  concurrency: number,
  mapper: (value: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(values.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(concurrency, values.length) }, async () => {
    while (cursor < values.length) {
      const index = cursor++;
      results[index] = await mapper(values[index]);
    }
  });
  await Promise.all(workers);
  return results;
}

export async function fetchAssetDetails(asset: GameAsset, requestedRevisionId?: string | null): Promise<GameAsset> {
  const revisions = await request<RevisionApiRecord[]>(
    `/revisions?asset_id=${encodeURIComponent(asset.id)}`,
  );
  const candidate = asset.candidateRevisionId
    ? revisions.find((revision) => revision.id === asset.candidateRevisionId)
    : undefined;
  const approved = asset.approvedRevisionId
    ? revisions.find((revision) => revision.id === asset.approvedRevisionId)
    : undefined;
  const requested = requestedRevisionId
    ? revisions.find((revision) => revision.id === requestedRevisionId)
    : undefined;
  const relevant = Array.from(new Map(
    [candidate, approved, requested]
      .filter((revision): revision is RevisionApiRecord => Boolean(revision))
      .map((revision) => [revision.id, revision]),
  ).values());
  const renditionLists = await Promise.all(
    relevant.map((revision) =>
      request<RenditionApiRecord[]>(
        `/revisions/${encodeURIComponent(revision.id)}/renditions`,
      ),
    ),
  );
  const renditions = renditionLists.flat();
  const candidateRenditions = candidate
    ? renditions.filter((rendition) => rendition.revision_id === candidate.id)
    : [];
  const qaRuns = (
    await Promise.all(
      candidateRenditions.map((rendition) =>
        request<QARunApiRecord[]>(`/qa-runs?rendition_id=${encodeURIComponent(rendition.id)}`),
      ),
    )
  ).flat();
  const checks = normalizeChecks(qaRuns);
  const isMedia = asset.revisionFormat === "media";
  const qaReady =
    !isMedia ||
    (candidateRenditions.length > 0 &&
      candidateRenditions.every((rendition) =>
        qaRuns.some((run) => run.rendition_id === rendition.id && run.verdict !== "fail"),
      ));
  const reviewReady =
    Boolean(candidate) && qaReady && asset.reviewStatus !== "rejected";
  const candidateRendition = candidateRenditions[0];
  const approvedRendition = approved
    ? renditions.find((rendition) => rendition.revision_id === approved.id)
    : undefined;
  const revisionViews: AssetRevision[] = [];
  if (candidate) revisionViews.push(normalizeRevision(candidate, candidateRendition, "candidate"));
  if (approved) revisionViews.push(normalizeRevision(approved, approvedRendition, "approved"));
  for (const revision of revisions) {
    if (revision.id === candidate?.id || revision.id === approved?.id) continue;
    const status = revision.review_status === "rejected"
      ? "rejected"
      : revision.review_status === "superseded"
        ? "superseded"
        : "approved";
    revisionViews.push(
      normalizeRevision(
        revision,
        renditions.find((rendition) => rendition.revision_id === revision.id),
        status,
      ),
    );
  }
  const content = candidate?.content ?? approved?.content;
  const provider = candidate?.provider_snapshot ?? approved?.provider_snapshot ?? {};
  const thumbnails = [candidateRendition, approvedRendition]
    .filter((rendition): rendition is RenditionApiRecord => Boolean(rendition))
    .map((rendition) => renditionUrl(rendition.id));

  return {
    ...asset,
    detailsLoaded: true,
    reviewReady,
    reviewBlockReason: !candidate
      ? "当前没有待审候选修订。"
      : !qaReady
        ? "候选媒体尚未具备完整且通过的硬 QA 证据。"
        : asset.reviewStatus === "rejected"
          ? "该候选已驳回，请先生成新的不可变候选。"
          : undefined,
    revisions: revisionViews,
    revisionFormat: (candidate?.format ?? approved?.format ?? asset.revisionFormat),
    revisionContent: content ?? null,
    thumbnails,
    preview: thumbnails.length > 0
      ? { kind: "image", images: thumbnails, label: asset.kind === "production" ? "视觉锚点" : "媒体预览" }
      : asset.preview,
    qa: checks,
    qaPassed: checks.filter((check) => check.passed).length,
    qaTotal: checks.length,
    prompt: textField(content, "prompt", "positivePrompt", "positive_prompt"),
    negativePrompt: textField(content, "negativePrompt", "negative_prompt"),
    model: String(provider.model ?? provider.image_model ?? "unknown"),
    recipe: String(candidate?.prompt_recipe ?? approved?.prompt_recipe ?? "unknown"),
    seed: String(provider.seed ?? "unknown"),
    updatedAt: asset.timeAccuracy === "unknown" ? "" : candidate?.created_at ?? approved?.created_at ?? asset.updatedAt,
    updatedLabel: asset.timeAccuracy === "unknown" ? "历史时间未知" : candidate?.created_at ?? approved?.created_at ?? asset.updatedLabel,
  };
}

export async function createAssetRevision(asset: GameAsset, content: unknown): Promise<GameAsset> {
  const created = await request<RevisionApiRecord>("/revisions", {
    method: "POST",
    body: JSON.stringify({
      asset_id: asset.id,
      format: asset.revisionFormat,
      content,
      parent_revision_id: asset.candidateRevisionId ?? asset.approvedRevisionId ?? null,
    }),
  });
  return fetchAssetDetails({
    ...asset,
    candidateRevisionId: created.id,
    reviewStatus: "pending",
    detailsLoaded: false,
  });
}

interface RelationApiRecord {
  source_asset_id: string;
  target_asset_id: string;
  relation_type: string;
}

export async function fetchWorkbench(): Promise<WorkbenchPayload> {
  const projectResult = await request<ProjectSummary[] | ApiList<ProjectSummary>>("/projects");
  const projects = unpackList(projectResult);
  if (projects.length === 0) {
    return emptyWorkbenchPayload;
  }
  const project = normalizeProject(projects[0]);
  // The API indexes discovered Projects during startup. Reading the workbench
  // must not launch another full iCloud scan on every mount or query refetch.
  const [assetResult, jobResult, relations] = await Promise.all([
    request<GameAsset[] | ApiList<GameAsset>>(`/assets?project_id=${encodeURIComponent(project.id)}`),
    request<JobSummary[] | ApiList<JobSummary>>(`/jobs?project_id=${encodeURIComponent(project.id)}`),
    request<RelationApiRecord[]>(`/relations?project_id=${encodeURIComponent(project.id)}`),
  ]);

  const assets = unpackList(assetResult).map(normalizeAsset);
  const hydratedAssets = await mapWithConcurrency(assets, 8, async (asset) => {
    if (!asset.candidateRevisionId && !asset.approvedRevisionId) {
      return {
        ...asset,
        detailsLoaded: true,
        reviewBlockReason: "当前资产还没有候选或已批准修订。",
      };
    }
    return asset.revisionFormat === "media" || (
      asset.kind === "design" ||
      asset.subtype === "style_bible" ||
      asset.subtype === "prompt_recipe"
    )
      ? fetchAssetDetails(asset)
      : asset;
  });
  const byId = new Map(hydratedAssets.map((asset) => [asset.id, asset]));
  const linkedMedia = new Map<string, GameAsset["linkedMedia"]>();
  const relatedAssets = new Map<string, GameAsset["relatedAssets"]>();
  for (const relation of relations) {
    const sourceAsset = byId.get(relation.source_asset_id);
    const targetAsset = byId.get(relation.target_asset_id);
    if (!sourceAsset || !targetAsset) continue;
    const outgoing = relatedAssets.get(sourceAsset.id) ?? [];
    outgoing.push({
      assetId: targetAsset.id,
      key: targetAsset.key,
      name: targetAsset.name,
      kind: targetAsset.kind,
      subtype: targetAsset.subtype,
      relationType: relation.relation_type,
      direction: "outgoing",
    });
    relatedAssets.set(sourceAsset.id, outgoing);
    const incoming = relatedAssets.get(targetAsset.id) ?? [];
    incoming.push({
      assetId: sourceAsset.id,
      key: sourceAsset.key,
      name: sourceAsset.name,
      kind: sourceAsset.kind,
      subtype: sourceAsset.subtype,
      relationType: relation.relation_type,
      direction: "incoming",
    });
    relatedAssets.set(targetAsset.id, incoming);

    if (!['depicts', 'represents'].includes(relation.relation_type) || sourceAsset.thumbnails.length === 0) continue;
    const current = linkedMedia.get(targetAsset.id) ?? [];
    current.push({
      assetId: sourceAsset.id,
      key: sourceAsset.key,
      name: sourceAsset.name,
      subtype: sourceAsset.subtype,
      images: sourceAsset.thumbnails,
    });
    linkedMedia.set(targetAsset.id, current);
  }
  const assetsWithLinkedPreviews = hydratedAssets.map((asset) => {
    const linked = [...(linkedMedia.get(asset.id) ?? [])]
      .sort((left, right) => Number(!left.key.endsWith('.neutral')) - Number(!right.key.endsWith('.neutral')) || left.key.localeCompare(right.key));
    const related = [...(relatedAssets.get(asset.id) ?? [])]
      .sort((left, right) => left.relationType.localeCompare(right.relationType) || left.key.localeCompare(right.key));
    if (!linked.length) return { ...asset, relatedAssets: related };
    const images = linked
      .flatMap((entry) => entry.images)
      .filter((image, index, values) => values.indexOf(image) === index)
      .slice(0, 3);
    return {
      ...asset,
      thumbnails: images,
      linkedMedia: linked,
      relatedAssets: related,
      preview: { kind: "image" as const, images, label: asset.subtype === "character" ? "关联立绘" : "关联图标" },
    };
  });
  const jobs = unpackList(jobResult);

  return {
    project: { ...project, assetCount: assetsWithLinkedPreviews.length },
    assets: assetsWithLinkedPreviews,
    job: jobs.length > 0 ? normalizeJob(jobs[0]) : normalizeJob(undefined),
  };
}

export interface ReviewBatchResult {
  succeeded: string[];
  failed: Array<{ revisionId: string; message: string }>;
}

export async function submitReview(
  revisionIds: string[],
  decision: "approve" | "reject",
): Promise<ReviewBatchResult> {
  const settled = await Promise.allSettled(
    revisionIds.map((revisionId) =>
      request<Record<string, unknown>>("/reviews", {
        method: "POST",
        body: JSON.stringify({ revision_id: revisionId, verdict: decision }),
      }),
    ),
  );
  const result: ReviewBatchResult = { succeeded: [], failed: [] };
  settled.forEach((entry, index) => {
    const revisionId = revisionIds[index];
    if (entry.status === "fulfilled") result.succeeded.push(revisionId);
    else {
      result.failed.push({
        revisionId,
        message: entry.reason instanceof Error ? entry.reason.message : "审核请求失败",
      });
    }
  });
  return result;
}

export async function unlockProvider(profileId: string, apiKey: string) {
  return request<{ detail?: string }>(`/providers/${encodeURIComponent(profileId)}/unlock`, {
    method: "POST",
    body: JSON.stringify({ api_key: apiKey }),
  });
}

export async function lockProvider(profileId: string) {
  return request<{ detail?: string }>(`/providers/${encodeURIComponent(profileId)}/lock`, {
    method: "POST",
  });
}

interface ProviderApiRecord {
  id: string;
  name: string;
  base_url: string;
  text_model: string;
  image_model: string;
  quality: string;
  concurrency: number;
  max_retries: number;
  allow_private_network: boolean;
  credential_mode?: ProviderCredentialMode;
  model_discovery_mode?: ProviderModelDiscoveryMode;
  models_path?: string | null;
  is_active?: boolean;
  models?: ProviderModelRecord[];
  models_refreshed_at?: string | null;
  models_sync?: Partial<ProviderModelsSync>;
  model_catalog_api_version?: number;
  is_unlocked?: boolean;
}

export interface ProviderModelsResponse {
  provider_profile_id: string;
  models: ProviderModelRecord[];
  refreshed_at: string | null;
  new_model_ids?: string[];
  cleared_default_routes: ProviderModelModality[];
  model_catalog_api_version: number;
  models_sync?: ProviderModelsSync;
}

function providerPayload(profile: ProviderProfile) {
  return {
    name: profile.name,
    kind: "openai_compatible",
    base_url: profile.baseUrl,
    allow_private_network: profile.allowPrivateNetwork,
    credential_mode: profile.credentialMode,
    model_discovery_mode: profile.modelDiscoveryMode,
    models_path: profile.modelsPath.trim() || null,
  };
}

function normalizeModelsSync(value: unknown): ProviderModelsSync {
  const record = isRecord(value) ? value : {};
  const rawState = String(record.state ?? "never");
  const state: ProviderModelsSync["state"] = ["never", "synced", "empty", "manual_required", "error"].includes(rawState)
    ? (rawState as ProviderModelsSync["state"])
    : "never";
  return {
    state,
    checked_at: typeof record.checked_at === "string" ? record.checked_at : null,
    endpoint: typeof record.endpoint === "string" ? record.endpoint : null,
    status_code: typeof record.status_code === "number" ? record.status_code : null,
    message: typeof record.message === "string" ? record.message : null,
    hint: typeof record.hint === "string" ? record.hint : null,
    request_id: typeof record.request_id === "string" ? record.request_id : null,
  };
}

function normalizeProviderModels(value: unknown): ProviderModelRecord[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((model) => {
    if (!isRecord(model) || !model.id) return [];
    const id = String(model.id);
    return [{
      id,
      modalities: [normalizeModelCapability(id, Array.isArray(model.modalities) ? model.modalities : [])],
      classification: model.classification === "provider" || model.classification === "heuristic" || model.classification === "manual"
        ? model.classification
        : "unknown",
      available: model.available !== false,
      enabled: model.enabled !== false,
    }];
  });
}

function normalizeProviderModelsResponse(
  value: unknown,
  fallbackProviderId: string,
): ProviderModelsResponse {
  const record = isRecord(value) ? value : {};
  const clearedDefaultRoutes = Array.isArray(record.cleared_default_routes)
    ? record.cleared_default_routes.filter(
        (item): item is ProviderModelModality =>
          item === "text" || item === "image" || item === "video" || item === "audio",
      )
    : [];
  const rawVersion = Number(record.model_catalog_api_version ?? 1);
  return {
    provider_profile_id: String(record.provider_profile_id ?? fallbackProviderId),
    models: normalizeProviderModels(record.models),
    refreshed_at: typeof record.refreshed_at === "string" ? record.refreshed_at : null,
    new_model_ids: Array.isArray(record.new_model_ids)
      ? record.new_model_ids.map(String)
      : [],
    cleared_default_routes: clearedDefaultRoutes,
    model_catalog_api_version: Number.isFinite(rawVersion) ? rawVersion : 1,
    models_sync: normalizeModelsSync(record.models_sync),
  };
}

function normalizeGenerationProvider(profile: Record<string, unknown>): GenerationProviderProfile {
  const quality = String(profile.quality ?? "high");
  const rawCatalogVersion = Number(profile.model_catalog_api_version ?? 1);
  return {
    id: String(profile.id ?? ""),
    name: String(profile.name ?? "未命名供应商"),
    kind: String(profile.kind ?? ""),
    base_url: String(profile.base_url ?? ""),
    text_model: String(profile.text_model ?? ""),
    image_model: String(profile.image_model ?? ""),
    quality: quality === "low" || quality === "medium" ? quality : "high",
    concurrency: Number(profile.concurrency ?? 1),
    max_retries: Number(profile.max_retries ?? 0),
    allow_private_network: profile.allow_private_network === true,
    credential_mode: profile.credential_mode === "optional" || profile.credential_mode === "none"
      ? profile.credential_mode
      : "required",
    model_discovery_mode: profile.model_discovery_mode === "manual" ? "manual" : "auto",
    models_path: typeof profile.models_path === "string" ? profile.models_path : null,
    is_active: profile.is_active !== false,
    models: normalizeProviderModels(profile.models),
    models_refreshed_at: typeof profile.models_refreshed_at === "string" ? profile.models_refreshed_at : null,
    models_sync: normalizeModelsSync(profile.models_sync),
    model_catalog_api_version: Number.isFinite(rawCatalogVersion) ? rawCatalogVersion : 1,
    is_unlocked: profile.is_unlocked === true,
  };
}

export async function ensureProviderProfile(profile: ProviderProfile): Promise<ProviderApiRecord> {
  const profiles = await request<ProviderApiRecord[]>("/providers");
  const existing = profiles.find(
    (item) =>
      (item.id === profile.id || item.name === profile.name) &&
      item.base_url === profile.baseUrl &&
      item.allow_private_network === profile.allowPrivateNetwork &&
      item.credential_mode === profile.credentialMode &&
      item.model_discovery_mode === profile.modelDiscoveryMode &&
      (item.models_path ?? "models") === (profile.modelsPath.trim() || "models"),
  );
  if (existing) return existing;

  return request<ProviderApiRecord>("/providers", {
    method: "POST",
    body: JSON.stringify(providerPayload(profile)),
  });
}

export async function createProviderProfile(profile: ProviderProfile): Promise<GenerationProviderProfile> {
  const created = await request<Record<string, unknown>>("/providers", {
    method: "POST",
    body: JSON.stringify(providerPayload(profile)),
  });
  return normalizeGenerationProvider(created);
}

export async function updateProviderProfile(
  profileId: string,
  profile: ProviderProfile,
): Promise<GenerationProviderProfile> {
  const updated = await request<Record<string, unknown>>(
    `/providers/${encodeURIComponent(profileId)}`,
    { method: "PATCH", body: JSON.stringify(providerPayload(profile)) },
  );
  return normalizeGenerationProvider(updated);
}

export async function archiveProvider(profileId: string): Promise<GenerationProviderProfile> {
  const result = await request<Record<string, unknown>>(
    `/providers/${encodeURIComponent(profileId)}/archive`,
    { method: "POST" },
  );
  return normalizeGenerationProvider(result);
}

export async function restoreProvider(profileId: string): Promise<GenerationProviderProfile> {
  const result = await request<Record<string, unknown>>(
    `/providers/${encodeURIComponent(profileId)}/restore`,
    { method: "POST" },
  );
  return normalizeGenerationProvider(result);
}

export async function fetchProviderDefaults(): Promise<ProviderDefaults> {
  const value = await request<Partial<ProviderDefaults>>("/provider-defaults");
  return {
    text: value.text ?? null,
    image: value.image ?? null,
    video: value.video ?? null,
    audio: value.audio ?? null,
    max_concurrency: Number(value.max_concurrency ?? 3),
    max_transport_retries: Number(value.max_transport_retries ?? 2),
    updated_at: value.updated_at ?? null,
  };
}

export async function updateProviderDefaults(defaults: ProviderDefaults): Promise<ProviderDefaults> {
  const value = await request<Partial<ProviderDefaults>>("/provider-defaults", {
    method: "PUT",
    body: JSON.stringify({
      text: defaults.text,
      image: defaults.image,
      video: defaults.video,
      audio: defaults.audio,
      max_concurrency: defaults.max_concurrency,
      max_transport_retries: defaults.max_transport_retries,
    }),
  });
  return {
    text: value.text ?? null,
    image: value.image ?? null,
    video: value.video ?? null,
    audio: value.audio ?? null,
    max_concurrency: Number(value.max_concurrency ?? defaults.max_concurrency),
    max_transport_retries: Number(value.max_transport_retries ?? defaults.max_transport_retries),
    updated_at: value.updated_at ?? null,
  };
}

export interface DirectProviderProfile {
  baseUrl: string;
  modelsPath?: string | null;
  credentialMode?: ProviderCredentialMode;
}

export interface DirectProviderModels {
  endpoint: string;
  statusCode: number;
  requestId: string | null;
  models: Array<Record<string, unknown>>;
}

export class ProviderDirectError extends Error {
  readonly status: number | null;
  readonly category: string;
  readonly endpoint: string;
  readonly requestId?: string;
  readonly hint?: string;

  constructor(
    message: string,
    endpoint: string,
    category: string,
    status: number | null = null,
    requestId?: string,
    hint?: string,
  ) {
    super(message);
    this.name = "ProviderDirectError";
    this.status = status;
    this.category = category;
    this.endpoint = endpoint;
    this.requestId = requestId;
    this.hint = hint;
  }
}

function directModelsEndpoint(profile: DirectProviderProfile): string {
  const base = new URL(profile.baseUrl);
  const path = (profile.modelsPath?.trim() || "models").replace(/^\/+/, "");
  if (!path || path.includes("\\") || path.includes("?") || path.includes("#") || path.includes("://")) {
    throw new ProviderDirectError(
      "模型列表路径无效。",
      profile.baseUrl,
      "validation",
      422,
      undefined,
      "请输入不带协议、查询参数或片段的相对路径，例如 models。",
    );
  }
  const segments = path.split("/");
  if (segments.some((segment) => segment === "." || segment === "..")) {
    throw new ProviderDirectError(
      "模型列表路径无效。",
      profile.baseUrl,
      "validation",
      422,
      undefined,
      "请输入不带协议、查询参数或片段的相对路径，例如 models。",
    );
  }
  base.search = "";
  base.hash = "";
  return `${base.toString().replace(/\/$/, "")}/${path}`;
}

function redactProviderMessage(value: string, apiKey: string): string {
  return value
    .replaceAll(apiKey, "[redacted]")
    .replace(/Bearer\s+[^\s,;]+/gi, "Bearer [redacted]")
    .slice(0, 320)
    .trim();
}

function directResponseMessage(payload: unknown, apiKey: string): string | undefined {
  if (!isRecord(payload)) return undefined;
  const error = payload.error;
  const candidates: unknown[] = [];
  if (isRecord(error)) {
    candidates.push(error.message, error.detail, error.code);
  } else if (typeof error === "string") {
    candidates.push(error);
  }
  candidates.push(payload.message, payload.detail);
  const message = candidates.find(
    (value): value is string => typeof value === "string" && value.trim().length > 0,
  );
  return message ? redactProviderMessage(message, apiKey) : undefined;
}

function directProviderHint(status: number): string {
  if (status === 401 || status === 403) return "检查 API Key 和供应商权限。";
  if (status === 404 || status === 405) return "连接仍可使用；请在模型选择器中手动登记模型 ID。";
  if (status === 429) return "稍后重试，或检查供应商的额度和限流策略。";
  if (status >= 500) return "检查供应商状态页，稍后重试。";
  return "检查 Base URL、模型列表路径和供应商接口文档。";
}

function directModelItems(payload: unknown, endpoint: string): Array<Record<string, unknown>> {
  let rawItems: unknown[] | undefined;
  if (Array.isArray(payload)) rawItems = payload;
  else if (isRecord(payload)) {
    for (const key of ["data", "models", "items"]) {
      if (Array.isArray(payload[key])) {
        rawItems = payload[key] as unknown[];
        break;
      }
    }
  }
  if (!rawItems) {
    throw new ProviderDirectError(
      "供应商返回的模型列表格式无法识别。",
      endpoint,
      "invalid_response",
      200,
      undefined,
      "供应商应返回数组，或包含 data、models、items 数组的 JSON。",
    );
  }

  const items: Array<Record<string, unknown>> = [];
  for (const raw of rawItems) {
    if (typeof raw === "string" && raw.trim()) {
      items.push({ id: raw.trim() });
      continue;
    }
    if (!isRecord(raw)) continue;
    const modelId = ["id", "name", "model", "model_id"].find(
      (key) => typeof raw[key] === "string" && String(raw[key]).trim(),
    );
    if (!modelId) continue;
    const item: Record<string, unknown> = { id: String(raw[modelId]).trim() };
    for (const key of ["modalities", "capabilities", "input_modalities", "output_modalities"]) {
      const value = raw[key];
      if (typeof value === "string" || Array.isArray(value) || isRecord(value)) item[key] = value;
    }
    items.push(item);
  }
  if (rawItems.length > 0 && items.length === 0) {
    throw new ProviderDirectError(
      "供应商返回的模型列表中没有可识别的模型 ID。",
      endpoint,
      "invalid_response",
      200,
      undefined,
      "请确认模型项包含 id、name、model 或 model_id 字段。",
    );
  }
  return items;
}

export async function fetchProviderModelsDirect(
  profile: DirectProviderProfile,
  apiKey = "",
): Promise<DirectProviderModels> {
  const endpoint = directModelsEndpoint(profile);
  const secret = apiKey.trim();
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 120_000);
  try {
    let response: Response;
    try {
      response = await fetch(endpoint, {
        method: "GET",
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...(secret ? { Authorization: `Bearer ${secret}` } : {}),
        },
      });
    } catch (error) {
      const aborted = error instanceof DOMException && error.name === "AbortError";
      throw new ProviderDirectError(
        aborted ? "供应商模型列表请求超时。" : "无法直接连接供应商模型列表接口。",
        endpoint,
        "network",
        null,
        undefined,
        aborted
          ? "检查 Base URL 和供应商响应时间。"
          : "检查供应商是否允许浏览器跨域访问（CORS）、Base URL 和本机网络。",
      );
    }

    const requestId = response.headers.get("x-request-id")
      ?? response.headers.get("request-id")
      ?? response.headers.get("cf-ray");
    if (!response.ok) {
      let payload: unknown = null;
      try {
        payload = await response.json();
      } catch {
        // Keep arbitrary gateway bodies out of the UI and error logs.
      }
      const responseMessage = directResponseMessage(payload, secret);
      const message = responseMessage || `供应商返回 HTTP ${response.status}`;
      throw new ProviderDirectError(
        message,
        endpoint,
        response.status === 401 || response.status === 403 ? "auth" : response.status >= 500 ? "server" : response.status === 404 || response.status === 405 ? "validation" : "unknown",
        response.status,
        requestId ?? undefined,
        directProviderHint(response.status),
      );
    }

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new ProviderDirectError(
        "供应商返回了无效的模型列表 JSON。",
        endpoint,
        "invalid_response",
        response.status,
        requestId ?? undefined,
        "确认模型列表接口返回 JSON，而不是 HTML 或空响应。",
      );
    }
    return {
      endpoint,
      statusCode: response.status,
      requestId,
      models: directModelItems(payload, endpoint),
    };
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function fetchProviderModels(profileId: string): Promise<ProviderModelsResponse> {
  const value = await request<Record<string, unknown>>(
    `/providers/${encodeURIComponent(profileId)}/models`,
  );
  return normalizeProviderModelsResponse(value, profileId);
}

export async function updateProviderModelOverrides(
  profileId: string,
  models: Array<Pick<ProviderModelRecord, "id" | "modalities" | "classification" | "available"> & { enabled?: boolean }>,
  options: {
    catalogRefreshed?: boolean;
    requestId?: string | null;
    removedModelIds?: string[];
    catalogApiVersion?: number;
  } = {},
): Promise<ProviderModelsResponse> {
  const removedModelIds = options.removedModelIds ?? [];
  if (removedModelIds.length > 0 && (options.catalogApiVersion ?? 1) < 2) {
    throw new Error("本机后端未加载模型删除接口的新版本，请重启本机 API 后再试。");
  }
  const value = await request<Record<string, unknown>>(`/providers/${encodeURIComponent(profileId)}/models`, {
    method: "PATCH",
    body: JSON.stringify({
      models,
      removed_model_ids: removedModelIds,
      catalog_refreshed: options.catalogRefreshed === true,
      request_id: options.requestId ?? null,
    }),
  });
  const result = normalizeProviderModelsResponse(value, profileId);
  if (
    removedModelIds.length > 0 &&
    (result.model_catalog_api_version < 2 || removedModelIds.some(
      (modelId) => result.models.some((model) => model.id === modelId),
    ))
  ) {
    throw new Error("本机后端未加载模型删除接口的新版本，请重启本机 API 后再试。");
  }
  return result;
}

export async function testProvider(profileId: string) {
  return request<{ models?: string[] }>(`/providers/${encodeURIComponent(profileId)}/test`, {
    method: "POST",
  });
}

interface CreatedProjectApiRecord {
  id: string;
  name: string;
  root_path: string;
}

export interface SystemInfo {
  projects_root: string;
  state_dir: string;
  project_workspace: string;
  credential_store: string;
}

export async function fetchSystemInfo(): Promise<SystemInfo> {
  return request<SystemInfo>("/system");
}

export async function createProject(directoryName: string, name: string, defaultLanguage: string) {
  const project = await request<CreatedProjectApiRecord>("/projects", {
    method: "POST",
    body: JSON.stringify({
      directory_name: directoryName,
      name,
      default_language: defaultLanguage,
    }),
    timeoutMs: 30_000,
  });
  const scan = await request<{ assets_indexed: number; revisions_indexed: number; errors: string[] }>(
    `/projects/${encodeURIComponent(project.id)}/scan`,
    { method: "POST", timeoutMs: 120_000 },
  );
  if (scan.errors.length > 0) {
    throw new Error(`Project 已创建，但扫描发现 ${scan.errors.length} 个错误：${scan.errors[0]}`);
  }
  return { project, scan };
}

export async function updateProject(projectId: string, name: string) {
  const project = await request<CreatedProjectApiRecord>(
    `/projects/${encodeURIComponent(projectId)}`,
    {
      method: "PATCH",
      body: JSON.stringify({ name }),
    },
  );
  return normalizeProject(project);
}

export async function fetchGenerationProviders(): Promise<GenerationProviderProfile[]> {
  const profiles = await request<Array<Record<string, unknown>>>("/providers");
  return profiles.map(normalizeGenerationProvider);
}

export async function createAndConfirmGenerationPlan(
  payload: GenerationPlanInput,
): Promise<{ plan: GenerationPlanRun; jobs: GenerationJobRun[] }> {
  const plan = await request<GenerationPlanRun>("/generation-plans", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const jobs = await request<GenerationJobRun[]>(
    `/generation-plans/${encodeURIComponent(plan.id)}/confirm`,
    { method: "POST" },
  );
  return { plan, jobs };
}

function generationConversationPath(sessionId: string, suffix = ""): string {
  return `/generation-conversations/${encodeURIComponent(sessionId)}${suffix}`;
}

export async function createGenerationConversation(
  projectId: string,
  seedAssetIds: string[] = [],
  title?: string,
  agentModel?: string | null,
): Promise<GenerationConversation> {
  return request<GenerationConversation>("/generation-conversations", {
    method: "POST",
    body: JSON.stringify({
      project_id: projectId,
      seed_asset_ids: seedAssetIds,
      ...(title ? { title } : {}),
      ...(agentModel ? { agent_model: agentModel } : {}),
    }),
    timeoutMs: 20_000,
  });
}

export async function fetchGenerationConversations(
  projectId?: string,
  includeArchived = false,
): Promise<GenerationConversationSummary[]> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  if (includeArchived) params.set("include_archived", "true");
  const query = params.toString() ? `?${params.toString()}` : "";
  return request<GenerationConversationSummary[]>(`/generation-conversations${query}`, {
    timeoutMs: 15_000,
  });
}

export async function steerGenerationConversationTurn(
  sessionId: string,
  content: string,
  clientMessageId?: string,
): Promise<GenerationConversationMessageResult> {
  return request<GenerationConversationMessageResult>(generationConversationPath(sessionId, "/steer"), {
    method: "POST",
    body: JSON.stringify({ content, client_message_id: clientMessageId }),
    timeoutMs: 20_000,
  });
}

export async function answerGenerationInput(
  sessionId: string,
  requestId: string,
  clientResponseId: string,
  answers: Record<string, { answers: string[] }>,
): Promise<{ request_id: string; turn_id: string; status: string; next_sequence: number }> {
  return request(generationConversationPath(sessionId, `/input-requests/${encodeURIComponent(requestId)}/answer`), {
    method: "POST",
    body: JSON.stringify({ client_response_id: clientResponseId, answers }),
    timeoutMs: 20_000,
  });
}

export async function fetchGenerationAgentCapabilities(): Promise<GenerationAgentCapabilities> {
  return request<GenerationAgentCapabilities>("/generation-agent/capabilities", { timeoutMs: 20_000 });
}

export async function fetchGenerationConversation(
  sessionId: string,
): Promise<GenerationConversation> {
  return request<GenerationConversation>(generationConversationPath(sessionId), {
    timeoutMs: 15_000,
  });
}

export async function sendGenerationConversationMessage(
  sessionId: string,
  content: string,
  contextAssetIds: string[] = [],
  clientMessageId?: string,
): Promise<GenerationConversationMessageResult> {
  return request<GenerationConversationMessageResult>(generationConversationPath(sessionId, "/messages"), {
    method: "POST",
    body: JSON.stringify({
      content,
      context_asset_ids: contextAssetIds,
      ...(clientMessageId ? { client_message_id: clientMessageId } : {}),
    }),
    timeoutMs: 20_000,
  });
}

export async function fetchGenerationConversationEvents(
  sessionId: string,
  after = 0,
): Promise<GenerationConversationEvent[]> {
  const events: GenerationConversationEvent[] = [];
  let cursor = after;
  while (true) {
    const page = await request<GenerationConversationEvent[]>(`${generationConversationPath(sessionId, "/events")}?after=${Math.max(0,cursor)}&limit=2000`, { timeoutMs: 15_000 });
    events.push(...page);
    const next = Math.max(cursor, ...page.map(x=>x.sequence));
    if (page.length < 2000 || next <= cursor) return events;
    cursor = next;
  }
}

export async function updateGenerationConversationDraft(
  sessionId: string,
  baseHash: string,
  draft: GenerationPlanningDraft,
): Promise<GenerationConversation> {
  return request<GenerationConversation>(generationConversationPath(sessionId, "/draft"), {
    method: "PATCH",
    body: JSON.stringify({ base_hash: baseHash, draft }),
    timeoutMs: 20_000,
  });
}

export async function updateGenerationConversationSettings(
  sessionId: string,
  agentModel: string | null,
): Promise<GenerationConversation> {
  return request<GenerationConversation>(generationConversationPath(sessionId, "/settings"), {
    method: "PATCH",
    body: JSON.stringify({ agent_model: agentModel }),
    timeoutMs: 15_000,
  });
}

export async function updateGenerationConversationContext(
  sessionId: string,
  seedAssetIds: string[],
): Promise<GenerationConversation> {
  return request<GenerationConversation>(generationConversationPath(sessionId, "/context"), {
    method: "PATCH",
    body: JSON.stringify({ seed_asset_ids: seedAssetIds }),
    timeoutMs: 15_000,
  });
}

export async function archiveGenerationConversation(sessionId: string): Promise<GenerationConversation> {
  return request<GenerationConversation>(generationConversationPath(sessionId, "/archive"), { method: "POST", timeoutMs: 15_000 });
}

export async function unarchiveGenerationConversation(sessionId: string): Promise<GenerationConversation> {
  return request<GenerationConversation>(generationConversationPath(sessionId, "/unarchive"), { method: "POST", timeoutMs: 15_000 });
}

export async function deleteGenerationConversation(sessionId: string): Promise<void> {
  await request<void>(generationConversationPath(sessionId), { method: "DELETE", timeoutMs: 20_000 });
}

export async function confirmGenerationConversation(
  sessionId: string,
  draftHash: string,
  acceptedWarningCodes: string[] = [],
  contextHash?: string | null,
): Promise<GenerationConversationConfirmResult> {
  return request<GenerationConversationConfirmResult>(generationConversationPath(sessionId, "/confirm"), {
    method: "POST",
    body: JSON.stringify({ draft_hash: draftHash, ...(contextHash ? { context_hash: contextHash } : {}), accepted_warning_codes: acceptedWarningCodes }),
    timeoutMs: 30_000,
  });
}

export async function cancelGenerationConversationTurn(
  sessionId: string,
): Promise<GenerationConversation> {
  return request<GenerationConversation>(generationConversationPath(sessionId, "/cancel-turn"), {
    method: "POST",
    timeoutMs: 15_000,
  });
}

export function subscribeToGenerationConversationEvents(
  sessionId: string,
  onEvent: (event: GenerationConversationEvent) => void,
  onConnectionChange?: (connected: boolean) => void,
  after = 0,
): () => void {
  const controller = new AbortController();
  let cursor = Math.max(0, after);
  let stopped = false;
  let retryCount = 0;
  const waitForReconnect = (delayMs: number) => new Promise<void>((resolve) => {
    const timer = window.setTimeout(resolve, delayMs);
    controller.signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      resolve();
    }, { once: true });
  });
  const parseEventStream = async () => {
    while (!stopped) {
      try {
        const url = `${API_ROOT}${generationConversationPath(sessionId, "/events/stream")}?after=${cursor}`;
        const response = await fetch(url, {
          headers: { Accept: "text/event-stream", ...(cursor ? { "Last-Event-ID": String(cursor) } : {}) },
          signal: controller.signal,
        });
        if (response.status === 404) {
          onConnectionChange?.(false);
          return;
        }
        if (!response.ok || !response.body) {
          throw new ApiError(`事件流连接失败（HTTP ${response.status}）`, response.status);
        }
        retryCount = 0;
        onConnectionChange?.(true);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let eventId = "";
        let eventName = "message";
        let dataLines: string[] = [];
        const flush = () => {
          if (!dataLines.length) return;
          try {
            const parsed = JSON.parse(dataLines.join("\n")) as GenerationConversationEvent;
            if (typeof parsed.sequence === "number") cursor = Math.max(cursor, parsed.sequence);
            if (!parsed.event_type && eventName !== "message") parsed.event_type = eventName;
            if (eventId && !parsed.sequence) parsed.sequence = Number(eventId);
            onEvent(parsed);
          } catch {
            // Ignore keepalives and malformed non-public frames; the durable GET
            // endpoint remains the recovery path after a reconnect.
          }
          eventId = "";
          eventName = "message";
          dataLines = [];
        };
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split(/\r?\n/);
          buffer = lines.pop() ?? "";
          for (const line of lines) {
            if (!line) {
              flush();
              continue;
            }
            if (line.startsWith(":")) continue;
            const separator = line.indexOf(":");
            const field = separator >= 0 ? line.slice(0, separator) : line;
            const valueText = separator >= 0 ? line.slice(separator + 1).trimStart() : "";
            if (field === "id") eventId = valueText;
            else if (field === "event") eventName = valueText;
            else if (field === "data") dataLines.push(valueText);
          }
        }
        if (buffer.trim()) {
          // A server may close immediately after a final frame without the
          // blank line; parse it on the way out before reconnecting.
          const finalLines = buffer.split(/\r?\n/);
          for (const line of finalLines) {
            if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
          }
        }
        flush();
        if (!stopped) onConnectionChange?.(false);
      } catch (error) {
        if (stopped || (error instanceof DOMException && error.name === "AbortError")) break;
        onConnectionChange?.(false);
      }
      if (!stopped) {
        retryCount += 1;
        await waitForReconnect(Math.min(2_000, 250 * 2 ** Math.min(retryCount - 1, 3)));
      }
    }
  };
  void parseEventStream();
  return () => {
    stopped = true;
    controller.abort();
    onConnectionChange?.(false);
  };
}

export async function fetchRunInspection(planId: string): Promise<RunInspection> {
  return request<RunInspection>(
    `/generation-plans/${encodeURIComponent(planId)}/inspect`,
    { timeoutMs: 15_000 },
  );
}

export async function updateGenerationBudget(
  planId: string,
  extraCallBudget: number,
): Promise<GenerationPlanRun> {
  return request<GenerationPlanRun>(
    `/generation-plans/${encodeURIComponent(planId)}/budget`,
    {
      method: "PATCH",
      body: JSON.stringify({ extra_call_budget: extraCallBudget }),
    },
  );
}

export async function resumeGenerationPlan(planId: string): Promise<GenerationJobRun[]> {
  return request<GenerationJobRun[]>(
    `/generation-plans/${encodeURIComponent(planId)}/resume`,
    { method: "POST" },
  );
}

export async function createRunRemediation(
  jobId: string,
  payload: {
    idempotency_key?: string;
    action: "retry" | "tool_repair" | "regenerate" | "image_edit" | "await_user";
    strategy: string;
    reason: string;
    parameters?: Record<string, unknown>;
    finding_ids?: string[];
    expected_additional_calls?: number;
  },
): Promise<RemediationRun> {
  return request<RemediationRun>(`/jobs/${encodeURIComponent(jobId)}/remediations`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function diagnoseRunWithCodex(
  jobId: string,
  findingIds: string[] = [],
): Promise<AgentSessionRun> {
  return request<AgentSessionRun>(`/jobs/${encodeURIComponent(jobId)}/agent/diagnose`, {
    method: "POST",
    body: JSON.stringify({ finding_ids: findingIds }),
    timeoutMs: 60_000,
  });
}

export function runEvidenceUrl(evidenceId: string): string {
  return `${API_ROOT}/run-evidence/${encodeURIComponent(evidenceId)}/content`;
}

export function subscribeToRunEvents(
  planId: string,
  onEvent: (event: RunEventItem) => void,
  onConnectionChange?: (connected: boolean) => void,
) {
  if (typeof EventSource === "undefined") return () => undefined;
  const source = new EventSource(
    `${API_ROOT}/runs/events?plan_id=${encodeURIComponent(planId)}`,
  );
  source.onopen = () => onConnectionChange?.(true);
  source.onmessage = (event) => {
    try {
      onEvent(JSON.parse(event.data) as RunEventItem);
    } catch {
      // The stream also emits typed events and keepalives.
    }
  };
  const handleTypedEvent = (event: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(event.data) as RunEventItem);
    } catch {
      // Ignore a malformed event without closing the durable stream.
    }
  };
  const eventTypes = [
    "plan.confirmed",
    "run.queued",
    "run.lease_acquired",
    "run.inputs_resolved",
    "run.stage_changed",
    "run.awaiting_user",
    "run.recovered",
    "run.resumed",
    "finding.created",
    "action.accepted",
    "budget.changed",
    "provider.call_started",
    "provider.call_failed",
    "provider.credentials_locked",
    "artifact.created",
    "worker.started",
  ];
  for (const eventType of eventTypes) {
    source.addEventListener(eventType, handleTypedEvent as EventListener);
  }
  source.onerror = () => onConnectionChange?.(false);
  return () => {
    for (const eventType of eventTypes) {
      source.removeEventListener(eventType, handleTypedEvent as EventListener);
    }
    source.close();
  };
}

export function subscribeToJobEvents(
  projectId: string,
  onEvent: (event: Partial<JobSummary>) => void,
  onConnectionChange: (connected: boolean) => void,
) {
  if (typeof EventSource === "undefined") return () => undefined;

  const source = new EventSource(
    `${API_ROOT}/jobs/events?project_id=${encodeURIComponent(projectId)}`,
  );
  source.onopen = () => onConnectionChange(true);
  const handleJob = (event: MessageEvent<string>) => {
    try {
      onEvent(normalizeJob(JSON.parse(event.data) as unknown));
    } catch {
      // Keep the stream alive when a vendor sends a non-JSON heartbeat.
    }
  };
  source.addEventListener("job", handleJob as EventListener);
  source.onerror = () => {
    onConnectionChange(false);
  };

  return () => {
    source.removeEventListener("job", handleJob as EventListener);
    source.close();
  };
}

export async function recoverGenerationTurn(sessionId: string, turnId: string, clientRequestId: string) {
  return request(generationConversationPath(sessionId, `/turns/${encodeURIComponent(turnId)}/recover`), { method: "POST", body: JSON.stringify({client_request_id: clientRequestId}), timeoutMs: 20_000 });
}
export async function renameGenerationConversation(sessionId: string, title: string) {
  return request<GenerationConversation>(generationConversationPath(sessionId, "/settings"), {method:"PATCH",body:JSON.stringify({title})});
}
export async function resolveGenerationProposal(sessionId: string, action: "keep" | "apply", baseHash: string) {
  return request<GenerationConversation>(generationConversationPath(sessionId,"/proposal"), {method:"POST",body:JSON.stringify({action,base_hash:baseHash})});
}
