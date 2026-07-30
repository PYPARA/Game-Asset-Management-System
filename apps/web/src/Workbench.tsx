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
  CaretDown,
  CheckCircle,
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
  SquaresFour,
  Star,
  Tray,
  UploadSimple,
  UserCircle,
  UsersThree,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { ProjectDialog } from "./components/ProjectDialog";
import { AssetEditorDrawer } from "./components/AssetEditorDrawer";
import { Inspector } from "./components/Inspector";
import { SettingsDrawer } from "./components/SettingsDrawer";
import {
  emptyWorkbenchPayload,
  createAssetRevision,
  fetchAssetDetails,
  fetchGenerationProviders,
  fetchWorkbench,
  refreshProviderModels,
  submitReview,
  subscribeToJobEvents,
  unlockProvider,
} from "./lib/api";
import { readCredential } from "./lib/credentials";
import { assetSubtypeLabel } from "./lib/labels";
import type { GameAsset, JobSummary, ProjectSummary, ReviewStatus } from "./types";

const GenerationPlanDrawer = lazy(() =>
  import("./components/GenerationPlanDrawer").then((module) => ({
    default: module.GenerationPlanDrawer,
  })),
);
const RunInspectorDrawer = lazy(() =>
  import("./components/RunInspectorDrawer").then((module) => ({
    default: module.RunInspectorDrawer,
  })),
);

const libraryItems = [
  { id: "content", label: "叙事内容", icon: Article },
  { id: "design", label: "设计文档", icon: Notebook },
  { id: "characters", label: "角色", icon: UsersThree },
  { id: "items", label: "物品", icon: Package },
  { id: "locations", label: "地点", icon: MapPin },
  { id: "achievements", label: "成就", icon: SealCheck },
  { id: "2d-media", label: "2D 媒体", icon: ImagesSquare },
  { id: "production", label: "生产资料", icon: Palette },
  { id: "audio", label: "音频", icon: SpeakerHigh },
];

const workspaceItems = [
  { id: "review", label: "待审查", icon: Tray },
  { id: "mine", label: "我创建的", icon: UserCircle },
  { id: "following", label: "已关注", icon: Star },
  { id: "released", label: "已发布", icon: SealCheck },
];

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

function modelCacheIsStale(refreshedAt: string | null): boolean {
  if (!refreshedAt) return true;
  const refreshed = Date.parse(refreshedAt);
  return !Number.isFinite(refreshed) || Date.now() - refreshed > 24 * 60 * 60 * 1000;
}

async function unlockActiveProviderCredentials(): Promise<void> {
  const providers = (await fetchGenerationProviders()).filter((provider) => provider.is_active);
  await Promise.allSettled(providers.map(async (provider) => {
    if (provider.kind !== "fake") {
      const apiKey = await readCredential(provider.id);
      if (!apiKey) return;
      await unlockProvider(provider.id, apiKey);
    }
    if (provider.models.length === 0 || modelCacheIsStale(provider.models_refreshed_at)) {
      await refreshProviderModels(provider.id);
    }
  }));
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
  const [assets, setAssets] = useState<GameAsset[]>([]);
  const [job, setJob] = useState<JobSummary>(emptyWorkbenchPayload.job);
  const [streamConnected, setStreamConnected] = useState(false);
  const [activeCategory, setActiveCategory] = useState("2d-media");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [viewMode, setViewMode] = useState<"thumbnails" | "compact">("thumbnails");
  const [sorting, setSorting] = useState<SortingState>([{ id: "updatedAt", desc: true }]);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
  const [projectToEdit, setProjectToEdit] = useState<ProjectSummary | null>(null);
  const [toast, setToast] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(60);
  const [editingAssetId, setEditingAssetId] = useState<string | null>(null);
  const [planDrawerOpen, setPlanDrawerOpen] = useState(false);
  const [planSeedAssetIds, setPlanSeedAssetIds] = useState<string[]>([]);
  const [runInspectorPlanId, setRunInspectorPlanId] = useState<string | null>(null);
  const [runFocusAssetIds, setRunFocusAssetIds] = useState<string[]>([]);

  useEffect(() => {
    if (isError || isLoading) {
      if (isError) providerBootstrapStarted.current = false;
      return;
    }
    if (providerBootstrapStarted.current) return;
    providerBootstrapStarted.current = true;
    void unlockActiveProviderCredentials().catch(() => {
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
    setSelectedAssetId((current) =>
      current && data.assets.some((asset) => asset.id === current && asset.category === activeCategory)
        ? current
        : (data.assets.find((asset) => asset.category === activeCategory)?.id ?? null),
    );
  }, [activeCategory, data.assets, data.job]);

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
  }, [activeCategory, search, statusFilter, typeFilter]);

  useEffect(() => setTypeFilter("all"), [activeCategory]);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(""), 3_400);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  const subtypes = useMemo(
    () => Array.from(new Set(assets.filter((asset) => activeCategory === "all" || asset.category === activeCategory).map((asset) => asset.subtype)))
      .sort((left, right) => assetSubtypeLabel(assets.find((asset) => asset.subtype === left)?.kind ?? "content", left).localeCompare(assetSubtypeLabel(assets.find((asset) => asset.subtype === right)?.kind ?? "content", right), "zh-CN")),
    [activeCategory, assets],
  );
  const categoryCounts = useMemo(
    () =>
      Object.fromEntries(
        libraryItems.map((item) => [
          item.id,
          assets.filter((asset) => asset.category === item.id).length,
        ]),
      ),
    [assets],
  );
  const pendingCount = useMemo(
    () => assets.filter((asset) => asset.reviewStatus === "pending").length,
    [assets],
  );

  const filteredAssets = useMemo(
    () =>
      assets.filter((asset) => {
        if (activeCategory !== "all" && asset.category !== activeCategory) return false;
        if (statusFilter !== "all" && asset.reviewStatus !== statusFilter) return false;
        if (typeFilter !== "all" && asset.subtype !== typeFilter) return false;
        return matchesSearch(asset, search);
      }),
    [activeCategory, assets, search, statusFilter, typeFilter],
  );
  const tableAssets = useMemo(
    () => filteredAssets.slice(0, visibleLimit),
    [filteredAssets, visibleLimit],
  );

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
        header: "硬 QA",
        size: 96,
        cell: ({ row }) => row.original.kind !== "media" && row.original.kind !== "production" ? (
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
    [],
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

  const selectedAsset = assets.find((asset) => asset.id === selectedAssetId) ?? null;
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
  const selectedAssets = assets.filter((asset) => selectedIds.includes(asset.id));
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
    const target = assets.find((asset) => asset.id === assetId);
    if (!target) return;
    setActiveCategory(target.category);
    setSelectedAssetId(target.id);
  };

  const openPlanDrawer = (assetIds: string[]) => {
    setPlanSeedAssetIds(assetIds);
    setPlanDrawerOpen(true);
  };

  return (
    <div className="workbench-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><CrownSimple size={23} weight="duotone" /></span>
          <strong>游戏资产制作台</strong>
        </div>
        <button className="project-switcher" type="button" disabled={isError} onClick={() => openProjectDialog(data.project.id ? data.project : null)}>
          {isError ? "项目状态不可用" : data.project.name} <CaretDown size={14} />
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
        <button className="icon-button" type="button" onClick={() => setSettingsOpen(true)} aria-label="供应商设置"><GearSix size={19} /></button>
        <div className="local-user"><span>本</span><strong>本机用户</strong></div>
      </header>

      <div className="workbench-main">
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
              <div className="sidebar-heading"><span>资产库</span></div>
              <ul>
                {libraryItems.map((item) => {
                  const Icon = item.icon;
                  return (
                    <li key={item.id}>
                      <button
                        className={activeCategory === item.id ? "active" : ""}
                        type="button"
                        onClick={() => setActiveCategory(item.id)}
                      >
                        <Icon size={18} /> <span>{item.label}</span><small>{categoryCounts[item.id] ?? 0}</small>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>

            <section className="nav-section workspace-nav">
              <div className="sidebar-heading"><span>工作区</span></div>
              <ul>
                {workspaceItems.map((item) => {
                  const Icon = item.icon;
                  return (
                    <li key={item.id}>
                      <button type="button" onClick={() => item.id === "review" && setStatusFilter("pending")}>
                        <Icon size={18} /> <span>{item.label}</span>{item.id === "review" && pendingCount > 0 && <small className="alert-count">{pendingCount}</small>}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>

            <section className="nav-section version-nav">
              <div className="sidebar-heading"><span>版本控制</span></div>
              <ul>
                <li><button type="button"><GitBranch size={18} /><span>本地提交</span></button></li>
                <li><button type="button"><UploadSimple size={18} /><span>拉取 / 推送</span></button></li>
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
            <button
              className="button secondary generate-button"
              type="button"
              disabled={!data.project.id || isError}
              onClick={() => openPlanDrawer(selectedIds)}
            ><Plus size={17} /> 新建生成计划</button>
          </header>

          <div className="toolbar">
            <label className="select-control">
              <SlidersHorizontal size={16} />
              <select aria-label="按审查状态筛选" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                <option value="all">全部状态</option>
                <option value="pending">待审查</option>
                <option value="approved">已审查</option>
                <option value="rejected">已驳回</option>
                <option value="generating">生成中</option>
              </select>
            </label>
            <label className="select-control asset-type-filter">
              <select aria-label="按资产类型筛选" value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>
                <option value="all">资产类型</option>
                {subtypes.map((subtype) => {
                  const matching = assets.find((asset) => asset.subtype === subtype && (activeCategory === "all" || asset.category === activeCategory));
                  return <option key={subtype} value={subtype}>{assetSubtypeLabel(matching?.kind ?? "content", subtype)}</option>;
                })}
              </select>
            </label>
            <label className="search-control">
              <MagnifyingGlass size={17} />
              <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索资产 Key / 名称 / 标签" />
              {search && <button type="button" onClick={() => setSearch("")} aria-label="清除搜索"><X size={15} /></button>}
            </label>
            <div className="view-switch" aria-label="显示密度">
              <button type="button" aria-pressed={viewMode === "thumbnails"} className={viewMode === "thumbnails" ? "active" : ""} onClick={() => setViewMode("thumbnails")}><SquaresFour size={17} /> 缩略图</button>
              <button type="button" aria-pressed={viewMode === "compact"} className={viewMode === "compact" ? "active" : ""} onClick={() => setViewMode("compact")}><ListBullets size={17} /> 列表</button>
            </div>
            <label className="select-control sort-control">
              <select
                aria-label="资产排序"
                value={sorting[0]?.id ?? "updatedAt"}
                onChange={(event) => setSorting([{ id: event.target.value, desc: true }])}
              >
                <option value="updatedAt">最近优先</option>
                <option value="reviewStatus">按状态</option>
              </select>
            </label>
            <button className="icon-button toolbar-more" type="button" aria-label="更多批次操作"><DotsThree size={19} /></button>
          </div>

          <div className="selection-summary">
            <span>{isLoading ? "正在扫描资产…" : `已选择 ${selectedIds.length} 项`}</span>
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
                <strong>{data.project.id ? "这个筛选下没有资产" : "Game-Projects 中尚无 Project"}</strong>
                <p>{data.project.id ? "调整分类、状态或搜索内容。" : "新建 Project，或把有效 Project 放入 Game-Projects 后刷新。"}</p>
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => {
                    if (data.project.id) {
                      setActiveCategory("2d-media");
                      setStatusFilter("all");
                      setSearch("");
                    } else {
                      openProjectDialog();
                    }
                  }}
                >
                  {data.project.id ? "查看 2D 媒体" : "新建 Project"}
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
          allAssets={assets}
          onClose={() => setSelectedAssetId(null)}
          onEdit={(asset) => setEditingAssetId(asset.id)}
          onNavigate={navigateToAsset}
          onReview={reviewAssets}
        />
      </div>

      <AssetEditorDrawer
        asset={assets.find((asset) => asset.id === editingAssetId) ?? null}
        allAssets={assets}
        onClose={() => setEditingAssetId(null)}
        onSave={saveAssetRevision}
      />

      <footer className="task-strip" aria-label="后台任务">
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
      </footer>

      {planDrawerOpen ? (
        <Suspense fallback={<div className="drawer-backdrop production-loading" role="status">正在载入计划编辑器…</div>}>
          <GenerationPlanDrawer
            open
            project={data.project}
            assets={assets}
            initialAssetIds={planSeedAssetIds}
            onClose={() => setPlanDrawerOpen(false)}
            onConfirmed={(planId) => {
              setPlanDrawerOpen(false);
              setRunFocusAssetIds(planSeedAssetIds);
              setRunInspectorPlanId(planId);
              setToast("生成计划已确认并进入持久队列。");
              void refetch();
            }}
          />
        </Suspense>
      ) : null}
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

      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
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
