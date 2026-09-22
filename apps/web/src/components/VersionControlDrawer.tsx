import {
  CheckCircle,
  CloudArrowUp,
  FileCode,
  GitBranch,
  Info,
  TerminalWindow,
  UploadSimple,
  X,
} from "@phosphor-icons/react";
import type { ProjectSummary } from "../types";
import { useModalFocus } from "../hooks/useModalFocus";
import { useRef } from "react";

interface VersionControlDrawerProps {
  open: boolean;
  project: ProjectSummary;
  onClose: () => void;
}

/**
 * Git is intentionally a human-owned boundary in GAMS.  This drawer makes
 * that boundary explicit instead of presenting inert commit/push controls.
 */
export function VersionControlDrawer({ open, project, onClose }: VersionControlDrawerProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useModalFocus({ open, dialogRef, initialFocusRef: closeRef, onClose });

  if (!open) return null;

  return (
    <div className="drawer-backdrop version-control-backdrop" onMouseDown={onClose}>
      <aside
        ref={dialogRef}
        className="version-control-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="version-control-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="drawer-header version-control-header">
          <div>
            <span className="section-kicker">项目历史</span>
            <h2 id="version-control-title">版本控制边界</h2>
          </div>
          <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭版本控制说明">
            <X size={19} />
          </button>
        </header>

        <div className="version-control-scroll">
          <section className="version-boundary-hero">
            <div className="version-boundary-icon"><GitBranch size={25} weight="duotone" /></div>
            <div>
              <strong>Git 操作由你确认</strong>
              <p>制作台只负责写入 Project 的事实文件，不会自动执行提交、拉取、推送或改写历史。</p>
            </div>
          </section>

          <section className="version-control-section">
            <div className="version-control-section-heading"><FileCode size={17} /><div><strong>本地提交</strong><small>适合在审阅差异后保存一个可回溯节点</small></div></div>
            <p>提交前请在 Git 客户端或终端检查变更。通常应提交以下正式内容：</p>
            <div className="version-path-list"><code>catalog/</code><code>history/</code><code>production/</code><code>approved/</code><code>releases/</code></div>
            <div className="version-note"><Info size={15} /><span><strong>不会提交</strong><small><code>workspace/</code>、本机 SQLite 和 <code>project.local.yaml</code> 属于临时或本地状态。</small></span></div>
          </section>

          <section className="version-control-section">
            <div className="version-control-section-heading"><UploadSimple size={17} /><div><strong>拉取 / 推送</strong><small>与远端仓库同步 Project 历史</small></div></div>
            <p>拉取前先保存当前工作，推送前先审阅待发布差异。凭据、远端地址和冲突解决由你的 Git 工具管理。</p>
            <div className="version-command"><TerminalWindow size={15} /><code>git status --short</code><span>只读检查</span></div>
          </section>

          <section className="version-control-section release-boundary">
            <div className="version-control-section-heading"><CloudArrowUp size={17} /><div><strong>Release 交付</strong><small>这是制作台内的真实操作</small></div></div>
            <p>Release 会生成不可变清单并执行交付预检，但它不等同于 Git commit，也不会替你推送到远端。</p>
            <div className="version-success-note"><CheckCircle size={15} /><span>当前项目：<strong>{project.name}</strong><small>{project.branch ?? "local"} · {project.path}</small></span></div>
          </section>
        </div>

        <footer className="drawer-footer version-control-footer">
          <span>需要提交或同步时，请使用你信任的 Git 客户端。</span>
          <div className="drawer-spacer" />
          <button className="button secondary" type="button" onClick={onClose}>知道了</button>
        </footer>
      </aside>
    </div>
  );
}
