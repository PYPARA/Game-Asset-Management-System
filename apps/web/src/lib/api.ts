import { demoAssets, demoPayload, demoProject } from "../demo-data";
import type {
  AssetRevision,
  GameAsset,
  JobSummary,
  ProjectSummary,
  QACheck,
  ReviewStatus,
  WorkbenchPayload,
} from "../types";
import type { ProviderProfile } from "../types";

const API_ROOT =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ?? "/api";

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
  if (!isRecord(value)) return demoProject;
  return {
    id: String(value.id ?? value.key ?? demoProject.id),
    name: String(value.name ?? demoProject.name),
    path: String(value.path ?? value.root_path ?? demoProject.path),
    branch: typeof value.branch === "string" ? value.branch : demoProject.branch,
    assetCount: Number(value.asset_count ?? value.assetCount ?? demoProject.assetCount),
    thumbnail: demoProject.thumbnail,
  };
}

function normalizeAsset(value: unknown, index: number): GameAsset {
  const fallback = demoAssets[index % demoAssets.length];
  if (!isRecord(value)) return fallback;

  const contentStatus = String(value.content_status ?? "candidate");
  const generationStatus = String(value.generation_status ?? "idle");
  const status = String(
    value.review_status ??
      value.reviewStatus ??
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

  const rawKind = String(value.kind ?? fallback.kind);
  const kind = ["content", "design", "entity", "media", "production"].includes(rawKind)
    ? (rawKind as GameAsset["kind"])
    : fallback.kind;
  const subtype = String(value.subtype ?? value.asset_type ?? fallback.subtype);
  const subtypeKey = subtype.toLocaleLowerCase();
  const category =
    kind === "content"
      ? "content"
      : kind === "design"
        ? "design"
        : kind === "production"
          ? "materials"
          : kind === "entity"
            ? subtypeKey.includes("character") || subtype.includes("角色") || subtype.includes("人物")
              ? "characters"
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

  return {
    id: String(value.id ?? fallback.id),
    candidateRevisionId,
    approvedRevisionId,
    reviewReady: false,
    reviewBlockReason: "正在加载真实候选修订与 QA 证据。",
    key: String(value.key ?? value.stable_key ?? fallback.key),
    name: String(value.name ?? value.title ?? fallback.name),
    kind,
    category: String(value.category ?? category),
    subtype,
    tags: Array.isArray(value.tags) ? value.tags.map(String) : fallback.tags,
    reviewStatus,
    updatedAt: String(value.updated_at ?? fallback.updatedAt),
    updatedLabel: String(value.updated_label ?? value.updated_at ?? "—"),
    updatedBy: String(value.updated_by ?? "本机索引"),
    thumbnails: [],
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
    return {
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
  }
  const rawStatus = String(value.status ?? "paused");
  const status = ["queued", "running", "completed", "paused", "credentials_locked"].includes(rawStatus)
    ? rawStatus
    : rawStatus === "succeeded"
      ? "completed"
      : "paused";
  const progress = Number(value.progress ?? 0);
  return {
    id: String(value.id ?? "job"),
    name: String(value.name ?? value.title ?? value.task_id ?? "生成任务"),
    progress,
    completed: Number(value.completed ?? (status === "completed" ? 1 : 0)),
    total: Number(value.total ?? 1),
    passed: Number(value.passed ?? (status === "completed" && value.result_revision_id ? 1 : 0)),
    status: status as JobSummary["status"],
    previewImages: Array.isArray(value.preview_images) ? value.preview_images.map(String) : [],
    outputPath: String(value.output_path ?? value.outputPath ?? ""),
  };
}

interface RevisionApiRecord {
  id: string;
  asset_id: string;
  sequence: number;
  format: string;
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
  const suffix = status === "candidate" ? "候选" : status === "approved" ? "已批准" : "已驳回";
  return {
    id: `r${sequence}`,
    label: `r${sequence}（${suffix}）`,
    image: rendition ? renditionUrl(rendition.id) : "",
    createdAt: revision.created_at,
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
    Boolean(candidate) && isMedia && qaReady && asset.reviewStatus !== "rejected";
  const candidateRendition = candidateRenditions[0];
  const approvedRendition = approved
    ? renditions.find((rendition) => rendition.revision_id === approved.id)
    : undefined;
  const revisionViews: AssetRevision[] = [];
  if (candidate) revisionViews.push(normalizeRevision(candidate, candidateRendition, "candidate"));
  if (approved) revisionViews.push(normalizeRevision(approved, approvedRendition, "approved"));
  for (const revision of revisions) {
    if (revision.id === candidate?.id || revision.id === approved?.id) continue;
    const status = revision.review_status === "rejected" ? "rejected" : "approved";
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
    reviewReady,
    reviewBlockReason: !candidate
      ? "当前没有待审候选修订。"
      : !isMedia
        ? "结构化文本 diff 审核尚未接入，当前禁止直接批准。"
      : !qaReady
        ? "候选媒体尚未具备完整且通过的硬 QA 证据。"
        : asset.reviewStatus === "rejected"
          ? "该候选已驳回，请先生成新的不可变候选。"
          : undefined,
    revisions: revisionViews,
    thumbnails,
    qa: checks,
    qaPassed: checks.filter((check) => check.passed).length,
    qaTotal: checks.length,
    prompt: textField(content, "prompt", "positivePrompt", "positive_prompt"),
    negativePrompt: textField(content, "negativePrompt", "negative_prompt"),
    model: String(provider.model ?? provider.image_model ?? "unknown"),
    recipe: String(candidate?.prompt_recipe ?? approved?.prompt_recipe ?? "unknown"),
    seed: String(provider.seed ?? "unknown"),
    updatedAt: candidate?.created_at ?? approved?.created_at ?? asset.updatedAt,
    updatedLabel: candidate?.created_at ?? approved?.created_at ?? asset.updatedLabel,
  };
}

export async function fetchWorkbench(): Promise<WorkbenchPayload> {
  try {
    const projectResult = await request<ProjectSummary[] | ApiList<ProjectSummary>>("/projects");
    const projects = unpackList(projectResult);
    if (projects.length === 0) return demoPayload;
    const project = normalizeProject(projects[0]);

    const [assetResult, jobResult] = await Promise.all([
      request<GameAsset[] | ApiList<GameAsset>>(`/assets?project_id=${encodeURIComponent(project.id)}`),
      request<JobSummary[] | ApiList<JobSummary>>(`/jobs?project_id=${encodeURIComponent(project.id)}`).catch(
        () => [],
      ),
    ]);

    const assets = unpackList(assetResult).map(normalizeAsset);
    const hydratedAssets = await mapWithConcurrency(assets, 8, async (asset) => {
      if (!asset.candidateRevisionId && !asset.approvedRevisionId) {
        return {
          ...asset,
          reviewBlockReason: "当前资产还没有候选或已批准修订。",
        };
      }
      try {
        return await fetchAssetDetails(asset);
      } catch (error) {
        return {
          ...asset,
          reviewReady: false,
          reviewBlockReason:
            error instanceof Error ? `真实修订加载失败：${error.message}` : "真实修订加载失败。",
        };
      }
    });
    const jobs = unpackList(jobResult);

    return {
      project,
      assets: hydratedAssets,
      job: jobs.length > 0 ? normalizeJob(jobs[0]) : normalizeJob(undefined),
      source: "api",
    };
  } catch {
    return demoPayload;
  }
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
    body: JSON.stringify({
      name: profile.name,
      kind: "openai_compatible",
      base_url: profile.baseUrl,
      text_model: profile.textModel,
      image_model: profile.imageModel,
      quality: profile.quality,
      concurrency: profile.concurrency,
      max_retries: profile.retries,
      allow_private_network: profile.allowPrivateNetwork,
    }),
  });
}

export async function testProvider(profileId: string) {
  return request<{ models?: string[] }>(`/providers/${encodeURIComponent(profileId)}/test`, {
    method: "POST",
  });
}

export async function previewEmperorImport(path: string) {
  return request<Record<string, unknown>>("/importers/emperor/preview", {
    method: "POST",
    body: JSON.stringify({ source_root: path }),
    timeoutMs: 30_000,
  });
}

export async function executeEmperorImport(path: string, destinationPath = path) {
  return request<Record<string, unknown>>("/importers/emperor/import", {
    method: "POST",
    body: JSON.stringify({
      source_root: path,
      destination_path: destinationPath,
      apply: true,
    }),
    timeoutMs: 120_000,
  });
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
