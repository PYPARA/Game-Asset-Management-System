import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { X, FileText, ImageSquare } from "@phosphor-icons/react";
import { fetchAssetDetails } from "../lib/api";
import type { GameAsset } from "../types";
import { MarkdownPreview } from "./MarkdownPreview";
import { useModalFocus } from "../hooks/useModalFocus";

export function ReferenceTile({ asset, onPreview }: { asset: GameAsset; onPreview: (asset: GameAsset) => void }) {
  return <button type="button" className="generation-reference-tile" onClick={() => onPreview(asset)} aria-label={`预览 ${asset.name}`}>
    {asset.thumbnails?.[0] ? <img src={asset.thumbnails?.[0]} alt="" loading="lazy" /> : <FileText size={24} />}
    <span><strong>{asset.name}</strong><small>{asset.subtypeLabel} · {asset.sourceMissing ? "源文件缺失" : "查看内容与版本"}</small></span>
  </button>;
}
export function GenerationAssetPreview({
  asset,
  revisionId,
  context = "reference",
  onClose,
}: {
  asset: GameAsset;
  revisionId?: string | null;
  context?: "reference" | "candidate";
  onClose: () => void;
}) {
  const ref = useRef<HTMLElement>(null);
  const [mediaMissing,setMediaMissing]=useState(false);
  useModalFocus({ open: true, dialogRef: ref, initialFocusRef: ref, onClose });
  const query = useQuery({ queryKey: ["generation-asset-preview", asset.id, revisionId], queryFn: () => fetchAssetDetails(asset, revisionId), staleTime: 0 });
  const value = query.data ?? asset;
  const version = revisionId ? (value.revisions??[]).find(x => x.id === revisionId) : (value.revisions??[]).find(x => x.id === value.approvedRevisionId) ?? value.revisions?.[0];
  const images = version?.image ? [version.image] : value.preview?.kind === "image" ? value.preview.images : value.thumbnails??[];
  const content = version?.content ?? value.revisionContent;
  const audio = /audio|music|sound|voice/.test(value.subtype);
  const promotedFromPreview = Boolean(revisionId && value.revisions?.some(revision => revision.status === "approved" && revision.parentRevisionId === revisionId));
  const versionState = !version
    ? "暂无可预览版本"
    : version.status === "approved"
      ? "已批准"
      : promotedFromPreview
        ? "已晋升为批准版本"
        : version.status === "superseded"
          ? "已被替代的历史候选"
          : context === "candidate"
            ? "待批准候选"
            : "候选或历史版本";
  const dialog = <div className="generation-modal-backdrop generation-asset-preview-backdrop" onClick={onClose}>
    <section ref={ref} className="generation-asset-preview" role="dialog" aria-modal="true" data-nested-modal="true" aria-label={`预览 ${asset.name}`} tabIndex={-1} onClick={e => e.stopPropagation()}>
      <header><div><small>{context === "candidate" ? "生成候选 · 版本预览" : "参考资产"}</small><h2>{value.name}</h2></div><button className="icon-button" type="button" onClick={onClose} aria-label="关闭资产预览"><X size={22} /></button></header>
      <div className="generation-asset-preview-body">
        {query.isLoading && <p role="status">正在读取资产内容…</p>}
        {query.isError && <p role="alert">无法读取资产详情，请稍后重试。</p>}
        {mediaMissing && <p role="alert">媒体文件无法读取。请到资产库检查文件位置或重新选择可用版本。</p>}
        {value.sourceDriftStatus && !["in_sync","historical"].includes(value.sourceDriftStatus) && <p role="status">源文件已变化或等待审核；本预览使用保存的修订，批准新版本后请重新检查引用。</p>}
        {value.sourceMissing && <p role="alert">源文件缺失。显示最后保存的版本，生成前需要处理缺失参考。</p>}
        {context === "reference" && revisionId && value.approvedRevisionId && revisionId !== value.approvedRevisionId && <p role="status">正在预览方案指定的历史版本，资产库已有不同的批准版本。生成前请确认继续使用本版本或更新引用。</p>}
        {revisionId && !version && !query.isLoading && <p role="alert">引用的版本不可用，请重新选择参考版本。</p>}
        <p>{version ? `${version.label} · ${versionState}` : versionState}</p>
        {audio ? images.map(src => <audio key={src} controls src={src} onError={()=>setMediaMissing(true)} />) : images.map(src => <a key={src} href={src} target="_blank" rel="noreferrer" title="打开原图"><img className="generation-preview-image" onError={()=>setMediaMissing(true)} src={src} alt={value.name} /></a>)}
        {typeof content === "string" ? <MarkdownPreview content={content} /> : content != null ? <details open={!images.length}><summary>内容与说明</summary><pre>{JSON.stringify(content, null, 2)}</pre></details> : !images.length && <p><ImageSquare size={22} /> 此资产尚无媒体产物。</p>}
        {!!(value.linkedMedia??[]).length && <><h3>关联媒体</h3><div className="generation-linked-gallery">{(value.linkedMedia??[]).map(media => <figure key={media.assetId}>{media.images.map(src => <a href={src} target="_blank" rel="noreferrer" key={src}><img src={src} alt={media.name} /></a>)}<figcaption>{media.name}</figcaption></figure>)}</div></>}
        <details><summary>技术详情</summary><p>{value.key}</p><p>版本：{version?.id ?? "未指定"}</p><p>{value.sourcePath}</p></details>
      </div>
    </section>
  </div>;
  return typeof document === "undefined" ? dialog : createPortal(dialog, document.body);
}
