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
  name: string;
  status: "queued" | "running" | "completed" | "paused" | "credentials_locked" | "qa_failed";
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
