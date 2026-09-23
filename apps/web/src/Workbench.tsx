import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type RowSelectionState,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import {
  Article,
  BookOpen,
  CaretDown,
  CheckCircle,
  CloudArrowUp,
  CrownSimple,
  DotsThree,
  FileText,
  FolderOpen,
  GearSix,
  GitBranch,
  ImagesSquare,
  ListBullets,
  MagnifyingGlass,
  MapPin,
  Notebook,
  Package,
  Palette,
  PencilSimple,
  Plus,
  SealCheck,
  SlidersHorizontal,
  SpeakerHigh,
  Sparkle,
  SquaresFour,
  Star,
  Tray,
  TreeStructure,
  UploadSimple,
  UserCircle,
  UsersThree,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { ProjectDialog } from "./components/ProjectDialog";
import { AssetEditorDrawer } from "./components/AssetEditorDrawer";
import { Inspector } from "./components/Inspector";
import { ProviderChannelsDrawer } from "./components/ProviderChannelsDrawer";
import { SystemSettingsDrawer } from "./components/SystemSettingsDrawer";
import { DeliveryDrawer } from "./components/DeliveryDrawer";
import { NarrativeAtlas } from "./components/NarrativeAtlas";
import { VersionControlDrawer } from "./components/VersionControlDrawer";
import { SelectMenu } from "./components/SelectMenu";
import {
  ApiError,
  emptyWorkbenchPayload,
  createAssetRevision,
  fetchAssetDetails,
  fetchGenerationProviders,
  fetchGenerationConversations,
  fetchNarrativeMap,
  fetchWorkbench,
  submitReview,
  subscribeToJobEvents,
  unlockProvider,
} from "./lib/api";
import { readCredential } from "./lib/credentials";
import { assetSubtypeLabel } from "./lib/labels";
import type { GameAsset, GenerationConversationSummary, JobSummary, NarrativeMap, ProjectSummary, ReviewStatus } from "./types";

const GenerationChatPage = lazy(() =>
  import("./components/GenerationChatPage").then((module) => ({
    default: module.GenerationChatPage,
  })),
);
const RunInspectorDrawer = lazy(() =>
  import("./components/RunInspectorDrawer").then((module) => ({
    default: module.RunInspectorDrawer,
  })),
);

type LibraryPredicate = (asset: GameAsset) => boolean;

interface LibraryView {
  id: string;
  label: string;
  icon: typeof Article;
  predicate: LibraryPredicate;
}

interface LibraryGroup {
  id: string;
  label: string;
  views: LibraryView[];
}

const sceneSubtypes = new Set(["scene", "event", "story_event", "dialogue_scene"]);
const storyArcSubtypes = new Set(["story_arc", "story-arc", "story", "chain"]);

function hasSubtype(asset: GameAsset, values: Set<string>): boolean {
  return values.has(asset.subtype.toLocaleLowerCase());
}

function assetKeyMatches(asset: GameAsset, segment: string): boolean {
  const key = `.${asset.key.toLocaleLowerCase()}.`;
  return key.includes(`.${segment}.`) || key.startsWith(`.${segment}.`);
}

function isSceneAsset(asset: GameAsset): boolean {
  return asset.kind === "content" && hasSubtype(asset, sceneSubtypes);
}

function mediaSubtypeMatches(asset: GameAsset, kind: "portrait" | "background" | "cg" | "icon" | "ending" | "audio"): boolean {
  const subtype = asset.subtype.toLocaleLowerCase();
  if (asset.kind !== "media") return false;
  if (kind === "portrait") return subtype === "portrait" || subtype.includes("character_portrait") || subtype.includes("character-portrait") || subtype.includes("立绘") || assetKeyMatches(asset, "portrait");
  if (kind === "background") return subtype === "background" || subtype.includes("scene_background") || subtype.includes("scene-background") || subtype.includes("背景") || assetKeyMatches(asset, "background");
  if (kind === "cg") return subtype === "cg" || subtype === "story_cg" || subtype === "story-cg" || subtype.includes("剧情 cg") || assetKeyMatches(asset, "cg");
  if (kind === "icon") return subtype === "icon" || subtype.includes("item_icon") || subtype.includes("item-icon") || subtype.includes("图标") || assetKeyMatches(asset, "icon");
  if (kind === "ending") return subtype === "ending" || subtype.includes("ending_illustration") || subtype.includes("ending-illustration") || subtype.includes("结局") || assetKeyMatches(asset, "ending");
  return subtype.includes("audio") || subtype.includes("音频") || assetKeyMatches(asset, "audio");
}

// A media asset is assigned to exactly one leaf.  More specific narrative
// media (ending art / CG) wins over broad words such as “background”, while
// unknown subtypes remain reachable through the dynamic “其他类型” fallback.
function mediaViewId(asset: GameAsset): string | null {
  if (mediaSubtypeMatches(asset, "audio")) return "audio";
  if (mediaSubtypeMatches(asset, "portrait")) return "portraits";
  if (mediaSubtypeMatches(asset, "ending")) return "ending-illustrations";
  if (mediaSubtypeMatches(asset, "cg")) return "cgs";
  if (mediaSubtypeMatches(asset, "background")) return "backgrounds";
  if (mediaSubtypeMatches(asset, "icon")) return "icons";
  return null;
}

function isPortraitAsset(asset: GameAsset): boolean { return mediaViewId(asset) === "portraits"; }
function isBackgroundAsset(asset: GameAsset): boolean { return mediaViewId(asset) === "backgrounds"; }
function isCgAsset(asset: GameAsset): boolean { return mediaViewId(asset) === "cgs"; }
function isIconAsset(asset: GameAsset): boolean { return mediaViewId(asset) === "icons"; }
function isEndingIllustration(asset: GameAsset): boolean { return mediaViewId(asset) === "ending-illustrations"; }
function isAudioAsset(asset: GameAsset): boolean { return mediaViewId(asset) === "audio"; }

const allAssetsView: LibraryView = { id: "all", label: "全部资产", icon: SquaresFour, predicate: () => true };

const baseLibraryGroups: LibraryGroup[] = [
  {
    id: "narrative",
    label: "叙事",
    views: [
      { id: "scenes", label: "场景", icon: Article, predicate: isSceneAsset },
      { id: "story-arcs", label: "故事弧", icon: TreeStructure, predicate: (asset) => asset.kind === "content" && hasSubtype(asset, storyArcSubtypes) },
      { id: "memorials", label: "奏折", icon: FileText, predicate: (asset) => asset.kind === "content" && asset.subtype === "memorial" },
      { id: "ending-content", label: "结局内容", icon: BookOpen, predicate: (asset) => asset.kind === "content" && asset.subtype === "ending" },
    ],
  },
  {
    id: "world",
    label: "世界",
    views: [
      { id: "characters", label: "角色", icon: UsersThree, predicate: (asset) => asset.kind === "entity" && asset.subtype === "character" },
      { id: "locations", label: "地点", icon: MapPin, predicate: (asset) => asset.kind === "entity" && asset.subtype === "location" },
      { id: "items", label: "物品", icon: Package, predicate: (asset) => asset.kind === "entity" && asset.subtype === "item" },
      { id: "achievements", label: "成就", icon: SealCheck, predicate: (asset) => asset.kind === "entity" && asset.subtype === "achievement" },
    ],
  },
  {
    id: "media",
    label: "媒体",
    views: [
      { id: "portraits", label: "角色立绘", icon: UserCircle, predicate: isPortraitAsset },
      { id: "backgrounds", label: "场景背景", icon: MapPin, predicate: isBackgroundAsset },
      { id: "cgs", label: "剧情 CG", icon: ImagesSquare, predicate: isCgAsset },
      { id: "icons", label: "物品图标", icon: Package, predicate: isIconAsset },
      { id: "ending-illustrations", label: "结局插画", icon: ImagesSquare, predicate: isEndingIllustration },
      { id: "audio", label: "音频", icon: SpeakerHigh, predicate: isAudioAsset },
    ],
  },
  {
    id: "design",
    label: "设计",
    views: [
      { id: "game-design", label: "游戏设计", icon: Notebook, predicate: (asset) => asset.kind === "design" && asset.subtype !== "style_bible" && ["action", "game_design", "design"].includes(asset.subtype) },
    ],
  },
  {
    id: "project-specs",
    label: "项目规范",
    views: [
      { id: "style-bible", label: "风格圣经", icon: Palette, predicate: (asset) => asset.kind === "design" && asset.subtype === "style_bible" },
      { id: "prompt-recipes", label: "Prompt 配方", icon: FileText, predicate: (asset) => asset.kind === "production" && asset.subtype === "prompt_recipe" },
      { id: "visual-anchors", label: "视觉锚点", icon: ImagesSquare, predicate: (asset) => asset.kind === "production" && asset.subtype === "visual_anchor" },
    ],
  },
];

function libraryGroupsFor(assets: GameAsset[]): LibraryGroup[] {
  const covered = new Set<GameAsset>();
  for (const group of baseLibraryGroups) {
    for (const view of group.views) {
      for (const asset of assets) if (view.predicate(asset)) covered.add(asset);
    }
  }
  const otherByGroup: Array<[string, LibraryPredicate]> = [
    ["narrative", (asset) => asset.kind === "content"],
    ["world", (asset) => asset.kind === "entity"],
    ["media", (asset) => asset.kind === "media"],
    ["design", (asset) => asset.kind === "design"],
    ["project-specs", (asset) => asset.kind === "production"],
  ];
  return baseLibraryGroups.map((group) => {
    const other = otherByGroup.find(([id]) => id === group.id)?.[1];
    const unknownAssets = other ? assets.filter((asset) => other(asset) && !covered.has(asset)) : [];
    return unknownAssets.length
      ? { ...group, views: [...group.views, { id: `${group.id}-other`, label: "其他类型", icon: FileText, predicate: (asset) => unknownAssets.some((item) => item.id === asset.id) }] }
      : group;
  });
}

const workspaceItems = [
  { id: "review", label: "待审查", icon: Tray },
  { id: "mine", label: "我创建的", icon: UserCircle },
  { id: "following", label: "已关注", icon: Star },
  { id: "released", label: "已发布", icon: SealCheck },
] as const;

type WorkspaceFilter = (typeof workspaceItems)[number]["id"] | "all";

const localAuthorAliases = new Set(["本机用户", "本地用户", "local", "local-user", "local_user"]);

function isLocalCreatedAsset(asset: GameAsset): boolean {
  const author = asset.createdBy?.trim().toLocaleLowerCase();
  return Boolean(author && localAuthorAliases.has(author));
}

const statusCopy: Record<ReviewStatus, string> = {
  pending: "待本系统确认",
  approved: "已审查",
  rejected: "已驳回",
  generating: "生成中",
};

const productionStageCopy: Record<string, string> = {
  planned: "已规划",
  generated: "已生成",
  normalized: "已归一化",
  reviewed: "旧系统已审核",
  integrated: "已集成",
  approved: "已批准",
  imported: "已导入",
};

function matchesSearch(asset: GameAsset, search: string) {
  const value = search.trim().toLocaleLowerCase();
  if (!value) return true;
  return [asset.key, asset.name, asset.subtype, asset.subtypeLabel, ...asset.tags]
    .join(" ")
    .toLocaleLowerCase()
    .includes(value);
}

async function unlockActiveProviderCredentials(): Promise<string[]> {
  const providers = (await fetchGenerationProviders()).filter((provider) => provider.is_active);
  const issues: string[] = [];
  await Promise.all(providers.map(async (provider) => {
    try {
      if (provider.kind !== "fake") {
        const apiKey = await readCredential(provider.id);
        const credentialMode = provider.credential_mode ?? "required";
        if (!apiKey && credentialMode === "required") return;
        if (apiKey) await unlockProvider(provider.id, apiKey);
      }
    } catch (error) {
      if (error instanceof ApiError) {
        const diagnostics = [
          error.endpoint,
          error.status ? `HTTP ${error.status}` : "",
          error.hint,
        ].filter(Boolean).join("；");
        issues.push(`${provider.name}：${error.message}${diagnostics ? `（${diagnostics}）` : ""}`);
      } else {
        issues.push(`${provider.name}：${error instanceof Error ? error.message : "后台同步失败"}`);
      }
    }
  }));
  return issues;
}

function AssetPreview({ asset }: { asset: GameAsset }) {
  if (asset.preview.kind === "content") {
    return (
      <div className="asset-preview-card content-preview-card">
        <FileText size={22} weight="duotone" />
        <span><strong>{asset.preview.label}</strong><small>{asset.preview.meta}</small></span>
        <p>{asset.preview.summary}</p>
      </div>
    );
  }
  if (asset.preview.kind === "placeholder") {
    const Icon = asset.subtype === "character" ? UserCircle : asset.subtype === "achievement" ? SealCheck : Package;
    return (
      <div className="asset-preview-card placeholder-preview-card">
        <Icon size={24} weight="duotone" />
        <span><strong>{asset.preview.label}</strong><small>{asset.preview.detail}</small></span>
      </div>
    );
  }
  return (
    <div className={`asset-preview ${asset.preview.images.length === 1 ? "wide" : ""}`} aria-label={asset.preview.label}>
      {asset.preview.images.map((image, index) => (
        <img key={`${image}-${index}`} src={image} alt="" loading="lazy" />
      ))}
    </div>
  );
}

export function Workbench() {
  const { data: queryData, error, isError, isFetching, isLoading, refetch } = useQuery({
    queryKey: ["workbench"],
    queryFn: fetchWorkbench,
  });
  const providerBootstrapStarted = useRef(false);
  const data = isError ? emptyWorkbenchPayload : (queryData ?? emptyWorkbenchPayload);
  const narrativeQuery = useQuery<NarrativeMap>({
    queryKey: ["narrative-map", data.project.id],
    queryFn: () => fetchNarrativeMap(data.project.id),
    enabled: Boolean(data.project.id),
  });
  const generationConversationsQuery = useQuery<GenerationConversationSummary[]>({
    queryKey: ["generation-conversations", data.project.id],
    queryFn: () => fetchGenerationConversations(data.project.id),
    enabled: Boolean(data.project.id),
    staleTime: 10_000,
    refetchInterval: (query) => (
      (query.state.data ?? []).some((item) => item.status === "running") ? 5_000 : 30_000
    ),
  });
  const [assets, setAssets] = useState<GameAsset[]>([]);
  const [job, setJob] = useState<JobSummary>(emptyWorkbenchPayload.job);
  const [streamConnected, setStreamConnected] = useState(false);
  const [activeViewId, setActiveViewId] = useState("scenes");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [workspaceFilter, setWorkspaceFilter] = useState<WorkspaceFilter>("all");
  const [followedAssetIds, setFollowedAssetIds] = useState<string[]>([]);
  const [followStorageReadyProjectId, setFollowStorageReadyProjectId] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [sceneChapterFilter, setSceneChapterFilter] = useState<"all" | "assigned" | "unassigned">("all");
  const [sceneCoverageFilter, setSceneCoverageFilter] = useState<"all" | "ready" | "missing">("all");
  const [viewMode, setViewMode] = useState<"thumbnails" | "compact">("thumbnails");
  const [sorting, setSorting] = useState<SortingState>([{ id: "updatedAt", desc: true }]);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [providerChannelsOpen, setProviderChannelsOpen] = useState(false);
  const [systemSettingsOpen, setSystemSettingsOpen] = useState(false);
  const [deliveryOpen, setDeliveryOpen] = useState(false);
  const [versionControlOpen, setVersionControlOpen] = useState(false);
  const [generationCenterOpen, setGenerationCenterOpen] = useState(()=>window.location.pathname.startsWith("/generation"));
  const [generationSessionId, setGenerationSessionId] = useState<string | null>(()=>decodeURIComponent(window.location.pathname.split("/")[2]??"")||null);
  const [generationSeedAssetIds, setGenerationSeedAssetIds] = useState<string[]>([]);
  const [generationCreateOnMount, setGenerationCreateOnMount] = useState(false);
  const navigateGeneration = (sessionId: string|null, opened=true) => {
    const path=opened ? `/generation${sessionId?`/${encodeURIComponent(sessionId)}`:""}` : "/";
    if (window.location.pathname!==path) window.history.pushState({},"",path);
    setGenerationCenterOpen(opened);
    if(opened)setGenerationSessionId(sessionId);
  };
  useEffect(()=>{
    const restore=()=>{setGenerationCenterOpen(window.location.pathname.startsWith("/generation"));setGenerationSessionId(decodeURIComponent(window.location.pathname.split("/")[2]??"")||null);setGenerationCreateOnMount(false);};
    window.addEventListener("popstate",restore);
    return ()=>window.removeEventListener("popstate",restore);
  },[]);
  const generationCenterTriggerRef = useRef<HTMLButtonElement | null>(null);
  const generationCenterWasOpen = useRef(false);
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
  const [projectToEdit, setProjectToEdit] = useState<ProjectSummary | null>(null);
  const [toast, setToast] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(60);
  const [editingAssetId, setEditingAssetId] = useState<string | null>(null);
  const [runInspectorPlanId, setRunInspectorPlanId] = useState<string | null>(null);
  const [runFocusAssetIds, setRunFocusAssetIds] = useState<string[]>([]);
  const [workspaceMode, setWorkspaceMode] = useState<"assets" | "narrative">("assets");
  const [narrativeFocusSceneId, setNarrativeFocusSceneId] = useState<string | null>(null);

  useEffect(() => {
    if (generationCenterOpen) {
      generationCenterWasOpen.current = true;
      return;
    }
    if (generationCenterWasOpen.current) {
      generationCenterWasOpen.current = false;
      generationCenterTriggerRef.current?.focus();
    }
  }, [generationCenterOpen]);

  const generationActivityCount = useMemo(() => {
    return (generationConversationsQuery.data ?? [])
      .filter((item) => item.status === "running" || item.status === "awaiting_input" || item.status === "awaiting_user" || item.status === "unavailable" || item.status === "completed")
      .length;
  }, [generationConversationsQuery.data]);

  useEffect(() => {
    if (isError || isLoading) {
      if (isError) providerBootstrapStarted.current = false;
      return;
    }
    if (providerBootstrapStarted.current) return;
    providerBootstrapStarted.current = true;
    void unlockActiveProviderCredentials().then((issues) => {
      if (issues.length > 0) {
        setToast(`供应商后台同步有 ${issues.length} 项未完成：${issues[0]}`);
      }
    }).catch((error) => {
      setToast(error instanceof Error ? `供应商后台初始化失败：${error.message}` : "供应商后台初始化失败。 ");
      providerBootstrapStarted.current = false;
    });
  }, [isError, isLoading]);

  const openProjectDialog = (project: ProjectSummary | null = null) => {
    setProjectToEdit(project);
    setProjectDialogOpen(true);
  };

  const closeProjectDialog = () => {
    setProjectDialogOpen(false);
    setProjectToEdit(null);
  };

  useEffect(() => {
    setAssets(data.assets);
    setJob(data.job);
  }, [data.assets, data.job]);

  // Follows are a local workspace preference rather than a catalog field.
  // Scope the key to the project so switching projects never leaks stars
  // into another catalog, and tolerate malformed/old localStorage values.
  useEffect(() => {
    const projectId = data.project.id;
    if (!projectId) {
      setFollowedAssetIds([]);
      setFollowStorageReadyProjectId("");
      setWorkspaceFilter("all");
      return;
    }
    let ids: string[] = [];
    try {
      const raw = window.localStorage.getItem(`gams.followedAssets.${projectId}`);
      const parsed: unknown = raw ? JSON.parse(raw) : [];
      if (Array.isArray(parsed)) ids = parsed.filter((id): id is string => typeof id === "string");
    } catch {
      // A corrupted preference should not make the whole workbench fail.
    }
    setFollowedAssetIds(ids);
    setFollowStorageReadyProjectId(projectId);
  }, [data.project.id]);

  useEffect(() => {
    const projectId = data.project.id;
    if (!projectId || followStorageReadyProjectId !== projectId) return;
    try {
      window.localStorage.setItem(`gams.followedAssets.${projectId}`, JSON.stringify(followedAssetIds));
    } catch {
      // Storage can be unavailable in private/sandboxed browser contexts;
      // following still works for the current session in that case.
    }
  }, [data.project.id, followStorageReadyProjectId, followedAssetIds]);

  useEffect(() => {
    if (activeViewId === "scenes" && data.assets.length > 0 && !data.assets.some(isSceneAsset)) {
      setActiveViewId("all");
    }
  }, [activeViewId, data.assets]);

  useEffect(() => {
    if (!data.project.id) {
      setStreamConnected(false);
      return;
    }
    return subscribeToJobEvents(
      data.project.id,
      (event) => setJob((current) => ({ ...current, ...event })),
      setStreamConnected,
    );
  }, [data.project.id]);

  useEffect(() => {
    setRowSelection({});
    setVisibleLimit(60);
  }, [activeViewId, sceneChapterFilter, sceneCoverageFilter, search, statusFilter, typeFilter, workspaceFilter]);

  useEffect(() => {
    setTypeFilter("all");
    setSceneChapterFilter("all");
    setSceneCoverageFilter("all");
  }, [activeViewId]);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(""), 3_400);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  useEffect(() => {
    const pendingPlanId = window.localStorage.getItem("gams.openRunInspector");
    if (!pendingPlanId) return;
    window.localStorage.removeItem("gams.openRunInspector");
    setRunFocusAssetIds([]);
    setRunInspectorPlanId(pendingPlanId);
  }, []);

  const sceneInfoByAssetId = useMemo(() => {
    const atlas = narrativeQuery.data;
    const chapterTitles = new Map((atlas?.chapters ?? []).map((chapter) => [chapter.asset_id, chapter.title]));
    return new Map((atlas?.scenes ?? []).map((scene) => [
      scene.asset_id,
      {
        chapterAssetId: scene.chapter_asset_id,
        chapterTitle: scene.chapter_asset_id ? chapterTitles.get(scene.chapter_asset_id) ?? "未命名章节" : null,
        assigned: Boolean(scene.chapter_asset_id),
        required: scene.coverage.required,
        ready: scene.coverage.ready,
        missing: scene.coverage.missing,
        ratio: scene.coverage.ratio,
      },
    ]));
  }, [narrativeQuery.data]);
  const indexedAssets = useMemo(
    () => assets.map((asset) => {
      const sceneInfo = sceneInfoByAssetId.get(asset.id);
      return sceneInfo ? { ...asset, sceneInfo } : asset;
    }),
    [assets, sceneInfoByAssetId],
  );
  const libraryGroups = useMemo(() => libraryGroupsFor(indexedAssets), [indexedAssets]);
  const libraryViews = useMemo(
    () => [allAssetsView, ...libraryGroups.flatMap((group) => group.views)],
    [libraryGroups],
  );
  const activeView = libraryViews.find((view) => view.id === activeViewId) ?? allAssetsView;
  const subtypes = useMemo(
    () => Array.from(new Set(indexedAssets.filter(activeView.predicate).map((asset) => asset.subtype)))
      .sort((left, right) => assetSubtypeLabel(indexedAssets.find((asset) => asset.subtype === left)?.kind ?? "content", left).localeCompare(assetSubtypeLabel(indexedAssets.find((asset) => asset.subtype === right)?.kind ?? "content", right), "zh-CN")),
    [activeView, indexedAssets],
  );
  const viewCounts = useMemo(
    () => Object.fromEntries(libraryViews.map((view) => [view.id, indexedAssets.filter(view.predicate).length])),
    [indexedAssets, libraryViews],
  );
  const pendingCount = useMemo(
    () => indexedAssets.filter((asset) => asset.reviewStatus === "pending").length,
    [indexedAssets],
  );
  const workspaceCounts = useMemo(() => ({
    review: pendingCount,
    mine: indexedAssets.filter(isLocalCreatedAsset).length,
    following: indexedAssets.filter((asset) => followedAssetIds.includes(asset.id)).length,
    released: indexedAssets.filter((asset) => asset.publicationStatus === "published").length,
  }), [followedAssetIds, indexedAssets, pendingCount]);

  const filteredAssets = useMemo(
    () =>
      indexedAssets.filter((asset) => {
        if (!activeView.predicate(asset)) return false;
        if (workspaceFilter === "review" && asset.reviewStatus !== "pending") return false;
        if (workspaceFilter === "mine" && !isLocalCreatedAsset(asset)) return false;
        if (workspaceFilter === "following" && !followedAssetIds.includes(asset.id)) return false;
        if (workspaceFilter === "released" && asset.publicationStatus !== "published") return false;
        if (statusFilter !== "all" && asset.reviewStatus !== statusFilter) return false;
        if (typeFilter !== "all" && asset.subtype !== typeFilter) return false;
        if (activeViewId === "scenes") {
          if (sceneChapterFilter === "assigned" && !asset.sceneInfo?.assigned) return false;
          if (sceneChapterFilter === "unassigned" && asset.sceneInfo?.assigned) return false;
          if (sceneCoverageFilter === "ready" && (asset.sceneInfo?.missing ?? 0) > 0) return false;
          if (sceneCoverageFilter === "missing" && (asset.sceneInfo?.missing ?? 0) === 0) return false;
        }
        return matchesSearch(asset, search);
      }),
    [activeView, activeViewId, followedAssetIds, indexedAssets, sceneChapterFilter, sceneCoverageFilter, search, statusFilter, typeFilter, workspaceFilter],
  );
  const tableAssets = useMemo(
    () => filteredAssets.slice(0, visibleLimit),
    [filteredAssets, visibleLimit],
  );

  useEffect(() => {
    setSelectedAssetId((current) =>
      current && filteredAssets.some((asset) => asset.id === current)
        ? current
        : (filteredAssets[0]?.id ?? null),
    );
  }, [filteredAssets]);

  const columns = useMemo<ColumnDef<GameAsset>[]>(
    () => [
      {
        id: "select",
        size: 38,
        header: ({ table }) => (
          <input
            aria-label="选择当前列表全部资产"
            type="checkbox"
            checked={table.getIsAllRowsSelected()}
            ref={(node) => {
              if (node) node.indeterminate = table.getIsSomeRowsSelected();
            }}
            onChange={table.getToggleAllRowsSelectedHandler()}
          />
        ),
        cell: ({ row }) => (
          <input
            aria-label={`选择 ${row.original.name}`}
            type="checkbox"
            checked={row.getIsSelected()}
            onChange={row.getToggleSelectedHandler()}
            onClick={(event) => event.stopPropagation()}
          />
        ),
      },
      {
        id: "asset",
        header: "资产 Key",
        cell: ({ row }) => (
          <button
            className="asset-cell asset-cell-button"
            type="button"
            onClick={() => setSelectedAssetId(row.original.id)}
            aria-label={`查看 ${row.original.name}`}
          >
            <AssetPreview asset={row.original} />
            <div className="asset-copy">
              <strong>{row.original.key}</strong>
              <span>{row.original.name}</span>
              <small title={row.original.subtype}>{row.original.subtypeLabel}</small>
            </div>
          </button>
        ),
      },
      {
        accessorKey: "reviewStatus",
        header: "状态",
        size: 116,
        cell: ({ row, getValue }) => {
          const status = getValue<ReviewStatus>();
          return (
            <div className="asset-stage-cell">
              <span className={`production-stage ${row.original.productionStage}`}>
                {productionStageCopy[row.original.productionStage] ?? row.original.productionStage}
              </span>
              <small>{statusCopy[status]}</small>
            </div>
          );
        },
      },
      {
        id: "qa",
        header: activeViewId === "scenes" ? "章节 / 覆盖" : "硬 QA",
        size: 96,
        cell: ({ row }) => activeViewId === "scenes" ? (
          <div className={`scene-coverage-cell ${row.original.sceneInfo?.missing ? "warn" : "ready"}`}>
            <span>{row.original.sceneInfo?.chapterTitle ?? "未分章"}</span>
            <small>{row.original.sceneInfo?.ready ?? 0}/{row.original.sceneInfo?.required ?? 0} 就绪 · 缺失 {row.original.sceneInfo?.missing ?? 0}</small>
          </div>
        ) : row.original.revisionFormat !== "media" ? (
          <div className="qa-cell neutral"><span>不适用<small>非媒体资产</small></span></div>
        ) : (
          <div className={row.original.qaPassed === row.original.qaTotal ? "qa-cell pass" : "qa-cell warn"}>
            {row.original.qaPassed === row.original.qaTotal ? (
              <CheckCircle size={17} weight="fill" />
            ) : (
              <WarningCircle size={17} weight="fill" />
            )}
            <span>
              {row.original.qaPassed === row.original.qaTotal ? "通过" : "需复核"}
              <small>{row.original.qaPassed}/{row.original.qaTotal} 项检查</small>
            </span>
          </div>
        ),
      },
      {
        accessorKey: "updatedAt",
        header: "更新于",
        size: 108,
        cell: ({ row }) => (
          <div className="updated-cell">
            <span>{row.original.updatedLabel}</span>
            <small>by {row.original.updatedBy}</small>
          </div>
        ),
      },
    ],
    [activeViewId],
  );

  const table = useReactTable({
    data: tableAssets,
    columns,
    state: { rowSelection, sorting },
    enableRowSelection: true,
    onRowSelectionChange: setRowSelection,
    onSortingChange: setSorting,
    getRowId: (row) => row.id,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const selectedAsset = indexedAssets.find((asset) => asset.id === selectedAssetId) ?? null;
  useEffect(() => {
    if (!selectedAsset || selectedAsset.detailsLoaded) return;
    if (!selectedAsset.candidateRevisionId && !selectedAsset.approvedRevisionId) return;
    let cancelled = false;
    void fetchAssetDetails(selectedAsset)
      .then((details) => {
        if (cancelled) return;
        setAssets((current) => current.map((asset) => asset.id === details.id ? details : asset));
      })
      .catch((detailError: unknown) => {
        if (cancelled) return;
        setAssets((current) => current.map((asset) => asset.id === selectedAsset.id ? {
          ...asset,
          detailsLoaded: true,
          reviewReady: false,
          reviewBlockReason: detailError instanceof Error ? `资产详情加载失败：${detailError.message}` : "资产详情加载失败。",
        } : asset));
      });
    return () => { cancelled = true; };
  }, [selectedAsset?.detailsLoaded, selectedAsset?.id]);
  const selectedIds = Object.entries(rowSelection).filter(([, selected]) => selected).map(([id]) => id);
  const selectedAssets = indexedAssets.filter((asset) => selectedIds.includes(asset.id));
  const selectionReviewable =
    selectedIds.length > 0 &&
    selectedAssets.every(
      (asset) => Boolean(asset.candidateRevisionId) && asset.reviewReady === true,
    );

  const reviewAssets = async (
    decision: "approve" | "reject" | "regenerate",
    explicitIds?: string[],
  ) => {
    const ids = explicitIds ?? selectedIds;
    if (ids.length === 0) {
      setToast("请先选择要处理的资产。");
      return;
    }
    if (decision === "regenerate") {
      if (!job.planId) {
        setToast("当前没有可检查的生产运行，请先建立生成计划。");
        return;
      }
      setRunFocusAssetIds(ids);
      setRunInspectorPlanId(job.planId);
      return;
    }

    const targets = assets.filter((asset) => ids.includes(asset.id));
    const blocked = targets.filter(
      (asset) => !asset.candidateRevisionId || asset.reviewReady !== true,
    );
    if (blocked.length > 0) {
      setToast(
        blocked.length === 1
          ? blocked[0].reviewBlockReason ?? "真实候选修订尚未加载，审核已禁用。"
          : `${blocked.length} 项资产缺少可审核的真实候选或 QA 证据。`,
      );
      return;
    }

    const revisionToAsset = new Map(
      targets.map((asset) => [asset.candidateRevisionId as string, asset.id]),
    );
    setReviewBusy(true);
    try {
      const result = await submitReview(Array.from(revisionToAsset.keys()), decision);
      const succeededAssetIds = new Set(
        result.succeeded
          .map((revisionId) => revisionToAsset.get(revisionId))
          .filter((assetId): assetId is string => Boolean(assetId)),
      );
      if (succeededAssetIds.size > 0) {
        const nextStatus: ReviewStatus = decision === "approve" ? "approved" : "rejected";
        setAssets((current) =>
          current.map((asset) =>
            succeededAssetIds.has(asset.id)
              ? {
                  ...asset,
                  reviewStatus: nextStatus,
                  reviewReady: false,
                  candidateRevisionId: undefined,
                  reviewBlockReason:
                    decision === "approve"
                      ? "该候选已批准。"
                      : "该候选已驳回，请生成新的候选。",
                }
              : asset,
          ),
        );
      }
      setRowSelection({});
      if (result.failed.length === 0) {
        setToast(`${decision === "approve" ? "已批准" : "已驳回"} ${result.succeeded.length} 项资产。`);
      } else {
        setToast(
          `${result.succeeded.length} 项成功，${result.failed.length} 项失败：${result.failed[0].message}`,
        );
      }
      if (result.succeeded.length > 0) void refetch();
    } finally {
      setReviewBusy(false);
    }
  };

  const saveAssetRevision = async (asset: GameAsset, content: unknown) => {
    const updated = await createAssetRevision(asset, content);
    setAssets((current) => current.map((item) => item.id === updated.id ? updated : item));
    setToast(`已将 ${asset.name} 保存为新的候选修订。`);
  };

  const navigateToAsset = (assetId: string) => {
    const target = indexedAssets.find((asset) => asset.id === assetId);
    if (!target) return;
    const targetView = libraryViews.find((view) => view.id !== "all" && view.predicate(target));
    setActiveViewId(targetView?.id ?? "all");
    setSelectedAssetId(target.id);
  };

  const startGenerationSession = (seedAssetIds: string[] = []) => {
    if (!data.project.id || isError) return;
    setGenerationSessionId(null);
    setGenerationSeedAssetIds([...new Set(seedAssetIds)]);
    setGenerationCreateOnMount(true);
    navigateGeneration(null);
  };

  const openGenerationSession = (sessionId: string) => {
    setGenerationSessionId(sessionId);
    setGenerationSeedAssetIds([]);
    setGenerationCreateOnMount(false);
    navigateGeneration(sessionId);
  };

  const openGenerationCenter = () => {
    const sessions=generationConversationsQuery.data??[];
    const preferred=generationSessionId ?? sessions.find(item=>item.status==="running")?.id ?? sessions[0]?.id ?? null;
    navigateGeneration(preferred);
  };
  const confirmGenerationSurface = (_planId: string) => {
    setToast("生成批次已确认，进度和结果保留在当前会话。");
    void refetch(); void generationConversationsQuery.refetch();
  };

  const generationSessionRemoved = (sessionId: string) => {
    if (generationSessionId === sessionId) setGenerationSessionId(null);
    setGenerationCreateOnMount(false);
    setGenerationSeedAssetIds([]);
    void generationConversationsQuery.refetch();
  };

  const openNarrativeProduction = async (assetIds: string[]) => {
    const refreshed = await refetch();
    if (refreshed.data) {
      setAssets(refreshed.data.assets);
      setJob(refreshed.data.job);
    }
    startGenerationSession(assetIds);
  };

  const openNarrativeAsset = (assetId: string) => {
    setWorkspaceMode("assets");
    setNarrativeFocusSceneId(null);
    navigateToAsset(assetId);
  };

  const toggleFollowAsset = (asset: GameAsset) => {
    const alreadyFollowed = followedAssetIds.includes(asset.id);
    setFollowedAssetIds((current) => alreadyFollowed
      ? current.filter((id) => id !== asset.id)
      : [...current, asset.id]);
    setToast(alreadyFollowed ? `已取消关注「${asset.name}」。` : `已关注「${asset.name}」。`);
  };

  const selectWorkspace = (workspace: WorkspaceFilter) => {
    setWorkspaceFilter(workspace);
    // A workspace is a cross-library task view.  Start from the complete
    // catalogue so a pending media asset is not hidden by the default
    // “场景” library view (or by a previous type/coverage filter).
    setActiveViewId("all");
    setTypeFilter("all");
    setSceneChapterFilter("all");
    setSceneCoverageFilter("all");
    // A workspace shortcut owns the status predicate. Switching to another
    // shortcut clears the previous transient status so it cannot silently
    // hide otherwise valid assets.
    setStatusFilter(workspace === "review" ? "pending" : "all");
    if (workspace === "mine" && !indexedAssets.some(isLocalCreatedAsset)) {
      setToast("当前本机模式没有创建者信息，暂时无法筛选“我创建的”。");
    } else if (workspace === "following" && followedAssetIds.length === 0) {
      setToast("还没有关注资产，请先在检查器中点击星标。");
    } else if (workspace === "released" && !indexedAssets.some((asset) => asset.publicationStatus === "published")) {
      setToast("当前项目还没有已发布资产；请通过 Release 交付发布。");
    }
  };

  // Workspace shortcuts (for example “待审查”) temporarily narrow the
  // catalogue with a status predicate.  A catalogue navigation is a new
  // browsing context, so clear that transient predicate when the user picks
  // an asset-library view; otherwise a pending-only result can make a valid
  // category appear empty until the toolbar is manually reset.
  const selectLibraryView = (viewId: string) => {
    setActiveViewId(viewId);
    setStatusFilter("all");
    setWorkspaceFilter("all");
    setTypeFilter("all");
    setSceneChapterFilter("all");
    setSceneCoverageFilter("all");
  };

  const workspaceEmptyCopy = workspaceFilter === "review"
    ? { title: "没有待审查资产", detail: "所有候选都已处理，或当前项目没有待审候选。" }
    : workspaceFilter === "mine"
      ? indexedAssets.some((asset) => Boolean(asset.createdBy))
        ? { title: "没有由本机用户创建的资产", detail: "这里只显示带有明确创建者信息的资产。" }
        : { title: "当前项目没有创建者信息", detail: "本机 Catalog 尚未记录 created_by / author，因此不会猜测资产归属。" }
      : workspaceFilter === "following"
        ? { title: "还没有关注资产", detail: "打开任意资产，在右侧检查器点击星标即可关注。" }
        : workspaceFilter === "released"
          ? { title: "还没有已发布资产", detail: "只有进入不可变 Release 的资产会显示在这里。" }
          : null;

  return (
    <div className={`workbench-shell ${generationCenterOpen ? "generation-mode" : ""} ${workspaceMode === "narrative" ? "narrative-mode" : "asset-mode"}`}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><CrownSimple size={23} weight="duotone" /></span>
          <strong>{workspaceMode === "narrative" ? "Narrative Atlas" : "游戏资产制作台"}</strong>
        </div>
        <button className="project-switcher" type="button" disabled={isError} onClick={() => openProjectDialog(data.project.id ? data.project : null)}>
          {isError ? "项目状态不可用" : data.project.name} <CaretDown size={14} />
        </button>
        <nav className="workspace-mode-switch" aria-label="工作台视图">
          <button type="button" className={!generationCenterOpen && workspaceMode === "assets" ? "active" : ""} aria-pressed={!generationCenterOpen && workspaceMode === "assets"} onClick={() => {navigateGeneration(null,false); setNarrativeFocusSceneId(null); setWorkspaceMode("assets"); }}><SquaresFour size={16} /> 资产制作台</button>
          <button type="button" className={!generationCenterOpen && workspaceMode === "narrative" ? "active" : ""} aria-pressed={!generationCenterOpen && workspaceMode === "narrative"} onClick={() => {navigateGeneration(null,false);setWorkspaceMode("narrative");}}><TreeStructure size={16} /> 叙事地图</button>
        </nav>
        <button ref={generationCenterTriggerRef} className="topbar-action generation-center-action" type="button" onClick={openGenerationCenter} aria-label="生成中心" disabled={!data.project.id || isError}>
          <Sparkle size={17} weight="fill" /><span>生成中心</span>{generationActivityCount > 0 && <b>{generationActivityCount}</b>}
        </button>
        <div className="topbar-spacer" />
        <div className={`connection-state ${isError ? "error" : isLoading ? "connecting" : "online"}`}>
          <span />
          <div>
            <strong>{isError ? "服务异常" : isLoading ? "正在连接" : "本地模式"}</strong>
            <small>
              {isError
                ? "无法连接本地后端"
                : isLoading
                  ? "正在读取项目"
                  : data.project.id
                    ? streamConnected
                      ? "SSE 已连接"
                      : "后端已连接"
                    : "等待创建 Project"}
            </small>
          </div>
        </div>
        <button className="icon-button" type="button" onClick={() => openProjectDialog()} aria-label="新建 Project" disabled={isError}><FolderOpen size={19} /></button>
        <button className="icon-button" type="button" onClick={() => setDeliveryOpen(true)} aria-label="Release 与游戏交付" disabled={!data.project.id || isError}><CloudArrowUp size={19} /></button>
        <button className="topbar-action" type="button" onClick={() => { setSystemSettingsOpen(false); setProviderChannelsOpen(true); }} aria-label="供应商渠道"><SlidersHorizontal size={17} /><span>供应商渠道</span></button>
        <button className="topbar-action" type="button" onClick={() => { setProviderChannelsOpen(false); setSystemSettingsOpen(true); }} aria-label="系统设置"><GearSix size={19} /><span>系统设置</span></button>
        <div className="local-user"><span>本</span><strong>本机用户</strong></div>
      </header>

      <div className="workbench-main" style={generationCenterOpen ? { display: "none" } : undefined}>
        {workspaceMode === "narrative" ? (
          <NarrativeAtlas project={data.project} onOpenProduction={openNarrativeProduction} focusSceneId={narrativeFocusSceneId} onOpenAsset={openNarrativeAsset} />
        ) : <>
        <nav className="sidebar" aria-label="资产导航">
          <div className="sidebar-scroll">
            <section className="project-section">
              <div className="sidebar-heading"><span>项目</span><button type="button" aria-label="新建 Project" onClick={() => openProjectDialog()}><Plus size={15} /></button></div>
              {isError ? (
                <div className="project-card project-card-error">
                  <WarningCircle size={18} weight="fill" />
                  <span><strong>项目状态不可用</strong><small>本地后端连接失败</small></span>
                </div>
              ) : data.project.id ? (
                <button className={`project-card ${data.project.thumbnail ? "" : "no-thumbnail"}`} type="button" onClick={() => openProjectDialog(data.project)} aria-label={`编辑项目 ${data.project.name}`} title="编辑项目名称">
                  {data.project.thumbnail && <img src={data.project.thumbnail} alt="项目缩略图" />}
                  <span><strong>{data.project.name}</strong><small>{data.project.path}</small><em>{data.project.branch ?? "local"}</em></span>
                  <PencilSimple className="project-edit-icon" size={15} aria-hidden="true" />
                </button>
              ) : (
                <button className="project-card project-card-empty" type="button" onClick={() => openProjectDialog()}>
                  <span><strong>尚无 Project</strong><small>Game-Projects 中没有可用项目</small></span>
                </button>
              )}
              <button className="import-entry" type="button" onClick={() => openProjectDialog()} disabled={isError}><Plus size={16} /> 新建 Project</button>
            </section>

            <section className="nav-section">
              <div className="sidebar-heading">
                <button className="sidebar-heading-link" type="button" onClick={() => selectLibraryView("all")} aria-label="打开资产库">
                  <span>资产库</span>
                </button>
              </div>
              <ul>
                <li>
                  <button className={activeViewId === "all" ? "active" : ""} type="button" onClick={() => selectLibraryView("all")}>
                    <SquaresFour size={18} /> <span>全部资产</span><small>{viewCounts.all ?? indexedAssets.length}</small>
                  </button>
                </li>
              </ul>
              {libraryGroups.map((group) => (
                <div className="library-group" key={group.id}>
                  <div className="library-group-label">{group.label}</div>
                  <ul>
                  {group.views.map((item) => {
                    const Icon = item.icon;
                    return (
                      <li key={item.id}>
                        <button
                        className={activeViewId === item.id ? "active" : ""}
                        type="button"
                        onClick={() => selectLibraryView(item.id)}
                      >
                        <Icon size={18} /> <span>{item.label}</span><small>{viewCounts[item.id] ?? 0}</small>
                      </button>
                    </li>
                  );
                  })}
                  </ul>
                </div>
              ))}
            </section>

            <section className="nav-section workspace-nav">
              <div className="sidebar-heading"><span>工作区</span></div>
              <ul>
                {workspaceItems.map((item) => {
                  const Icon = item.icon;
                  return (
                    <li key={item.id}>
                      <button
                        className={workspaceFilter === item.id ? "active" : ""}
                        type="button"
                        aria-label={item.id === "review" ? `${item.label} ${workspaceCounts[item.id]}` : item.label}
                        aria-pressed={workspaceFilter === item.id}
                        onClick={() => selectWorkspace(item.id)}
                      >
                        <Icon size={18} /> <span>{item.label}</span><small aria-hidden="true" className={item.id === "review" && workspaceCounts[item.id] > 0 ? "alert-count" : "workspace-count"}>{workspaceCounts[item.id]}</small>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>

            <section className="nav-section version-nav">
              <div className="sidebar-heading"><span>版本控制</span></div>
              <ul>
                <li><button type="button" onClick={() => setVersionControlOpen(true)} title="查看 Git 提交边界"><GitBranch size={18} /><span>本地提交</span><small className="nav-item-note">说明</small></button></li>
                <li><button type="button" onClick={() => setVersionControlOpen(true)} title="查看 Git 同步边界"><UploadSimple size={18} /><span>拉取 / 推送</span><small className="nav-item-note">说明</small></button></li>
                <li><button type="button" onClick={() => setDeliveryOpen(true)} disabled={!data.project.id || isError}><CloudArrowUp size={18} /><span>Release 交付</span></button></li>
              </ul>
            </section>
          </div>
        </nav>

        <main className="batch-panel">
          <header className="batch-heading">
            <div>
              <span className="section-kicker">资产生产批次</span>
              <h1>批量审查 <span>· {job.status === "awaiting_user" ? "等待人工" : job.status === "running" || job.status === "queued" ? "生产中" : "已同步"}</span> <CheckCircle size={21} weight="fill" /></h1>
            </div>
          </header>

          <div className="toolbar">
            <div className="select-control">
              <SlidersHorizontal size={16} />
              <SelectMenu
                ariaLabel="按审查状态筛选"
                value={statusFilter}
                options={[
                  { value: "all", label: "全部状态" },
                  { value: "pending", label: "待审查" },
                  { value: "approved", label: "已审查" },
                  { value: "rejected", label: "已驳回" },
                  { value: "generating", label: "生成中" },
                ]}
                onChange={(value) => {
                  setStatusFilter(value);
                  // Choosing a status in the toolbar is an explicit filter,
                  // so it should not silently remain nested inside a
                  // workspace shortcut such as “待审查”.
                  setWorkspaceFilter("all");
                }}
              />
            </div>
            <div className="select-control asset-type-filter">
              <SelectMenu
                ariaLabel="按资产类型筛选"
                value={typeFilter}
                options={[
                  { value: "all", label: "资产类型" },
                  ...subtypes.map((subtype) => {
                    const matching = indexedAssets.find((asset) => asset.subtype === subtype && activeView.predicate(asset));
                    return { value: subtype, label: assetSubtypeLabel(matching?.kind ?? "content", subtype) };
                  }),
                ]}
                onChange={setTypeFilter}
              />
            </div>
            {activeViewId === "scenes" && <>
              <div className="select-control scene-filter-control">
                <SelectMenu
                  ariaLabel="按章节归属筛选"
                  value={sceneChapterFilter}
                  options={[
                    { value: "all", label: "全部章节" },
                    { value: "assigned", label: "已入章" },
                    { value: "unassigned", label: "未分章" },
                  ]}
                  onChange={(value) => setSceneChapterFilter(value as typeof sceneChapterFilter)}
                />
              </div>
              <div className="select-control scene-filter-control">
                <SelectMenu
                  ariaLabel="按场景覆盖筛选"
                  value={sceneCoverageFilter}
                  options={[
                    { value: "all", label: "全部覆盖" },
                    { value: "ready", label: "已就绪" },
                    { value: "missing", label: "有缺失" },
                  ]}
                  onChange={(value) => setSceneCoverageFilter(value as typeof sceneCoverageFilter)}
                />
              </div>
            </>}
            <label className="search-control">
              <MagnifyingGlass size={17} />
              <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索资产 Key / 名称 / 标签" />
              {search && <button type="button" onClick={() => setSearch("")} aria-label="清除搜索"><X size={15} /></button>}
            </label>
            <div className="view-switch" aria-label="显示密度">
              <button type="button" aria-pressed={viewMode === "thumbnails"} className={viewMode === "thumbnails" ? "active" : ""} onClick={() => setViewMode("thumbnails")}><SquaresFour size={17} /> 缩略图</button>
              <button type="button" aria-pressed={viewMode === "compact"} className={viewMode === "compact" ? "active" : ""} onClick={() => setViewMode("compact")}><ListBullets size={17} /> 列表</button>
            </div>
            <div className="select-control sort-control">
              <SelectMenu
                ariaLabel="资产排序"
                value={sorting[0]?.id ?? "updatedAt"}
                options={[
                  { value: "updatedAt", label: "最近优先" },
                  { value: "reviewStatus", label: "按状态" },
                ]}
                onChange={(value) => setSorting([{ id: value, desc: true }])}
              />
            </div>
            <button className="icon-button toolbar-more" type="button" aria-label="更多批次操作"><DotsThree size={19} /></button>
          </div>

          <div className="selection-summary">
            <span>{isLoading ? "正在扫描资产…" : `已选择 ${selectedIds.length} 项`}</span>
            {workspaceFilter !== "all" && <span className="workspace-filter-chip"><span>工作区：{workspaceItems.find((item) => item.id === workspaceFilter)?.label}</span><button type="button" onClick={() => selectLibraryView(activeViewId)} aria-label="清除工作区筛选">清除</button></span>}
            <button type="button" onClick={() => setRowSelection({})}>清除选择</button>
            <small>{filteredAssets.length} 项结果</small>
          </div>

          <div className={`asset-table-wrap ${viewMode}`}>
            {isError ? (
              <div className="empty-assets error-state" role="alert">
                <WarningCircle size={34} weight="fill" />
                <strong>无法加载资产数据</strong>
                <p>{error instanceof Error ? error.message : "本地后端发生未知错误。"}</p>
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => void refetch()}
                  disabled={isFetching}
                >
                  {isFetching ? "正在重试…" : "重试连接"}
                </button>
              </div>
            ) : filteredAssets.length > 0 ? (
              <>
                <table className="asset-table">
                <thead>
                  {table.getHeaderGroups().map((headerGroup) => (
                    <tr key={headerGroup.id}>
                      {headerGroup.headers.map((header) => (
                        <th key={header.id} style={{ width: header.getSize() }}>
                          {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                        </th>
                      ))}
                    </tr>
                  ))}
                </thead>
                <tbody>
                  {table.getRowModel().rows.map((row) => (
                    <tr
                      key={row.id}
                      className={`${row.getIsSelected() ? "selected" : ""} ${selectedAssetId === row.original.id ? "inspected" : ""}`}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
                </table>
                {tableAssets.length < filteredAssets.length && (
                  <button
                    className="button secondary load-more-assets"
                    type="button"
                    onClick={() => setVisibleLimit((current) => current + 60)}
                  >
                    加载更多（尚有 {filteredAssets.length - tableAssets.length} 项）
                  </button>
                )}
              </>
            ) : (
              <div className="empty-assets">
                <FileText size={31} />
                <strong>{data.project.id ? workspaceEmptyCopy?.title ?? "这个筛选下没有资产" : "Game-Projects 中尚无 Project"}</strong>
                <p>{data.project.id ? workspaceEmptyCopy?.detail ?? "调整分类、状态或搜索内容。" : "新建 Project，或把有效 Project 放入 Game-Projects 后刷新。"}</p>
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => {
                    if (data.project.id) {
                      selectLibraryView(workspaceFilter === "all" ? "scenes" : "all");
                      setSearch("");
                    } else {
                      openProjectDialog();
                    }
                  }}
                >
                  {data.project.id ? workspaceFilter === "all" ? "查看场景" : "查看全部资产" : "新建 Project"}
                </button>
              </div>
            )}
          </div>

          <footer className="batch-actions">
            <label><input type="checkbox" checked={table.getIsAllRowsSelected()} onChange={table.getToggleAllRowsSelectedHandler()} /> 全选本页</label>
            <span>已选择 {selectedIds.length} 项</span>
            <div />
            <button className="button primary" type="button" disabled={!selectionReviewable || reviewBusy} onClick={() => reviewAssets("approve")}>批量批准版本</button>
            <button className="button secondary" type="button" disabled={!selectionReviewable || reviewBusy} onClick={() => reviewAssets("reject")}>驳回</button>
            <button className="button secondary" type="button" disabled={selectedIds.length === 0 || !job.planId || reviewBusy} onClick={() => reviewAssets("regenerate")}>需要重做</button>
            <button className="icon-button" type="button" aria-label="更多审核操作"><DotsThree size={19} /></button>
          </footer>
        </main>

        <Inspector
          asset={selectedAsset}
          allAssets={indexedAssets}
          followed={selectedAsset ? followedAssetIds.includes(selectedAsset.id) : false}
          onClose={() => setSelectedAssetId(null)}
          onEdit={(asset) => setEditingAssetId(asset.id)}
          onNavigate={navigateToAsset}
          onOpenNarrative={(sceneAssetId) => { setNarrativeFocusSceneId(sceneAssetId); setWorkspaceMode("narrative"); }}
          onReview={reviewAssets}
          onToggleFollow={toggleFollowAsset}
        />
        </>}
      </div>

      <AssetEditorDrawer
        asset={indexedAssets.find((asset) => asset.id === editingAssetId) ?? null}
        allAssets={indexedAssets}
        onClose={() => setEditingAssetId(null)}
        onSave={saveAssetRevision}
      />

      {generationCenterOpen ? (
        <Suspense fallback={<div className="generation-drawer-loading" role="status">正在打开生成中心…</div>}>
          <GenerationChatPage
            sessionId={generationSessionId}
            seedAssetIds={generationSeedAssetIds}
            createOnMount={generationCreateOnMount}
            open
            onClose={() => navigateGeneration(null,false)}
            onNewSession={() => startGenerationSession()}
            onSelectSession={openGenerationSession}
            onSessionCreated={(sessionId) => {
              navigateGeneration(sessionId);
              setGenerationCreateOnMount(false);
              setGenerationSeedAssetIds([]);
              void generationConversationsQuery.refetch();
            }}
            onOpenAsset={(assetId)=>{navigateGeneration(null,false);setWorkspaceMode("assets");navigateToAsset(assetId);}}
            onConfirmed={confirmGenerationSurface}
            onSessionRemoved={generationSessionRemoved}
            workbenchData={data}
          />
        </Suspense>
      ) : null}

      {!generationCenterOpen && <footer className="task-strip" aria-label="后台任务">
        <div className="task-label"><strong>近期任务</strong><span /></div>
        <div className="task-copy">
          <strong>{isError ? "后台任务服务不可用" : job.name}</strong>
          <span>{isError ? "等待重新连接" : job.status === "completed" ? "已完成" : job.status === "awaiting_user" ? "等待人工决定" : job.status === "credentials_locked" ? "等待凭据" : job.status === "qa_failed" ? "硬 QA 未通过" : job.status === "paused" ? "已暂停" : "处理中"}</span>
        </div>
        <span className={`job-status ${job.status}`}>{isError ? "错误" : job.status === "completed" ? "已完成" : job.status === "awaiting_user" ? "待人工" : job.status === "qa_failed" ? "QA 未通过" : job.status === "paused" ? "已暂停" : `${job.progress}%`}</span>
        <div className="job-previews">{job.previewImages.map((image, index) => <img key={`${image}-${index}`} src={image} alt="" />)}</div>
        <div className="job-metrics"><span>{isError ? "任务数据不可用" : `共生成 ${job.total} 项`}</span><strong>{isError ? "—" : `通过 QA ${job.passed} 项`}</strong></div>
        <div className="job-output"><small>输出位置</small><span title={job.outputPath}>{isError ? "—" : job.outputPath}</span></div>
        <button className="button secondary" type="button" disabled={!job.planId || isError} onClick={() => { if (job.planId) { setRunFocusAssetIds([]); setRunInspectorPlanId(job.planId); } }}><ListBullets size={17} /> 检查运行</button>
      </footer>}

      {runInspectorPlanId ? (
        <Suspense fallback={<div className="drawer-backdrop production-loading" role="status">正在载入运行检查器…</div>}>
          <RunInspectorDrawer
            open
            planId={runInspectorPlanId}
            focusAssetIds={runFocusAssetIds}
            onClose={() => setRunInspectorPlanId(null)}
            onChanged={() => void refetch()}
          />
        </Suspense>
      ) : null}

      <ProviderChannelsDrawer open={providerChannelsOpen} onClose={() => setProviderChannelsOpen(false)} />
      <SystemSettingsDrawer open={systemSettingsOpen} onClose={() => setSystemSettingsOpen(false)} />
      <DeliveryDrawer
        open={deliveryOpen}
        projectId={data.project.id}
        onClose={() => setDeliveryOpen(false)}
        onMessage={setToast}
      />
      <VersionControlDrawer
        open={versionControlOpen}
        project={data.project}
        onClose={() => setVersionControlOpen(false)}
      />
      <ProjectDialog
        open={projectDialogOpen}
        project={projectToEdit}
        onClose={closeProjectDialog}
        onSaved={async () => {
          await refetch();
        }}
      />
      <div className={`toast ${toast ? "visible" : ""}`} role="status" aria-live="polite">{toast}</div>
    </div>
  );
}
