import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useRef, useState } from "react";
import {
  ArrowsLeftRight,
  CheckCircle,
  ClockCounterClockwise,
  Copy,
  FloppyDisk,
  Star,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import type { GameAsset } from "../types";

type InspectorTab = "prompt" | "qa" | "history";
const inspectorTabOrder: InspectorTab[] = ["prompt", "qa", "history"];

interface InspectorProps {
  asset: GameAsset | null;
  onClose: () => void;
  onReview: (decision: "approve" | "reject" | "regenerate", ids?: string[]) => void;
}

export function Inspector({ asset, onClose, onReview }: InspectorProps) {
  const [tab, setTab] = useState<InspectorTab>("prompt");
  const [compareMode, setCompareMode] = useState<"side" | "overlay">("side");
  const [overlayOpacity, setOverlayOpacity] = useState(50);
  const [prompt, setPrompt] = useState(asset?.prompt ?? "");
  const tabRefs = useRef<Record<InspectorTab, HTMLButtonElement | null>>({
    prompt: null,
    qa: null,
    history: null,
  });

  useEffect(() => {
    setPrompt(asset?.prompt ?? "");
    setTab("prompt");
    setCompareMode("side");
  }, [asset?.id, asset?.prompt]);

  const candidate = asset?.revisions.find((revision) => revision.status === "candidate");
  const approved = asset?.revisions.find((revision) => revision.status === "approved");

  if (!asset) {
    return (
      <aside className="inspector inspector-empty">
        <ArrowsLeftRight size={30} />
        <strong>选择资产查看版本</strong>
        <p>你可以在这里比较候选稿、检查 Prompt 与 QA，并作出审核决定。</p>
      </aside>
    );
  }

  const tabs: Array<{ id: InspectorTab; label: string; suffix?: string }> = [
    { id: "prompt", label: "提示词与配方" },
    { id: "qa", label: "硬 QA", suffix: `${asset.qaPassed}/${asset.qaTotal}` },
    { id: "history", label: "版本记录", suffix: String(asset.revisions.length) },
  ];
  const reviewBlockReason =
    !candidate
      ? asset.reviewBlockReason ?? "真实候选修订尚未加载，审核已禁用。"
      : asset.reviewReady === false
        ? asset.reviewBlockReason ?? "候选或硬 QA 证据尚未完整加载，审核已禁用。"
        : null;

  const moveTabFocus = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentTab: InspectorTab,
  ) => {
    const currentIndex = inspectorTabOrder.indexOf(currentTab);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % inspectorTabOrder.length;
    if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + inspectorTabOrder.length) % inspectorTabOrder.length;
    }
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = inspectorTabOrder.length - 1;
    if (nextIndex === null) return;

    event.preventDefault();
    const nextTab = inspectorTabOrder[nextIndex];
    setTab(nextTab);
    tabRefs.current[nextTab]?.focus();
  };

  return (
    <aside className="inspector" aria-label="资产检查器">
      <header className="inspector-header">
        <div className="inspector-identity">
          <strong title={asset.key}>{asset.key}</strong>
          <span>{asset.name}</span>
        </div>
        <div className="inspector-header-actions">
          <span className="asset-type-chip">{asset.subtype}</span>
          <button type="button" className="icon-button" aria-label="关注资产"><Star size={18} /></button>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭检查器"><X size={19} /></button>
        </div>
      </header>

      <div className="inspector-scroll">
        <section className="comparison-section">
          <div className="comparison-toolbar">
            <div className="segmented-control" role="group" aria-label="版本比较方式">
              <button
                className={compareMode === "side" ? "active" : ""}
                aria-pressed={compareMode === "side"}
                onClick={() => setCompareMode("side")}
                type="button"
              >
                并排
              </button>
              <button
                className={compareMode === "overlay" ? "active" : ""}
                aria-pressed={compareMode === "overlay"}
                onClick={() => setCompareMode("overlay")}
                type="button"
              >
                叠加
              </button>
            </div>
            <button className="text-button" type="button"><Copy size={15} /> 复制 Key</button>
          </div>

          {candidate && approved && compareMode === "side" ? (
            <div className="comparison-grid">
              <figure>
                <figcaption><strong>当前候选</strong><span>{candidate.id}</span></figcaption>
                <div className="compare-image-frame"><img src={candidate.image} alt={`${asset.name} 当前候选`} /></div>
              </figure>
              <div className="compare-divider" aria-hidden="true"><ArrowsLeftRight size={17} /></div>
              <figure>
                <figcaption><strong>已批准</strong><span>{approved.id}</span></figcaption>
                <div className="compare-image-frame"><img src={approved.image} alt={`${asset.name} 已批准版本`} /></div>
              </figure>
            </div>
          ) : candidate && approved ? (
            <div className="overlay-comparison">
              <div className="overlay-frame">
                <img src={approved.image} alt={`${asset.name} 已批准版本`} />
                <img src={candidate.image} alt={`${asset.name} 候选版本叠加`} style={{ opacity: overlayOpacity / 100 }} />
              </div>
              <label>
                候选稿透明度
                <input type="range" min="0" max="100" value={overlayOpacity} onChange={(event) => setOverlayOpacity(Number(event.target.value))} />
                <span>{overlayOpacity}%</span>
              </label>
            </div>
          ) : candidate ? (
            <div className="comparison-grid comparison-single">
              <figure>
                <figcaption><strong>首个候选</strong><span>{candidate.id}</span></figcaption>
                {candidate.image ? (
                  <div className="compare-image-frame"><img src={candidate.image} alt={`${asset.name} 当前候选`} /></div>
                ) : (
                  <div className="review-blocked"><strong>此修订没有可预览媒体</strong></div>
                )}
              </figure>
            </div>
          ) : (
            <div className="review-blocked" id="inspector-review-block-reason" role="status">
              <WarningCircle size={27} weight="fill" />
              <strong>当前资产不可审核</strong>
              <p>{reviewBlockReason}</p>
            </div>
          )}

          {candidate && (
            <dl className="revision-meta">
              <div><dt>版本</dt><dd>{candidate.label}</dd></div>
              <div><dt>更新时间</dt><dd>{candidate.createdAt}</dd></div>
              <div><dt>分辨率</dt><dd>{candidate.resolution}</dd></div>
              <div><dt>文件</dt><dd>{candidate.fileSize}</dd></div>
            </dl>
          )}
        </section>

        <div
          className="inspector-tabs"
          role="tablist"
          aria-label="资产检查详情"
          aria-orientation="horizontal"
        >
          {tabs.map((item) => (
            <button
              ref={(node) => {
                tabRefs.current[item.id] = node;
              }}
              key={item.id}
              id={`tab-${item.id}`}
              role="tab"
              aria-selected={tab === item.id}
              aria-controls={`panel-${item.id}`}
              tabIndex={tab === item.id ? 0 : -1}
              className={tab === item.id ? "active" : ""}
              type="button"
              onClick={() => setTab(item.id)}
              onKeyDown={(event) => moveTabFocus(event, item.id)}
            >
              {item.label} {item.suffix && <span>{item.suffix}</span>}
            </button>
          ))}
        </div>

        <section
          className="inspector-panel"
          id="panel-prompt"
          role="tabpanel"
          aria-labelledby="tab-prompt"
          hidden={tab !== "prompt"}
        >
            <label className="prompt-field">
              <span>提示词</span>
              <textarea
                value={prompt}
                readOnly
                rows={5}
              />
            </label>
            <label className="prompt-field">
              <span>负向提示词</span>
              <textarea value={asset.negativePrompt} readOnly rows={3} />
            </label>
            <button className="save-prompt" type="button" disabled title="配方修订写回将在下一实施里程碑接入">
              <FloppyDisk size={16} /> 配方修订待接入
            </button>
            <dl className="recipe-meta">
              <div><dt>模型</dt><dd>{asset.model}</dd></div>
              <div><dt>配方</dt><dd>{asset.recipe}</dd></div>
              <div><dt>质量</dt><dd>high</dd></div>
              <div><dt>种子</dt><dd>{asset.seed}</dd></div>
            </dl>
        </section>

        <section
          className="inspector-panel"
          id="panel-qa"
          role="tabpanel"
          aria-labelledby="tab-qa"
          hidden={tab !== "qa"}
          tabIndex={0}
        >
            <div className="qa-heading">
              <strong>硬 QA 检查</strong>
              <span className={asset.qaPassed === asset.qaTotal ? "qa-summary pass" : "qa-summary warn"}>
                {asset.qaPassed}/{asset.qaTotal} 通过
              </span>
            </div>
            <ul className="qa-list">
              {asset.qa.map((check) => (
                <li key={check.id} className={check.passed ? "pass" : "warn"}>
                  {check.passed ? <CheckCircle size={18} weight="fill" /> : <WarningCircle size={18} weight="fill" />}
                  <span>{check.label}</span>
                  <small>{check.result}</small>
                </li>
              ))}
            </ul>
        </section>

        <section
          className="inspector-panel"
          id="panel-history"
          role="tabpanel"
          aria-labelledby="tab-history"
          hidden={tab !== "history"}
          tabIndex={0}
        >
            <ol className="revision-history">
              {asset.revisions.map((revision) => (
                <li key={revision.id}>
                  <ClockCounterClockwise size={18} />
                  <div><strong>{revision.label}</strong><span>{revision.createdAt} · {revision.author}</span></div>
                  <small>{revision.fileSize}</small>
                </li>
              ))}
            </ol>
        </section>
      </div>

      <footer className="inspector-review-actions">
        <button
          className="button secondary"
          type="button"
          disabled={Boolean(reviewBlockReason)}
          aria-describedby={reviewBlockReason ? "inspector-review-block-reason" : undefined}
          onClick={() => onReview("reject", [asset.id])}
        >
          驳回
        </button>
        <button
          className="button secondary"
          type="button"
          disabled
          title="返工任务将在生成计划编辑器接入后启用"
        >
          需要重做（待接入）
        </button>
        <button
          className="button primary"
          type="button"
          disabled={Boolean(reviewBlockReason)}
          aria-describedby={reviewBlockReason ? "inspector-review-block-reason" : undefined}
          onClick={() => onReview("approve", [asset.id])}
        >
          批准候选
        </button>
      </footer>
    </aside>
  );
}
