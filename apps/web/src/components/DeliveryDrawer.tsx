import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowClockwise, CheckCircle, CloudArrowUp, WarningCircle, X } from "@phosphor-icons/react";
import {
  applyExport,
  fetchDeliveries,
  fetchExportConfig,
  fetchReleases,
  previewExport,
  rollbackExport,
  updateExportConfig,
  verifyExport,
} from "../lib/api";
import { useModalFocus } from "../hooks/useModalFocus";
import type { DeliveryRecord, ExportPreview, ReleaseSummary } from "../types";

interface DeliveryDrawerProps {
  open: boolean;
  projectId: string;
  onClose: () => void;
  onMessage?: (message: string) => void;
}

export function DeliveryDrawer({ open, projectId, onClose, onMessage }: DeliveryDrawerProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [releases, setReleases] = useState<ReleaseSummary[]>([]);
  const [deliveries, setDeliveries] = useState<DeliveryRecord[]>([]);
  const [selectedReleaseId, setSelectedReleaseId] = useState("");
  const [gameRoot, setGameRoot] = useState("");
  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useModalFocus({ open, dialogRef, initialFocusRef: closeButtonRef, onClose });

  const v2Releases = useMemo(
    () => releases.filter((release) => release.manifest_version === 2),
    [releases],
  );

  useEffect(() => {
    if (!open || !projectId) return;
    let cancelled = false;
    setError("");
    void Promise.all([fetchReleases(projectId), fetchExportConfig(projectId), fetchDeliveries(projectId)])
      .then(([nextReleases, config, nextDeliveries]) => {
        if (cancelled) return;
        setReleases(nextReleases);
        setDeliveries(nextDeliveries);
        setSelectedReleaseId((current) => current || nextReleases.find((item) => item.manifest_version === 2)?.id || "");
        setGameRoot(String(config.local.game_root ?? ""));
      })
      .catch((loadError: unknown) => {
        if (!cancelled) setError(loadError instanceof Error ? loadError.message : "无法读取交付配置。");
      });
    return () => { cancelled = true; };
  }, [open, projectId]);

  if (!open) return null;

  const run = async (action: "preview" | "apply" | "verify" | "rollback") => {
    if (!selectedReleaseId) {
      setError("请先选择一个 Release Manifest v2。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (action === "preview") {
        setPreview(await previewExport(projectId, selectedReleaseId, gameRoot || undefined));
        setMessage("预览已生成，没有修改游戏仓库。");
      } else if (action === "apply") {
        const result = await applyExport(projectId, selectedReleaseId, gameRoot || undefined, true);
        setDeliveries((current) => [result, ...current]);
        setMessage(result.status === "no_op" ? "目标已是相同快照，交付为 no-op。" : "交付完成，gams-lock.json 已最后写入并验证。");
        setPreview(null);
        onMessage?.(result.status === "no_op" ? "交付无变化。" : "Release 已交付到游戏仓库。");
      } else if (action === "verify") {
        const result = await verifyExport(projectId, selectedReleaseId, gameRoot || undefined, false);
        setMessage(result.ok ? "受管文件哈希全部一致。" : `验证发现 ${(result.issues as string[] | undefined)?.length ?? 0} 个问题。`);
        if (!result.ok) setError((result.issues as string[] | undefined)?.join("；") ?? "交付验证失败。");
      } else {
        const result = await rollbackExport(projectId, selectedReleaseId, gameRoot || undefined, true);
        setDeliveries((current) => [result, ...current]);
        setMessage("已通过新的 Delivery 收据回滚到所选 Release。");
        setPreview(null);
        onMessage?.("Release 回滚完成。");
      }
    } catch (actionError: unknown) {
      setError(actionError instanceof Error ? actionError.message : "交付动作失败。");
    } finally {
      setBusy(false);
    }
  };

  const saveRoot = async () => {
    setBusy(true);
    setError("");
    try {
      await updateExportConfig(projectId, gameRoot.trim() || null);
      setMessage("本机 checkout 绑定已保存到 project.local.yaml。");
    } catch (saveError: unknown) {
      setError(saveError instanceof Error ? saveError.message : "无法保存 checkout 绑定。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section ref={dialogRef} className="delivery-drawer" role="dialog" aria-modal="true" aria-label="Release 与游戏交付" tabIndex={-1}>
        <header className="drawer-header delivery-header">
          <div>
            <span className="section-kicker">不可变 Release · 外部 checkout</span>
            <h2>Release 与游戏交付</h2>
          </div>
          <button ref={closeButtonRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭交付面板"><X size={19} /></button>
        </header>
        <div className="delivery-scroll">
          <section className="delivery-section delivery-hero">
            <div className="delivery-hero-icon"><CloudArrowUp size={23} weight="duotone" /></div>
            <div><strong>交付是一次显式文件事务</strong><p>未知文件不会删除，受管文件被人工修改时会阻止覆盖；Git 始终由人工决定。</p></div>
          </section>
          <section className="delivery-section">
            <div className="delivery-section-heading"><div><span className="section-kicker">目标</span><h3>绑定游戏 checkout</h3></div><button className="button ghost" type="button" onClick={saveRoot} disabled={busy}>保存绑定</button></div>
            <label className="delivery-field"><span>绝对路径</span><input value={gameRoot} onChange={(event) => setGameRoot(event.target.value)} placeholder="/Users/.../Game-Project" /></label>
            <small className="delivery-help">只写入本机 project.local.yaml，不进入 Release、Manifest 或游戏仓库提交。</small>
          </section>
          <section className="delivery-section">
            <div className="delivery-section-heading"><div><span className="section-kicker">快照</span><h3>选择 Release Manifest v2</h3></div><button className="button ghost" type="button" onClick={() => void run("preview")} disabled={busy || !v2Releases.length}><ArrowClockwise size={14} />刷新预览</button></div>
            <label className="delivery-field"><span>Release</span><select value={selectedReleaseId} onChange={(event) => { setSelectedReleaseId(event.target.value); setPreview(null); }}><option value="">未选择</option>{v2Releases.map((release) => <option key={release.id} value={release.id}>{release.name} · {release.id.slice(0, 8)} · {release.asset_count} 项</option>)}</select></label>
            {releases.length > 0 && v2Releases.length === 0 && <div className="delivery-message warning"><WarningCircle size={15} />当前只有 Release v1；请用 M3 CLI/API 创建 Manifest v2。</div>}
          </section>
          {preview && <section className="delivery-section delivery-preview" aria-label="导出预览"><div className="delivery-section-heading"><div><span className="section-kicker">只读预检</span><h3>{preview.no_op ? "目标已经同步" : `${preview.changes.filter((change) => change.action !== "keep").length} 项文件将变化`}</h3></div><span className={`delivery-status ${preview.blocking ? "blocked" : "ready"}`}>{preview.blocking ? "阻止应用" : "可应用"}</span></div><div className="delivery-change-list">{preview.changes.map((change) => <div key={String(change.path)}><code>{String(change.path)}</code><span className={`change-${String(change.action)}`}>{String(change.action)}</span></div>)}</div>{preview.issues.length > 0 && <div className="delivery-message error"><WarningCircle size={15} />{preview.issues.join("；")}</div>}</section>}
          {message && <div className="delivery-message success"><CheckCircle size={15} />{message}</div>}
          {error && <div className="delivery-message error"><WarningCircle size={15} />{error}</div>}
          <section className="delivery-section delivery-history"><div className="delivery-section-heading"><div><span className="section-kicker">Project 收据</span><h3>最近 Delivery</h3></div></div>{deliveries.length === 0 ? <p className="delivery-empty">尚无外部交付记录。</p> : <div className="delivery-history-list">{deliveries.slice(0, 5).map((delivery) => <div key={delivery.id}><span><strong>{delivery.status}</strong><small>{delivery.release_id.slice(0, 8)} · {delivery.display_path}</small></span><code>{new Date(delivery.created_at).toLocaleString()}</code></div>)}</div>}</section>
        </div>
        <footer className="drawer-footer delivery-footer"><span>应用前会再次读取 lock 并校验受管文件哈希。</span><div className="drawer-spacer" /><button className="button ghost" type="button" onClick={() => void run("verify")} disabled={busy || !selectedReleaseId}>验证</button><button className="button ghost" type="button" onClick={() => void run("rollback")} disabled={busy || !selectedReleaseId}>回滚到所选</button><button className="button primary" type="button" onClick={() => void run("apply")} disabled={busy || !selectedReleaseId || Boolean(preview?.blocking)}>{busy ? "处理中…" : "应用交付"}</button></footer>
      </section>
    </div>
  );
}
