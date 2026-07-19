import { useEffect, useMemo, useState } from "react";
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
import { ImportDialog } from "./components/ImportDialog";
import { Inspector } from "./components/Inspector";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { demoPayload } from "./demo-data";
import { fetchWorkbench, submitReview, subscribeToJobEvents } from "./lib/api";
import type { GameAsset, JobSummary, ReviewStatus } from "./types";

const libraryItems = [
  { id: "content", label: "叙事文档", count: 48, icon: Article },
  { id: "design", label: "设计文档", count: 31, icon: Notebook },
  { id: "characters", label: "角色", count: 132, icon: UsersThree },
  { id: "items", label: "物品", count: 512, icon: Package },
  { id: "locations", label: "地点", count: 86, icon: MapPin },
  { id: "2d-media", label: "2D 媒体", count: 342, icon: ImagesSquare },
  { id: "materials", label: "材质", count: 184, icon: Palette },
  { id: "audio", label: "音频", count: 97, icon: SpeakerHigh },
];

const workspaceItems = [
  { id: "review", label: "待审查", count: 24, icon: Tray },
  { id: "mine", label: "我创建的", icon: UserCircle },
  { id: "following", label: "已关注", icon: Star },
  { id: "released", label: "已发布", icon: SealCheck },
];

const statusCopy: Record<ReviewStatus, string> = {
  pending: "待审查",
  approved: "已审查",
  rejected: "已驳回",
  generating: "生成中",
};

function matchesSearch(asset: GameAsset, search: string) {
  const value = search.trim().toLocaleLowerCase();
  if (!value) return true;
  return [asset.key, asset.name, asset.subtype, ...asset.tags]
    .join(" ")
    .toLocaleLowerCase()
    .includes(value);
}

function AssetPreview({ asset }: { asset: GameAsset }) {
  return (
    <div className={`asset-preview ${asset.thumbnails.length === 1 ? "wide" : ""}`}>
      {asset.thumbnails.map((image, index) => (
        <img key={`${image}-${index}`} src={image} alt="" loading="lazy" />
      ))}
    </div>
  );
}

export function Workbench() {
  const { data = demoPayload, isLoading } = useQuery({
    queryKey: ["workbench"],
    queryFn: fetchWorkbench,
  });
  const [assets, setAssets] = useState<GameAsset[]>(demoPayload.assets);
  const [job, setJob] = useState<JobSummary>(demoPayload.job);
  const [streamConnected, setStreamConnected] = useState(false);
  const [activeCategory, setActiveCategory] = useState("2d-media");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [viewMode, setViewMode] = useState<"thumbnails" | "compact">("thumbnails");
  const [sorting, setSorting] = useState<SortingState>([{ id: "updatedAt", desc: true }]);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(demoPayload.assets[0]?.id ?? null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [toast, setToast] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(60);

  useEffect(() => {
    setAssets(data.assets);
    setJob(data.job);
    setSelectedAssetId((current) =>
      current && data.assets.some((asset) => asset.id === current) ? current : (data.assets[0]?.id ?? null),
    );
  }, [data.assets, data.job]);

  useEffect(() => {
    if (data.source !== "api" || !data.project.id) {
      setStreamConnected(false);
      return;
    }
    return subscribeToJobEvents(
      data.project.id,
      (event) => setJob((current) => ({ ...current, ...event })),
      setStreamConnected,
    );
  }, [data.project.id, data.source]);

  useEffect(() => {
    setRowSelection({});
    setVisibleLimit(60);
  }, [activeCategory, search, statusFilter, typeFilter]);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(""), 3_400);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  const subtypes = useMemo(
    () => Array.from(new Set(assets.map((asset) => asset.subtype))).sort(),
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
              <small>{row.original.subtype}</small>
            </div>
          </button>
        ),
      },
      {
        accessorKey: "reviewStatus",
        header: "状态",
        size: 92,
        cell: ({ getValue }) => {
          const status = getValue<ReviewStatus>();
          return <span className={`status-badge ${status}`}>{statusCopy[status]}</span>;
        },
      },
      {
        id: "qa",
        header: "硬 QA",
        size: 96,
        cell: ({ row }) => (
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
  const selectedIds = Object.entries(rowSelection).filter(([, selected]) => selected).map(([id]) => id);
  const selectedAssets = assets.filter((asset) => selectedIds.includes(asset.id));
  const selectionReviewable =
    selectedIds.length > 0 &&
    (data.source === "demo" ||
      selectedAssets.every(
        (asset) => Boolean(asset.candidateRevisionId) && asset.reviewReady === true,
      ));

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
      setToast("返工任务尚未接入生成计划；本次没有写入或入队。");
      return;
    }

    const targets = assets.filter((asset) => ids.includes(asset.id));
    if (data.source === "demo") {
      const nextStatus: ReviewStatus = decision === "approve" ? "approved" : "rejected";
      setAssets((current) =>
        current.map((asset) =>
          ids.includes(asset.id) ? { ...asset, reviewStatus: nextStatus } : asset,
        ),
      );
      setRowSelection({});
      setToast(`演示状态已更新 ${ids.length} 项；不会写入项目文件。`);
      return;
    }

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
    } finally {
      setReviewBusy(false);
    }
  };

  return (
    <div className="workbench-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><CrownSimple size={23} weight="duotone" /></span>
          <strong>游戏资产制作台</strong>
        </div>
        <button className="project-switcher" type="button">
          皇帝模拟器 <CaretDown size={14} />
        </button>
        <div className="topbar-spacer" />
        <div className={`connection-state ${data.source === "api" ? "online" : "demo"}`}>
          <span />
          <div><strong>{data.source === "api" ? "本地模式" : "离线演示"}</strong><small>{data.source === "api" ? (streamConnected ? "SSE 已连接" : "localhost") : "后端启动后自动切换"}</small></div>
        </div>
        <button className="icon-button" type="button" onClick={() => setImportOpen(true)} aria-label="导入项目"><FolderOpen size={19} /></button>
        <button className="icon-button" type="button" onClick={() => setSettingsOpen(true)} aria-label="供应商设置"><GearSix size={19} /></button>
        <div className="local-user"><span>本</span><strong>本机用户</strong></div>
      </header>

      <div className="workbench-main">
        <nav className="sidebar" aria-label="资产导航">
          <div className="sidebar-scroll">
            <section className="project-section">
              <div className="sidebar-heading"><span>项目</span><button type="button" aria-label="添加项目"><Plus size={15} /></button></div>
              <button className="project-card" type="button">
                <img src={data.project.thumbnail} alt="皇帝模拟器项目缩略图" />
                <span><strong>{data.project.name}</strong><small>{data.project.path}</small><em>{data.project.branch ?? "main"}</em></span>
              </button>
              <button className="import-entry" type="button" onClick={() => setImportOpen(true)}><UploadSimple size={16} /> 登记本地项目</button>
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
                        <Icon size={18} /> <span>{item.label}</span><small>{item.count}</small>
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
                        <Icon size={18} /> <span>{item.label}</span>{item.count && <small className="alert-count">{item.count}</small>}
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
              <span className="section-kicker">图像生成批次</span>
              <h1>批量审查 <span>· 已完成</span> <CheckCircle size={21} weight="fill" /></h1>
            </div>
            <button
              className="button secondary generate-button"
              type="button"
              disabled
              title="生成计划编辑器将在下一实施里程碑接入"
            ><Plus size={17} /> 新建生成计划（待接入）</button>
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
                {subtypes.map((subtype) => <option key={subtype} value={subtype}>{subtype}</option>)}
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
            {filteredAssets.length > 0 ? (
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
                <strong>这个筛选下没有资产</strong>
                <p>调整分类、状态或搜索内容。</p>
                <button className="button secondary" type="button" onClick={() => { setActiveCategory("2d-media"); setStatusFilter("all"); setSearch(""); }}>查看 2D 媒体</button>
              </div>
            )}
          </div>

          <footer className="batch-actions">
            <label><input type="checkbox" checked={table.getIsAllRowsSelected()} onChange={table.getToggleAllRowsSelectedHandler()} /> 全选本页</label>
            <span>已选择 {selectedIds.length} 项</span>
            <div />
            <button className="button primary" type="button" disabled={!selectionReviewable || reviewBusy} onClick={() => reviewAssets("approve")}>批量批准版本</button>
            <button className="button secondary" type="button" disabled={!selectionReviewable || reviewBusy} onClick={() => reviewAssets("reject")}>驳回</button>
            <button className="button secondary" type="button" disabled title="返工任务将在生成计划编辑器接入后启用">需要重做（待接入）</button>
            <button className="icon-button" type="button" aria-label="更多审核操作"><DotsThree size={19} /></button>
          </footer>
        </main>

        <Inspector asset={selectedAsset} onClose={() => setSelectedAssetId(null)} onReview={reviewAssets} />
      </div>

      <footer className="task-strip" aria-label="后台任务">
        <div className="task-label"><strong>近期任务</strong><span /></div>
        <div className="task-copy">
          <strong>{job.name}</strong>
          <span>{job.status === "completed" ? "已完成" : job.status === "credentials_locked" ? "等待凭据" : "处理中"}</span>
        </div>
        <span className={`job-status ${job.status}`}>{job.status === "completed" ? "已完成" : `${job.progress}%`}</span>
        <div className="job-previews">{job.previewImages.map((image, index) => <img key={`${image}-${index}`} src={image} alt="" />)}</div>
        <div className="job-metrics"><span>共生成 {job.total} 项</span><strong>通过 QA {job.passed} 项</strong></div>
        <div className="job-output"><small>输出位置</small><span title={job.outputPath}>{job.outputPath}</span></div>
        <button className="button secondary" type="button" disabled title="本地文件夹桥接尚未接入"><FolderOpen size={17} /> 打开文件夹</button>
      </footer>

      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <ImportDialog open={importOpen} onClose={() => setImportOpen(false)} />
      <div className={`toast ${toast ? "visible" : ""}`} role="status" aria-live="polite">{toast}</div>
    </div>
  );
}
