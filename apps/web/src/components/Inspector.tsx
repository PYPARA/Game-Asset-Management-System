import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowsLeftRight,
  CheckCircle,
  ClockCounterClockwise,
  Copy,
  FileText,
  ImagesSquare,
  PencilSimple,
  Star,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { assetSubtypeTitle, domainLabel, fieldLabel } from "../lib/labels";
import type { GameAsset } from "../types";

type InspectorTab = "details" | "relations" | "prompt" | "qa" | "history";

interface InspectorProps {
  asset: GameAsset | null;
  allAssets: GameAsset[];
  onClose: () => void;
  onEdit: (asset: GameAsset) => void;
  onNavigate: (assetId: string) => void;
  onReview: (decision: "approve" | "reject" | "regenerate", ids?: string[]) => void;
}

const relationLabels: Record<string, string> = {
  contains: "包含",
  appears_in: "出现在",
  references: "引用",
  depicts: "描绘",
  represents: "代表",
  fallback_to: "回退到",
  illustrates: "表现",
  depends_on: "依赖",
};

function MarkdownPreview({ content }: { content: string }) {
  return (
    <article className="markdown-document">
      {content.split("\n").map((line, index) => {
        if (line.startsWith("### ")) return <h4 key={index}>{line.slice(4)}</h4>;
        if (line.startsWith("## ")) return <h3 key={index}>{line.slice(3)}</h3>;
        if (line.startsWith("# ")) return <h2 key={index}>{line.slice(2)}</h2>;
        if (line.startsWith("- ")) return <p className="markdown-list-item" key={index}>{line.slice(2)}</p>;
        return line ? <p key={index}>{line}</p> : <span className="markdown-gap" key={index} />;
      })}
    </article>
  );
}

function StructuredValue({
  name,
  value,
  allAssets,
  onNavigate,
}: {
  name: string;
  value: unknown;
  allAssets: GameAsset[];
  onNavigate: (assetId: string) => void;
}) {
  if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
    return (
      <div className="document-chip-list">
        {value.length ? value.map((item) => {
          const key = name === "participants" && !item.startsWith("entity.character.")
            ? `entity.character.${item}`
            : item;
          const linked = allAssets.find((asset) => asset.key === key);
          return linked
            ? <button type="button" key={item} onClick={() => onNavigate(linked.id)}>{linked.name}<small>{linked.subtypeLabel}</small></button>
            : <span key={item}>{item}</span>;
        }) : <span>无</span>}
      </div>
    );
  }
  if (typeof value === "string") return <p className="document-text-value">{value || "—"}</p>;
  if (typeof value === "number" || typeof value === "boolean") return <p className="document-text-value">{String(value)}</p>;
  return <pre className="document-json-value">{JSON.stringify(value, null, 2)}</pre>;
}

function DocumentReader({ asset, allAssets, onNavigate }: Pick<InspectorProps, "asset" | "allAssets" | "onNavigate">) {
  if (!asset) return null;
  if (asset.revisionFormat === "markdown" && typeof asset.revisionContent === "string") {
    return <MarkdownPreview content={asset.revisionContent} />;
  }
  const content = asset.revisionContent && typeof asset.revisionContent === "object" && !Array.isArray(asset.revisionContent)
    ? asset.revisionContent as Record<string, unknown>
    : {};
  const hidden = new Set(["id", "source", "title", "name"]);
  return (
    <article className="document-reader">
      <header>
        <div className="document-stamp"><FileText size={20} weight="duotone" /><span>{asset.subtypeLabel}</span></div>
        <h2>{asset.name}</h2>
        {asset.preview.kind === "content" && <p>{asset.preview.summary}</p>}
        <div className="document-meta-row">
          {asset.preview.kind === "content" && <span>{domainLabel(asset.preview.meta)}</span>}
        </div>
      </header>
      <div className="document-fields">
        {Object.entries(content).filter(([name]) => !hidden.has(name)).map(([name, value]) => (
          <section key={name}>
            <h3>{fieldLabel(name)}</h3>
            <StructuredValue name={name} value={value} allAssets={allAssets} onNavigate={onNavigate} />
          </section>
        ))}
      </div>
    </article>
  );
}

function EntityGallery({ asset }: { asset: GameAsset }) {
  const [selectedKey, setSelectedKey] = useState(asset.linkedMedia[0]?.key ?? "");
  useEffect(() => setSelectedKey(asset.linkedMedia[0]?.key ?? ""), [asset.id, asset.linkedMedia]);
  const selected = asset.linkedMedia.find((item) => item.key === selectedKey) ?? asset.linkedMedia[0];
  if (!selected) {
    return <div className="entity-gallery-empty"><ImagesSquare size={30} /><strong>没有关联媒体</strong><p>{asset.subtype === "character" ? "请通过 depicts 关系关联角色立绘。" : "请通过 represents 关系关联物品图标。"}</p></div>;
  }
  const primaryImage = selected.images[0];
  return (
    <div className={`entity-gallery ${asset.subtype === "item" ? "item-gallery" : ""}`}>
      <figure>
        <div className="entity-main-image"><img src={primaryImage} alt={selected.name} /></div>
        <figcaption><strong>{selected.name}</strong><span>{selected.key}</span></figcaption>
      </figure>
      <div className="entity-thumbnail-rail" aria-label="关联媒体">
        {asset.linkedMedia.map((media) => (
          <button type="button" key={media.assetId} className={media.key === selected.key ? "active" : ""} onClick={() => setSelectedKey(media.key)} title={media.key}>
            <img src={media.images[0]} alt="" /><span>{media.name.includes("·") ? media.name.split("·").slice(1).join("·") : media.name}</span>
          </button>
        ))}
      </div>
      {asset.subtype === "item" && <div className="runtime-icon-preview"><span>48×48 运行时预览</span><img src={primaryImage} alt={`${asset.name} 48×48 预览`} /></div>}
    </div>
  );
}

function MediaComparison({ asset }: { asset: GameAsset }) {
  const [compareMode, setCompareMode] = useState<"side" | "overlay">("side");
  const [opacity, setOpacity] = useState(50);
  const candidate = asset.revisions.find((revision) => revision.status === "candidate");
  const approved = asset.revisions.find((revision) => revision.status === "approved");
  const only = candidate ?? approved;
  return (
    <section className="comparison-section">
      <div className="comparison-toolbar">
        <div className="segmented-control" role="group" aria-label="版本比较方式">
          <button className={compareMode === "side" ? "active" : ""} onClick={() => setCompareMode("side")} type="button">并排</button>
          <button className={compareMode === "overlay" ? "active" : ""} onClick={() => setCompareMode("overlay")} type="button" disabled={!candidate || !approved}>叠加</button>
        </div>
        <button className="text-button" type="button" onClick={() => void navigator.clipboard?.writeText(asset.key)}><Copy size={15} /> 复制 Key</button>
      </div>
      {candidate && approved && compareMode === "side" ? (
        <div className="comparison-grid">
          <figure><figcaption><strong>当前候选</strong><span>r{String(candidate.sequence).padStart(2, "0")}</span></figcaption><div className="compare-image-frame"><img src={candidate.image} alt="" /></div></figure>
          <div className="compare-divider" aria-hidden="true"><ArrowsLeftRight size={17} /></div>
          <figure><figcaption><strong>已批准</strong><span>r{String(approved.sequence).padStart(2, "0")}</span></figcaption><div className="compare-image-frame"><img src={approved.image} alt="" /></div></figure>
        </div>
      ) : candidate && approved ? (
        <div className="overlay-comparison"><div className="overlay-frame"><img src={approved.image} alt="" /><img src={candidate.image} alt="" style={{ opacity: opacity / 100 }} /></div><label>候选稿透明度<input type="range" min="0" max="100" value={opacity} onChange={(event) => setOpacity(Number(event.target.value))} /><span>{opacity}%</span></label></div>
      ) : only?.image ? (
        <div className="comparison-grid comparison-single"><figure><figcaption><strong>{only.status === "approved" ? "已批准版本" : "当前候选"}</strong><span>r{String(only.sequence).padStart(2, "0")}</span></figcaption><div className="compare-image-frame"><img src={only.image} alt={asset.name} /></div></figure></div>
      ) : (
        <div className="review-blocked"><WarningCircle size={27} weight="fill" /><strong>此修订没有可预览媒体</strong></div>
      )}
    </section>
  );
}

export function Inspector({ asset, allAssets, onClose, onEdit, onNavigate, onReview }: InspectorProps) {
  const structured = Boolean(asset && asset.kind !== "media" && asset.kind !== "production");
  const [tab, setTab] = useState<InspectorTab>(structured ? "details" : "prompt");
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  useEffect(() => setTab(asset && asset.kind !== "media" && asset.kind !== "production" ? "details" : "prompt"), [asset?.id]);

  if (!asset) return <aside className="inspector inspector-empty"><ArrowsLeftRight size={30} /><strong>选择资产查看详情</strong><p>这里会按资产类型展示文档、关联媒体、版本和审核信息。</p></aside>;

  const tabs: Array<{ id: InspectorTab; label: string; suffix?: string }> = structured
    ? [{ id: "details", label: asset.kind === "entity" ? "资料" : "内容" }, { id: "relations", label: "关系", suffix: String(asset.relatedAssets.length) }, { id: "history", label: "版本", suffix: String(asset.revisions.length) }]
    : [{ id: "prompt", label: "提示词与配方" }, { id: "qa", label: "硬 QA", suffix: `${asset.qaPassed}/${asset.qaTotal}` }, { id: "history", label: "版本", suffix: String(asset.revisions.length) }];
  const candidate = asset.revisions.find((revision) => revision.status === "candidate");
  const reviewBlockReason = !candidate ? asset.reviewBlockReason ?? "当前没有待审候选修订。" : asset.reviewReady === false ? asset.reviewBlockReason ?? "候选数据尚未完整加载。" : null;
  const moveFocus = (event: ReactKeyboardEvent<HTMLButtonElement>, current: InspectorTab) => {
    const index = tabs.findIndex((item) => item.id === current);
    const offset = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!offset) return;
    event.preventDefault();
    const next = tabs[(index + offset + tabs.length) % tabs.length].id;
    setTab(next);
    tabRefs.current[next]?.focus();
  };

  return (
    <aside className="inspector" aria-label="资产检查器">
      <header className="inspector-header">
        <div className="inspector-identity"><strong title={asset.key}>{asset.key}</strong><span>{asset.name}</span></div>
        <div className="inspector-header-actions"><span className="asset-type-chip" title={assetSubtypeTitle(asset.kind, asset.subtype)}>{asset.subtypeLabel}</span><button type="button" className="icon-button" aria-label="关注资产"><Star size={18} /></button><button type="button" className="icon-button" onClick={onClose} aria-label="关闭检查器"><X size={19} /></button></div>
      </header>

      <div className="inspector-scroll">
        {structured ? (
          <section className="structured-preview-section">
            {asset.kind === "entity" && ["character", "item"].includes(asset.subtype)
              ? <EntityGallery asset={asset} />
              : <div className="document-preview-summary"><FileText size={22} /><div><strong>{asset.subtypeLabel}</strong><span>{asset.preview.kind === "content" ? asset.preview.meta : "结构化资料"}</span></div><p>{asset.preview.kind === "content" ? asset.preview.summary : asset.name}</p></div>}
          </section>
        ) : <MediaComparison asset={asset} />}

        <div className="inspector-tabs" role="tablist" aria-label="资产检查详情">
          {tabs.map((item) => <button ref={(node) => { tabRefs.current[item.id] = node; }} key={item.id} role="tab" aria-selected={tab === item.id} tabIndex={tab === item.id ? 0 : -1} className={tab === item.id ? "active" : ""} type="button" onClick={() => setTab(item.id)} onKeyDown={(event) => moveFocus(event, item.id)}>{item.label} {item.suffix && <span>{item.suffix}</span>}</button>)}
        </div>

        {structured && tab === "details" && <section className="inspector-panel structured-details-panel"><DocumentReader asset={asset} allAssets={allAssets} onNavigate={onNavigate} /></section>}
        {structured && tab === "relations" && <section className="inspector-panel"><div className="relation-list">{asset.relatedAssets.length ? asset.relatedAssets.map((related) => <button type="button" key={`${related.direction}-${related.relationType}-${related.assetId}`} onClick={() => onNavigate(related.assetId)}><span>{relationLabels[related.relationType] ?? related.relationType}</span><strong>{related.name}</strong><small>{related.key}</small></button>) : <div className="relation-empty">这个资产还没有关系记录。</div>}</div></section>}
        {!structured && tab === "prompt" && <section className="inspector-panel"><label className="prompt-field"><span>提示词</span><textarea value={asset.prompt} readOnly rows={5} /></label><label className="prompt-field"><span>负向提示词</span><textarea value={asset.negativePrompt} readOnly rows={3} /></label><dl className="recipe-meta"><div><dt>模型</dt><dd>{asset.model}</dd></div><div><dt>配方</dt><dd>{asset.recipe}</dd></div><div><dt>质量</dt><dd>high</dd></div><div><dt>种子</dt><dd>{asset.seed}</dd></div></dl></section>}
        {!structured && tab === "qa" && <section className="inspector-panel"><div className="qa-heading"><strong>硬 QA 检查</strong><span className={asset.qaPassed === asset.qaTotal ? "qa-summary pass" : "qa-summary warn"}>{asset.qaPassed}/{asset.qaTotal} 通过</span></div><ul className="qa-list">{asset.qa.map((check) => <li key={check.id} className={check.passed ? "pass" : "warn"}>{check.passed ? <CheckCircle size={18} weight="fill" /> : <WarningCircle size={18} weight="fill" />}<span>{check.label}</span><small>{check.result}</small></li>)}</ul></section>}
        {tab === "history" && <section className="inspector-panel"><ol className="revision-history">{asset.revisions.map((revision) => <li key={revision.id} className={`revision-${revision.status}`}><ClockCounterClockwise size={18} /><div><strong>{revision.label}</strong><span>{revision.createdAt} · {revision.author}</span></div><small>{revision.format === "media" ? revision.fileSize : revision.format.toUpperCase()}</small></li>)}</ol></section>}
      </div>

      <footer className="inspector-review-actions">
        <button className="button secondary" type="button" disabled={Boolean(reviewBlockReason)} onClick={() => onReview("reject", [asset.id])}>驳回</button>
        {structured && <button className="button secondary edit-asset-button" type="button" disabled={!asset.detailsLoaded} onClick={() => onEdit(asset)}><PencilSimple size={15} /> 编辑内容</button>}
        <button className="button primary" type="button" disabled={Boolean(reviewBlockReason)} onClick={() => onReview("approve", [asset.id])}>批准候选</button>
      </footer>
    </aside>
  );
}
