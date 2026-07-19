export type ReviewStatus = "pending" | "approved" | "rejected" | "generating";

export type AssetKind = "content" | "design" | "entity" | "media" | "production";

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
  label: string;
  image: string;
  createdAt: string;
  author: string;
  resolution: string;
  fileSize: string;
  status: "candidate" | "approved" | "rejected";
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
  key: string;
  name: string;
  kind: AssetKind;
  category: string;
  subtype: string;
  tags: string[];
  reviewStatus: ReviewStatus;
  qaPassed: number;
  qaTotal: number;
  updatedAt: string;
  updatedLabel: string;
  updatedBy: string;
  thumbnails: string[];
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
  status: "queued" | "running" | "completed" | "paused" | "credentials_locked";
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
  source: "api" | "demo";
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
