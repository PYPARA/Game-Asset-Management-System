import { type FormEvent, useEffect, useRef, useState } from "react";
import { CheckCircle, FolderSimplePlus, Info, PencilSimple, Plus, X } from "@phosphor-icons/react";
import { createProject, fetchSystemInfo, updateProject } from "../lib/api";
import type { ProjectSummary } from "../types";

interface ProjectDialogProps {
  open: boolean;
  project?: ProjectSummary | null;
  onClose: () => void;
  onSaved?: () => Promise<void> | void;
}

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export function ProjectDialog({ open, project = null, onClose, onSaved }: ProjectDialogProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const directoryInputRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const busyRef = useRef(false);
  const [projectsRoot, setProjectsRoot] = useState("Game-Projects");
  const [directoryName, setDirectoryName] = useState("");
  const [name, setName] = useState("");
  const [defaultLanguage, setDefaultLanguage] = useState("zh-CN");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [succeeded, setSucceeded] = useState(false);
  const editing = Boolean(project?.id);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setDirectoryName("");
    setName(project?.name ?? "");
    setDefaultLanguage("zh-CN");
    setMessage("");
    setSucceeded(false);
    if (!project) {
      void fetchSystemInfo().then((info) => setProjectsRoot(info.projects_root));
    }
    window.setTimeout(() => {
      const target = project
        ? dialogRef.current?.querySelector<HTMLInputElement>('input[name="project-name"]')
        : directoryInputRef.current;
      target?.focus();
      if (project) target?.select();
    }, 0);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busyRef.current) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previousFocusRef.current?.focus();
    };
  }, [open, project?.id]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim() || (!editing && !directoryName.trim())) return;
    setBusy(true);
    setMessage("");
    setSucceeded(false);
    try {
      if (project) {
        await updateProject(project.id, name.trim());
        await onSaved?.();
        setSucceeded(true);
        setMessage("项目名称已保存，并已同步到 project.yaml。");
        return;
      }
      const result = await createProject(directoryName.trim(), name.trim(), defaultLanguage);
      await onSaved?.();
      setSucceeded(true);
      setMessage(`创建完成：已索引 ${result.scan.assets_indexed} 个资产、${result.scan.revisions_indexed} 个修订。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Project 创建失败。");
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;
  return (
    <div className="dialog-backdrop" onMouseDown={() => !busy && onClose()}>
      <section
        ref={dialogRef}
        className="project-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-dialog-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div className="dialog-title-icon">{editing ? <PencilSimple size={22} /> : <Plus size={22} />}</div>
          <div><span className="section-kicker">Game-Projects</span><h2 id="project-dialog-title">{editing ? "编辑项目" : "新建 Project"}</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label={`关闭${editing ? "编辑项目" : "新建 Project"}窗口`} disabled={busy}><X size={19} /></button>
        </header>

        <form onSubmit={submit}>
          <div className="dialog-body project-form">
            <div className="project-root-card">
              <FolderSimplePlus size={20} weight="duotone" />
              <span><small>{editing ? "项目目录（不可修改）" : "Project 容器"}</small><strong title={project?.path ?? projectsRoot}>{project?.path ?? projectsRoot}</strong></span>
            </div>

            {!editing && (
              <label className="project-field">
                <span>目录名称</span>
                <input
                  ref={directoryInputRef}
                  value={directoryName}
                  onChange={(event) => setDirectoryName(event.target.value)}
                  placeholder="例如 Emperor-Simulator"
                  disabled={busy}
                  spellCheck={false}
                  pattern="[A-Za-z0-9][A-Za-z0-9._-]*"
                  required
                />
                <small>只创建在上方容器内；已有 Project 会自动出现在列表中。</small>
              </label>
            )}

            <div className={editing ? "" : "project-form-grid"}>
              <label className="project-field">
                <span>项目名称</span>
                <input name="project-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="显示名称" disabled={busy} maxLength={200} required />
              </label>
              {!editing && (
                <label className="project-field">
                  <span>默认语言</span>
                  <select value={defaultLanguage} onChange={(event) => setDefaultLanguage(event.target.value)} disabled={busy}>
                    <option value="zh-CN">简体中文</option>
                    <option value="zh-TW">繁体中文</option>
                    <option value="en-US">English</option>
                    <option value="ja-JP">日本語</option>
                  </select>
                </label>
              )}
            </div>

            <div className="project-principle"><Info size={18} /><p>{editing ? "只修改项目的显示名称；项目目录、稳定 ID 和已有资产不会改变。" : "系统将创建根级 project.yaml、catalog、history、production、approved 和 workspace；二进制资源由普通 Git 记录。"}</p></div>
            {message && <p className={`dialog-message ${succeeded ? "success" : "error"}`} role={succeeded ? "status" : "alert"}>{succeeded && <CheckCircle size={16} weight="fill" />} {message}</p>}
          </div>
          <footer className="dialog-footer">
            <button className="button secondary" type="button" onClick={onClose} disabled={busy}>取消</button>
            <button className="button primary" type="submit" disabled={busy || !name.trim() || (!editing && !directoryName.trim())}>{busy ? (editing ? "保存中…" : "创建中…") : (editing ? "保存名称" : "创建并扫描")}</button>
          </footer>
        </form>
      </section>
    </div>
  );
}
