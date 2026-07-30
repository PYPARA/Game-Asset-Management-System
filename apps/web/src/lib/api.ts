import type {
  AssetRevision,
  GameAsset,
  GenerationJobRun,
  GenerationPlanInput,
  GenerationPlanRun,
  GenerationProviderProfile,
  JobSummary,
  ProjectSummary,
  ProviderDefaults,
  ProviderModelRecord,
  QACheck,
  RemediationRun,
  ReviewStatus,
  RunEventItem,
  RunInspection,
  WorkbenchPayload,
} from "../types";
import type { ProviderProfile } from "../types";
import { assetSubtypeLabel, domainLabel } from "./labels";

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

export class ApiError extends Error {
  readonly status: number;
  readonly category?: string;
  readonly retryAfter?: string;

  constructor(message: string, status: number, category?: string, retryAfter?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.category = category;
    this.retryAfter = retryAfter;
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
      try {
        const payload = (await response.json()) as unknown;
        if (isRecord(payload)) {
          const detail = payload.detail;
          if (typeof detail === "string") message = detail;
          if (isRecord(detail)) {
            if (typeof detail.message === "string") message = detail.message;
            if (typeof detail.category === "string") category = detail.category;
          }
        }
      } catch {
        // Some OpenAI-compatible gateways return an empty or non-JSON error body.
      }
      throw new ApiError(message, response.status, category, response.headers.get("Retry-After") ?? undefined);
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
  const metadata = isRecord(value.asset_metadata) ? value.asset_metadata : {};
  const subtypeKey = subtype.toLocaleLowerCase();
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
              : "2d-media";
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
      : {
          kind: "placeholder" as const,
          label: kind === "entity" ? (subtype === "character" ? "角色实体" : subtype === "achievement" ? "成就实体" : "物品实体") : "媒体资产",
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
    productionStage,
    timeAccuracy,
    preview,
    updatedAt: timeAccuracy === "unknown" ? "" : rawUpdatedAt,
    updatedLabel: timeAccuracy === "unknown" ? "历史时间未知" : rawUpdatedAt || "—",
    updatedBy: String(value.updated_by ?? (timeAccuracy === "unknown" ? "历史导入" : "本机索引")),
    thumbnails: [],
    linkedMedia: [],
    relatedAssets: [],
    revisionFormat: kind === "media" || kind === "production" ? "media" : subtype === "style_bible" ? "markdown" : "json",
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

export async function fetchAssetDetails(asset: GameAsset): Promise<GameAsset> {
  const revisions = await request<RevisionApiRecord[]>(
    `/revisions?asset_id=${encodeURIComponent(asset.id)}`,
  );
  const candidate = asset.candidateRevisionId
    ? revisions.find((revision) => revision.id === asset.candidateRevisionId)
    : undefined;
  const approved = asset.approvedRevisionId
    ? revisions.find((revision) => revision.id === asset.approvedRevisionId)
    : undefined;
  const relevant = [candidate, approved].filter(
    (revision): revision is RevisionApiRecord => Boolean(revision),
  );
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
  const isMedia = asset.kind === "media";
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
  const scan = await request<{ errors: string[] }>(
    `/projects/${encodeURIComponent(project.id)}/scan`,
    { method: "POST", timeoutMs: 120_000 },
  );
  if (scan.errors.length > 0) {
    throw new ApiError(`Project 扫描失败：${scan.errors[0]}`, 409, "scan_failed");
  }

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
    return asset.kind === "media" || asset.kind === "production"
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
  pricing?: Record<string, unknown> | null;
  is_active?: boolean;
  models?: ProviderModelRecord[];
  models_refreshed_at?: string | null;
  is_unlocked?: boolean;
}

interface ProviderModelsResponse {
  provider_profile_id: string;
  models: ProviderModelRecord[];
  refreshed_at: string | null;
}

function providerPayload(profile: ProviderProfile) {
  return {
    name: profile.name,
    kind: "openai_compatible",
    base_url: profile.baseUrl,
    text_model: profile.textModel,
    image_model: profile.imageModel,
    quality: profile.quality,
    concurrency: profile.concurrency,
    max_retries: profile.retries,
    allow_private_network: profile.allowPrivateNetwork,
  };
}

function normalizeGenerationProvider(profile: Record<string, unknown>): GenerationProviderProfile {
  const quality = String(profile.quality ?? "high");
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
    pricing: isRecord(profile.pricing)
      ? Object.fromEntries(
          Object.entries(profile.pricing).flatMap(([key, value]) => {
            const parsed = Number(value);
            return Number.isFinite(parsed) ? [[key, parsed]] : [];
          }),
        )
      : null,
    is_active: profile.is_active !== false,
    models: Array.isArray(profile.models)
      ? profile.models.flatMap((model) => isRecord(model) && model.id
        ? [{
            id: String(model.id),
            modalities: Array.isArray(model.modalities)
              ? model.modalities.filter((value): value is "text" | "image" => value === "text" || value === "image")
              : [],
            classification: model.classification === "provider" || model.classification === "heuristic" || model.classification === "manual"
              ? model.classification
              : "unknown",
            available: model.available !== false,
          }]
        : [])
      : [],
    models_refreshed_at: typeof profile.models_refreshed_at === "string" ? profile.models_refreshed_at : null,
    is_unlocked: profile.is_unlocked === true,
  };
}

export async function ensureProviderProfile(profile: ProviderProfile): Promise<ProviderApiRecord> {
  const profiles = await request<ProviderApiRecord[]>("/providers");
  const existing = profiles.find(
    (item) =>
      (item.id === profile.id || item.name === profile.name) &&
      item.base_url === profile.baseUrl &&
      item.text_model === profile.textModel &&
      item.image_model === profile.imageModel &&
      item.quality === profile.quality &&
      item.concurrency === profile.concurrency &&
      item.max_retries === profile.retries &&
      item.allow_private_network === profile.allowPrivateNetwork,
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
  return request<ProviderDefaults>("/provider-defaults");
}

export async function updateProviderDefaults(defaults: ProviderDefaults): Promise<ProviderDefaults> {
  return request<ProviderDefaults>("/provider-defaults", {
    method: "PUT",
    body: JSON.stringify({ text: defaults.text, image: defaults.image }),
  });
}

export async function fetchProviderModels(profileId: string): Promise<ProviderModelsResponse> {
  return request<ProviderModelsResponse>(`/providers/${encodeURIComponent(profileId)}/models`);
}

export async function refreshProviderModels(profileId: string): Promise<ProviderModelsResponse> {
  return request<ProviderModelsResponse>(
    `/providers/${encodeURIComponent(profileId)}/models/refresh`,
    { method: "POST", timeoutMs: 120_000 },
  );
}

export async function updateProviderModelOverrides(
  profileId: string,
  models: Array<Pick<ProviderModelRecord, "id" | "modalities">>,
): Promise<ProviderModelsResponse> {
  return request<ProviderModelsResponse>(`/providers/${encodeURIComponent(profileId)}/models`, {
    method: "PATCH",
    body: JSON.stringify({ models }),
  });
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
