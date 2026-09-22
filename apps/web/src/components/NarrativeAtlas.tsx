import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  BookOpen,
  CaretDown,
  CaretRight,
  CheckCircle,
  Clock,
  Cube,
  FileText,
  FlowArrow,
  FolderOpen,
  ImageSquare,
  LinkSimple,
  ListChecks,
  MagnifyingGlass,
  MapPin,
  Package,
  PencilSimple,
  Plus,
  Quotes,
  Sparkle,
  SpinnerGap,
  TreeStructure,
  UserCircle,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import {
  fetchNarrativeMap,
  materializeNarrativeRequirements,
  saveNarrativeScene,
} from "../lib/api";
import type {
  NarrativeGraphNode,
  NarrativeRequirement,
  NarrativeScene,
  ProjectSummary,
} from "../types";

const API_ROOT =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ?? "/api";

function renditionUrl(id: string | null) {
  return id ? `${API_ROOT}/renditions/${encodeURIComponent(id)}/content` : "";
}

const relationLabels: Record<string, string> = {
  appears_in: "出场",
  depicts: "描绘",
  illustrates: "插画",
  represents: "表现",
  references: "引用",
  depends_on: "依赖",
  contains: "包含",
};

const statusLabels: Record<NarrativeRequirement["status"], string> = {
  ready: "已就绪",
  candidate: "候选",
  planned: "已规划",
  missing: "缺失",
};

interface RequirementDraft {
  asset_key: string;
  title: string;
  role: string;
  kind: string;
  subtype: string;
  prompt: string;
}

interface SceneDraft {
  title: string;
  summary: string;
  scene_type: string;
  time: string;
  location: string;
  dialogue: string;
  requirements: RequirementDraft[];
}

interface NarrativeAtlasProps {
  project: ProjectSummary;
  onOpenProduction: (assetIds: string[]) => Promise<void> | void;
  focusSceneId?: string | null;
  onOpenAsset?: (assetId: string) => void;
}

function text(value: unknown) {
  return typeof value === "string" ? value : "";
}

function sceneDraft(scene: NarrativeScene): SceneDraft {
  const dialogueValue = scene.content.dialogue ?? scene.content.key_dialogue;
  const dialogue = Array.isArray(dialogueValue)
    ? dialogueValue.map((item) => typeof item === "string" ? item : JSON.stringify(item)).join("\n")
    : text(dialogueValue);
  return {
    title: text(scene.content.title) || scene.title,
    summary: text(scene.content.summary) || text(scene.content.description) || text(scene.content.text),
    scene_type: text(scene.content.scene_type) || "剧情场景",
    time: text(scene.content.time),
    location: text(scene.content.location),
    dialogue,
    requirements: scene.requirements.map((requirement) => ({
      asset_key: requirement.asset_key,
      title: requirement.title,
      role: requirement.role,
      kind: requirement.kind,
      subtype: requirement.subtype,
      prompt: requirement.prompt,
    })),
  };
}

function nodeIcon(node: NarrativeGraphNode) {
  if (node.kind === "media" || node.kind === "production") return ImageSquare;
  if (node.subtype.includes("character")) return UserCircle;
  if (node.subtype.includes("location")) return MapPin;
  if (node.kind === "entity") return Package;
  return FileText;
}

function GraphNodeCard({ node, onOpenAsset }: { node: NarrativeGraphNode; onOpenAsset?: (assetId: string) => void }) {
  const Icon = nodeIcon(node);
  const image = renditionUrl(node.rendition_id);
  return (
    <button className={`atlas-graph-node ${node.kind}`} data-node-id={node.asset_id} type="button" onClick={() => onOpenAsset?.(node.asset_id)} disabled={!onOpenAsset}>
      {image ? <img src={image} alt="" /> : <div className="atlas-node-placeholder"><Icon size={23} weight="duotone" /></div>}
      <div><strong>{node.title}</strong><small title={node.key}>{node.key}</small></div>
      <span className={`atlas-node-status ${node.status}`}>{node.status === "ready" ? "就绪" : node.status === "candidate" ? "候选" : node.status === "planned" ? "规划" : "缺失"}</span>
    </button>
  );
}

export function NarrativeAtlas({ project, onOpenProduction, focusSceneId, onOpenAsset }: NarrativeAtlasProps) {
  const atlasQuery = useQuery({
    queryKey: ["narrative-map", project.id],
    queryFn: () => fetchNarrativeMap(project.id),
    enabled: Boolean(project.id),
  });
  const atlas = atlasQuery.data;
  const [selectedSceneId, setSelectedSceneId] = useState<string | null>(null);
  const [collapsedChapters, setCollapsedChapters] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<SceneDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);
  const [treeInitialized, setTreeInitialized] = useState(false);

  const selectedScene = atlas?.scenes.find((scene) => scene.asset_id === selectedSceneId)
    ?? atlas?.scenes[0]
    ?? null;

  useEffect(() => {
    if (focusSceneId && atlas?.scenes.some((scene) => scene.asset_id === focusSceneId)) {
      const focused = atlas.scenes.find((scene) => scene.asset_id === focusSceneId);
      if (focused) {
        setSelectedSceneId(focusSceneId);
        setCollapsedChapters((current) => {
          const next = new Set(current);
          if (focused.chapter_asset_id) next.delete(focused.chapter_asset_id);
          return next;
        });
        setTreeInitialized(true);
      }
      return;
    }
    if (!treeInitialized && atlas?.scenes[0]) {
      const firstScene = atlas.scenes[0];
      setSelectedSceneId(firstScene.asset_id);
      setCollapsedChapters(new Set(
        atlas.chapters
          .filter((chapter) => chapter.asset_id !== firstScene.chapter_asset_id)
          .map((chapter) => chapter.asset_id),
      ));
      setTreeInitialized(true);
      return;
    }
    if (selectedSceneId && atlas && !atlas.scenes.some((scene) => scene.asset_id === selectedSceneId)) {
      setSelectedSceneId(atlas.scenes[0]?.asset_id ?? null);
    }
  }, [atlas, focusSceneId, selectedSceneId, treeInitialized]);

  useEffect(() => {
    if (selectedScene && !editing) setDraft(sceneDraft(selectedScene));
  }, [editing, selectedScene?.asset_id, selectedScene?.revision_id]);

  const query = search.trim().toLocaleLowerCase();
  const matchingSceneIds = useMemo(() => new Set(
    (atlas?.scenes ?? [])
      .filter((scene) => !query || `${scene.title} ${scene.key}`.toLocaleLowerCase().includes(query))
      .map((scene) => scene.asset_id),
  ), [atlas?.scenes, query]);
  const matchingUnassignedSceneIds = atlas?.unassigned_scene_ids.filter((id) => matchingSceneIds.has(id)) ?? [];

  const productionRequirements = selectedScene?.requirements.filter((item) => item.status !== "ready") ?? [];
  const graphNodes = selectedScene?.graph.nodes ?? [];
  const primaryNodes = graphNodes.filter((node) => node.asset_id !== selectedScene?.asset_id && node.kind === "entity");
  const supportingNodes = graphNodes.filter((node) => node.asset_id !== selectedScene?.asset_id && node.kind !== "entity");
  const heroNode = supportingNodes.find((node) =>
    node.rendition_id && (node.subtype.includes("cg") || node.subtype.includes("background")),
  );

  const toggleChapter = (chapterId: string) => setCollapsedChapters((current) => {
    const next = new Set(current);
    if (next.has(chapterId)) next.delete(chapterId);
    else next.add(chapterId);
    return next;
  });

  const beginEdit = () => {
    if (!selectedScene) return;
    setDraft(sceneDraft(selectedScene));
    setEditing(true);
    setMessage("");
  };

  const saveScene = async () => {
    if (!selectedScene || !draft) return;
    if (!draft.title.trim()) {
      setMessage("场景标题不能为空。");
      return;
    }
    const invalidRequirement = draft.requirements.find((item) => !item.asset_key.trim() || !item.title.trim());
    if (invalidRequirement) {
      setMessage("每项资产需求都需要稳定 Key 和标题。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      await saveNarrativeScene(
        selectedScene.asset_id,
        {
          ...selectedScene.content,
          title: draft.title.trim(),
          summary: draft.summary.trim(),
          scene_type: draft.scene_type.trim(),
          time: draft.time.trim(),
          location: draft.location.trim(),
          dialogue: draft.dialogue.split("\n").map((line) => line.trim()).filter(Boolean),
          asset_requirements: draft.requirements.map((item, index) => ({
            asset_key: item.asset_key.trim(),
            title: item.title.trim(),
            role: item.role.trim() || "场景配套资产",
            kind: item.kind || "media",
            subtype: item.subtype.trim() || "concept_art",
            prompt: item.prompt.trim(),
            order: index,
          })),
        },
        selectedScene.revision_id,
      );
      await atlasQuery.refetch();
      setEditing(false);
      setMessage("场景已保存为新的不可变候选修订。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "场景保存失败。");
    } finally {
      setBusy(false);
    }
  };

  const openProduction = async () => {
    if (!selectedScene || productionRequirements.length === 0) return;
    setBusy(true);
    setMessage("");
    try {
      const result = await materializeNarrativeRequirements(
        project.id,
        selectedScene.asset_id,
        productionRequirements.map((item) => item.id),
      );
      await atlasQuery.refetch();
      setPreviewOpen(false);
      await onOpenProduction(result.asset_ids);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法建立生产计划资产清单。");
    } finally {
      setBusy(false);
    }
  };

  if (!project.id) {
    return <div className="atlas-empty"><BookOpen size={36} /><strong>先载入一个 Project</strong><p>叙事地图只读取 Project 正式资产与类型化关系。</p></div>;
  }
  if (atlasQuery.isLoading) {
    return <div className="atlas-empty"><SpinnerGap className="spin" size={34} /><strong>正在编排叙事地图</strong><p>读取章节、场景与资产覆盖关系…</p></div>;
  }
  if (atlasQuery.isError || !atlas) {
    return <div className="atlas-empty error"><WarningCircle size={36} weight="fill" /><strong>叙事地图加载失败</strong><p>{atlasQuery.error instanceof Error ? atlasQuery.error.message : "本地服务未返回有效地图。"}</p><button className="atlas-button" type="button" onClick={() => void atlasQuery.refetch()}>重试</button></div>;
  }

  return (
    <div className="narrative-atlas" aria-label="叙事地图工作台">
      <aside className="atlas-tree-panel">
        <header><strong>叙事结构</strong><div><button type="button" aria-label="搜索场景" onClick={() => document.getElementById("atlas-tree-search")?.focus()}><MagnifyingGlass size={17} /></button><button type="button" aria-label="展开全部章节" onClick={() => setCollapsedChapters(new Set())}><TreeStructure size={17} /></button></div></header>
        <label className="atlas-tree-search"><MagnifyingGlass size={15} /><input id="atlas-tree-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索章节或场景" />{search && <button type="button" onClick={() => setSearch("")} aria-label="清除场景搜索"><X size={13} /></button>}</label>
        <div className="atlas-project-root"><FolderOpen size={18} weight="duotone" /><strong>{atlas.project_name}</strong></div>
        <nav aria-label="章节树">
          {atlas.chapters.map((chapter, chapterIndex) => {
            const visibleSceneIds = chapter.scene_ids.filter((sceneId) => matchingSceneIds.has(sceneId));
            if (query && visibleSceneIds.length === 0 && !chapter.title.toLocaleLowerCase().includes(query)) return null;
            const collapsed = collapsedChapters.has(chapter.asset_id);
            return <section className="atlas-chapter" key={chapter.asset_id}>
              <button className="atlas-chapter-button" type="button" onClick={() => toggleChapter(chapter.asset_id)} aria-expanded={!collapsed}>
                {collapsed ? <CaretRight size={14} /> : <CaretDown size={14} />}<BookOpen size={17} /><span>第 {chapterIndex + 1} 章　{chapter.title}</span><small>{chapter.scene_ids.length}</small>
              </button>
              {!collapsed && <ol>{visibleSceneIds.map((sceneId, sceneIndex) => {
                const scene = atlas.scenes.find((item) => item.asset_id === sceneId);
                if (!scene) return null;
                return <li key={scene.asset_id}><button type="button" className={selectedScene?.asset_id === scene.asset_id ? "active" : ""} onClick={() => { setSelectedSceneId(scene.asset_id); setEditing(false); setPreviewOpen(false); }}><span>{chapterIndex + 1}-{String(sceneIndex + 1).padStart(2, "0")}</span><strong>{scene.title}</strong>{scene.coverage.missing > 0 && <i>{scene.coverage.missing}</i>}</button></li>;
              })}</ol>}
            </section>;
          })}
          {matchingUnassignedSceneIds.length > 0 && <section className="atlas-chapter unassigned"><div className="atlas-chapter-button"><CaretDown size={14} /><FileText size={17} /><span>未分章场景</span><small>{matchingUnassignedSceneIds.length}</small></div><ol>{matchingUnassignedSceneIds.map((sceneId) => { const scene = atlas.scenes.find((item) => item.asset_id === sceneId); return scene ? <li key={sceneId}><button type="button" className={selectedScene?.asset_id === sceneId ? "active" : ""} onClick={() => setSelectedSceneId(sceneId)}><span>—</span><strong>{scene.title}</strong></button></li> : null; })}</ol></section>}
        </nav>
        <footer><span><i /> 已索引 {atlas.scenes.length} 个场景</span><small>{Math.round(atlas.coverage.ratio * 100)}% 就绪</small></footer>
      </aside>

      <main className="atlas-scene-panel">
        {selectedScene ? <>
          <header className="atlas-scene-header">
            <div><button className="atlas-scene-key atlas-asset-link" type="button" onClick={() => onOpenAsset?.(selectedScene.asset_id)} disabled={!onOpenAsset}>{selectedScene.key}</button><h1><button className="atlas-title-link" type="button" onClick={() => onOpenAsset?.(selectedScene.asset_id)} disabled={!onOpenAsset}>{selectedScene.title}</button></h1><p><span>场景类型：{text(selectedScene.content.scene_type) || "剧情场景"}</span><i /><span><Clock size={14} /> {text(selectedScene.content.time) || "时间待补充"}</span><i /><span><MapPin size={14} /> {text(selectedScene.content.location) || "地点待补充"}</span></p></div>
            <button className="atlas-icon-button" type="button" onClick={beginEdit} aria-label="编辑当前场景"><PencilSimple size={18} /></button>
          </header>
          <div className="atlas-scene-scroll">
            <section className="atlas-prose"><h2>场景概述</h2><p>{text(selectedScene.content.summary) || text(selectedScene.content.description) || text(selectedScene.content.text) || "这个场景还没有概述，点击编辑补充叙事意图。"}</p></section>
            <section className="atlas-prose atlas-dialogue"><h2>关键对白 <span>节选</span></h2>{(() => { const value = selectedScene.content.dialogue ?? selectedScene.content.key_dialogue; const lines = Array.isArray(value) ? value : text(value).split("\n").filter(Boolean); return lines.length ? <blockquote>{lines.map((line, index) => <p key={index}><Quotes size={14} />{typeof line === "string" ? line : JSON.stringify(line)}</p>)}</blockquote> : <p className="atlas-muted">暂无关键对白。</p>; })()}</section>
            <section className="atlas-relationship-section">
              <div className="atlas-section-title"><div><span>关系图谱</span><h2>场景关联</h2></div><small>{graphNodes.length} 个节点 · {selectedScene.graph.edges.length} 条关系</small></div>
              <div className="atlas-graph-canvas">
                <div className="atlas-node-column"><h3>角色 / 实体</h3>{primaryNodes.length ? primaryNodes.map((node) => <GraphNodeCard key={node.asset_id} node={node} onOpenAsset={onOpenAsset} />) : <p className="atlas-muted">暂无实体关系</p>}</div>
                <div className="atlas-graph-center"><div className="atlas-graph-flow"><FlowArrow size={27} weight="duotone" /><span>参与 / 引用</span></div><article className="atlas-scene-node">{heroNode?.rendition_id ? <img src={renditionUrl(heroNode.rendition_id)} alt="" /> : <div className="atlas-scene-node-placeholder"><Cube size={31} weight="duotone" /></div>}<div><Cube size={17} /><span><strong>{selectedScene.title}</strong><small>{selectedScene.key}</small></span></div></article><div className="atlas-graph-flow right"><span>内容关联</span><FlowArrow size={27} weight="duotone" /></div></div>
                <div className="atlas-node-column support"><h3>道具 / 地点 / 画面</h3>{supportingNodes.length ? supportingNodes.map((node) => <GraphNodeCard key={node.asset_id} node={node} onOpenAsset={onOpenAsset} />) : <p className="atlas-muted">暂无配套内容关系</p>}</div>
              </div>
              <div className="atlas-edge-ledger">{selectedScene.graph.edges.map((edge) => { const source = graphNodes.find((node) => node.asset_id === edge.source_asset_id); const target = graphNodes.find((node) => node.asset_id === edge.target_asset_id); return <span key={edge.id}><LinkSimple size={13} />{source?.title ?? "未知"}<ArrowRight size={12} />{relationLabels[edge.relation_type] ?? edge.relation_type}<ArrowRight size={12} />{target?.title ?? "未知"}</span>; })}</div>
            </section>
          </div>
        </> : <div className="atlas-empty"><BookOpen size={38} /><strong>还没有可展示的场景</strong><p>创建 content/scene 资产并用 contains 关系加入章节。</p></div>}

        {editing && selectedScene && draft && <div className="atlas-editor-backdrop" onMouseDown={() => setEditing(false)}><section className="atlas-editor" role="dialog" aria-modal="true" aria-labelledby="atlas-editor-title" onMouseDown={(event) => event.stopPropagation()}><header><div><span>不可变候选修订</span><h2 id="atlas-editor-title">编辑 {selectedScene.title}</h2></div><button type="button" onClick={() => setEditing(false)} aria-label="关闭场景编辑器"><X size={18} /></button></header><div className="atlas-editor-body"><div className="atlas-editor-grid"><label><span>场景标题</span><input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label><label><span>场景类型</span><input value={draft.scene_type} onChange={(event) => setDraft({ ...draft, scene_type: event.target.value })} /></label><label><span>时间</span><input value={draft.time} onChange={(event) => setDraft({ ...draft, time: event.target.value })} placeholder="建昭十二年·秋" /></label><label><span>地点</span><input value={draft.location} onChange={(event) => setDraft({ ...draft, location: event.target.value })} placeholder="大周·宣德殿" /></label></div><label><span>场景概述</span><textarea rows={4} value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} /></label><label><span>关键对白（每行一条）</span><textarea rows={6} value={draft.dialogue} onChange={(event) => setDraft({ ...draft, dialogue: event.target.value })} /></label><section className="atlas-requirement-editor"><div><span><ListChecks size={16} /> 资产需求</span><button type="button" onClick={() => setDraft({ ...draft, requirements: [...draft.requirements, { asset_key: "", title: "", role: "场景配套资产", kind: "media", subtype: "concept_art", prompt: "" }] })}><Plus size={14} /> 添加</button></div>{draft.requirements.map((requirement, index) => <article key={`${index}-${requirement.asset_key}`}><div><label><span>稳定 Key</span><input value={requirement.asset_key} onChange={(event) => setDraft({ ...draft, requirements: draft.requirements.map((item, itemIndex) => itemIndex === index ? { ...item, asset_key: event.target.value } : item) })} placeholder="media.cg.scene_key" /></label><label><span>标题</span><input value={requirement.title} onChange={(event) => setDraft({ ...draft, requirements: draft.requirements.map((item, itemIndex) => itemIndex === index ? { ...item, title: event.target.value } : item) })} /></label><label><span>用途</span><input value={requirement.role} onChange={(event) => setDraft({ ...draft, requirements: draft.requirements.map((item, itemIndex) => itemIndex === index ? { ...item, role: event.target.value } : item) })} /></label><label><span>子类型</span><input value={requirement.subtype} onChange={(event) => setDraft({ ...draft, requirements: draft.requirements.map((item, itemIndex) => itemIndex === index ? { ...item, subtype: event.target.value } : item) })} /></label></div><label><span>生产 Prompt</span><textarea rows={2} value={requirement.prompt} onChange={(event) => setDraft({ ...draft, requirements: draft.requirements.map((item, itemIndex) => itemIndex === index ? { ...item, prompt: event.target.value } : item) })} /></label><button type="button" aria-label={`删除资产需求 ${requirement.title || index + 1}`} onClick={() => setDraft({ ...draft, requirements: draft.requirements.filter((_, itemIndex) => itemIndex !== index) })}><X size={14} /></button></article>)}</section></div><footer>{message && <p role="alert">{message}</p>}<span /><button className="atlas-button secondary" type="button" onClick={() => setEditing(false)}>取消</button><button className="atlas-button primary" type="button" onClick={() => void saveScene()} disabled={busy}>{busy ? <SpinnerGap className="spin" size={16} /> : <CheckCircle size={16} />} 保存为候选</button></footer></section></div>}
      </main>

      <aside className="atlas-coverage-panel">
        <header><div><span>资产覆盖</span><strong>{selectedScene ? `${Math.round(selectedScene.coverage.ratio * 100)}%` : "—"}</strong></div><button type="button" onClick={() => void atlasQuery.refetch()} aria-label="刷新资产覆盖"><ArrowRight size={17} /></button></header>
        {selectedScene ? <div className="atlas-coverage-scroll">
          <section className="coverage-summary"><div><span>当前场景</span><strong>{selectedScene.coverage.ready}/{selectedScene.coverage.required}</strong></div><progress max={Math.max(1, selectedScene.coverage.required)} value={selectedScene.coverage.ready} /><p><span><i className="ready" /> 已就绪 {selectedScene.coverage.ready}</span><span><i className="candidate" /> 候选/规划 {selectedScene.coverage.candidate + selectedScene.coverage.planned}</span><span><i className="missing" /> 缺失 {selectedScene.coverage.missing}</span></p></section>
          {(["ready", "candidate", "planned", "missing"] as const).map((status) => { const rows = selectedScene.requirements.filter((item) => item.status === status); if (!rows.length) return null; return <section className={`coverage-group ${status}`} key={status}><h3><span><i /> {statusLabels[status]}（{rows.length}）</span>{status === "missing" && <button type="button" onClick={() => setPreviewOpen(true)}>规划</button>}</h3>{rows.map((requirement) => <article className="coverage-asset" key={requirement.id}>{requirement.rendition_id ? <img src={renditionUrl(requirement.rendition_id)} alt="" /> : <div><ImageSquare size={20} weight="duotone" /></div>}<span><strong>{requirement.title}</strong><small>{requirement.asset_key}</small><em>{requirement.role}</em></span><b>{statusLabels[requirement.status]}</b></article>)}</section>; })}
          {selectedScene.requirements.length === 0 && <div className="atlas-no-requirements"><ListChecks size={28} /><strong>还没有资产需求</strong><p>编辑场景，为角色、道具、地点或 CG 添加稳定资产 Key。</p><button className="atlas-button secondary" type="button" onClick={beginEdit}>编辑场景</button></div>}
        </div> : null}
        <footer><button className="atlas-button danger" type="button" onClick={() => void openProduction()} disabled={busy || productionRequirements.length === 0}>{busy ? <SpinnerGap className="spin" size={17} /> : <Sparkle size={17} weight="fill" />} 生成缺失资产</button><button className="atlas-button secondary" type="button" onClick={() => setPreviewOpen(true)} disabled={productionRequirements.length === 0}><ListChecks size={17} /> 预览生成方案</button>{message && !editing && <p role="status">{message}</p>}</footer>

        {previewOpen && selectedScene && <div className="atlas-plan-preview" role="dialog" aria-label="缺失资产生产方案"><header><div><span>复用 M2–M4 闭环</span><h2>生产方案预览</h2></div><button type="button" onClick={() => setPreviewOpen(false)} aria-label="关闭方案预览"><X size={17} /></button></header><p>以下 {productionRequirements.length} 项将先获得普通 Catalog 资产身份，再进入现有计划编辑器确认供应商、模型、DAG 与预算。</p><ol>{productionRequirements.map((item, index) => <li key={item.id}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{item.title}</strong><small>{item.asset_key}</small></div><em>{item.kind === "media" ? "图像生成" : "结构化文本"}</em></li>)}</ol><footer><small><CheckCircle size={14} /> 不创建第二套资产 ID 或发布状态</small><button className="atlas-button primary" type="button" onClick={() => void openProduction()} disabled={busy}>{busy ? "正在准备…" : "进入预算确认"}</button></footer></div>}
      </aside>
    </div>
  );
}
