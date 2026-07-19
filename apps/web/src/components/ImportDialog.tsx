import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle, FolderOpen, Info, UploadSimple, Warning, X } from "@phosphor-icons/react";
import { executeEmperorImport, previewEmperorImport } from "../lib/api";

interface ImportDialogProps {
  open: boolean;
  onClose: () => void;
}

interface PreviewResult {
  assets: number;
  anchors: number;
  relations: number;
  copiedFiles: number;
  demo: boolean;
  warnings: string[];
}

type ImportOperation = "preview" | "import" | null;

export function ImportDialog({ open, onClose }: ImportDialogProps) {
  const [path, setPath] = useState("/Users/0x10/Documents/Github/Emperor-Simulator");
  const [destinationPath, setDestinationPath] = useState(
    "/Users/0x10/Documents/Github/Emperor-Simulator-Assets",
  );
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [operation, setOperation] = useState<ImportOperation>(null);
  const [message, setMessage] = useState("");
  const backdropRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const pathInputRef = useRef<HTMLInputElement>(null);
  const requestSequenceRef = useRef(0);
  const onCloseRef = useRef(onClose);
  const busy = operation !== null;

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const requestClose = useCallback(() => {
    requestSequenceRef.current += 1;
    setOperation(null);
    onCloseRef.current();
  }, []);

  useEffect(() => {
    if (!open) return;

    const previouslyFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const backdrop = backdropRef.current;
    const dialog = dialogRef.current;
    const backgroundElements = backdrop?.parentElement
      ? Array.from(backdrop.parentElement.children).filter(
          (element): element is HTMLElement => element instanceof HTMLElement && element !== backdrop,
        )
      : [];
    const backgroundState = backgroundElements.map((element) => ({
      element,
      inert: element.inert,
      ariaHidden: element.getAttribute("aria-hidden"),
    }));

    pathInputRef.current?.focus();
    backgroundElements.forEach((element) => {
      element.inert = true;
      element.setAttribute("aria-hidden", "true");
    });

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        requestClose();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;

      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");

      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable.at(-1);
      const activeElement = document.activeElement;
      if (event.shiftKey && (activeElement === first || !dialog.contains(activeElement))) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      requestSequenceRef.current += 1;
      document.removeEventListener("keydown", onKeyDown);
      backgroundState.forEach(({ element, inert, ariaHidden }) => {
        element.inert = inert;
        if (ariaHidden === null) element.removeAttribute("aria-hidden");
        else element.setAttribute("aria-hidden", ariaHidden);
      });
      previouslyFocused?.focus();
    };
  }, [open, requestClose]);

  const updatePath = (nextPath: string) => {
    requestSequenceRef.current += 1;
    setPath(nextPath);
    setPreview(null);
    setPreviewPath(null);
    setOperation(null);
    setMessage("");
  };

  const record = (value: unknown): Record<string, unknown> | null =>
    typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;

  const runPreview = async () => {
    const requestedPath = path.trim();
    const requestSequence = requestSequenceRef.current + 1;
    requestSequenceRef.current = requestSequence;
    setOperation("preview");
    setPreview(null);
    setPreviewPath(null);
    setMessage("");
    try {
      const result = await previewEmperorImport(requestedPath);
      if (requestSequenceRef.current !== requestSequence) return;
      const payload = record(result.preview) ?? result;
      const runtime = record(payload.runtime_assets);
      const anchors = record(payload.anchors);
      const runtimeItems = Array.isArray(runtime?.items) ? runtime.items : [];
      const relationCount = runtimeItems.reduce((total, item) => {
        const entry = record(item);
        if (!entry) return total;
        return total + Number(Boolean(entry.fallbackKey)) + Number(Boolean(entry.characterId)) + Number(entry.type === "icon");
      }, 0);
      setPreview({
        assets: Number(runtime?.count ?? 0),
        anchors: Number(anchors?.count ?? 0),
        relations: relationCount,
        copiedFiles: 0,
        demo: false,
        warnings: Array.isArray(payload.warnings) ? payload.warnings.map(String) : [],
      });
      setPreviewPath(requestedPath);
    } catch (error) {
      if (requestSequenceRef.current !== requestSequence) return;
      setMessage(
        error instanceof Error
          ? `无法读取项目：${error.message}`
          : "无法读取项目。请确认目录存在且本地后端已启动。",
      );
    } finally {
      if (requestSequenceRef.current === requestSequence) setOperation(null);
    }
  };

  const runImport = async () => {
    const requestedPath = path.trim();
    if (!preview || preview.demo || previewPath !== requestedPath) {
      setMessage("当前目录尚未完成有效 dry-run，无法登记。请重新扫描后再继续。");
      return;
    }

    const requestSequence = requestSequenceRef.current + 1;
    requestSequenceRef.current = requestSequence;
    setOperation("import");
    setMessage("");
    try {
      await executeEmperorImport(requestedPath, destinationPath.trim());
      if (requestSequenceRef.current !== requestSequence) return;
      setMessage("导入完成。旧项目保持只读，媒体继续引用原路径且未复制大文件。");
    } catch (error) {
      if (requestSequenceRef.current !== requestSequence) return;
      setMessage(error instanceof Error ? `导入失败：${error.message}` : "导入失败。");
    } finally {
      if (requestSequenceRef.current === requestSequence) setOperation(null);
    }
  };

  if (!open) return null;

  return (
    <div ref={backdropRef} className="dialog-backdrop" onMouseDown={requestClose}>
      <section
        ref={dialogRef}
        className="import-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-title"
        aria-describedby="import-description"
        aria-busy={busy}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div className="dialog-title-icon">
            <UploadSimple size={22} />
          </div>
          <div>
            <span className="section-kicker">项目登记</span>
            <h2 id="import-title">导入 Emperor-Simulator</h2>
          </div>
          <button className="icon-button" type="button" onClick={requestClose} aria-label="关闭导入窗口">
            <X size={19} />
          </button>
        </header>

        <div className="dialog-body">
          <label className="path-field">
            <span>项目目录</span>
            <div>
              <FolderOpen size={18} />
              <input
                ref={pathInputRef}
                value={path}
                onChange={(event) => updatePath(event.target.value)}
                disabled={operation === "import"}
                spellCheck={false}
              />
            </div>
          </label>

          <label className="path-field">
            <span>管理项目目录</span>
            <div>
              <FolderOpen size={18} />
              <input
                value={destinationPath}
                onChange={(event) => { setDestinationPath(event.target.value); setMessage(""); }}
                disabled={operation === "import"}
                spellCheck={false}
              />
            </div>
          </label>

          <div className="import-principle" id="import-description">
            <Info size={18} />
            <p>先对旧项目执行只读 dry-run。正式导入只在管理项目目录创建 `.game-assets` 元数据并引用原媒体，不复制现有大文件。</p>
          </div>

          {preview ? (
            <div className={`preview-report ${preview.demo ? "demo" : ""}`}>
              <div className="preview-report-header">
                {preview.demo ? <Warning size={20} weight="fill" /> : <CheckCircle size={20} weight="fill" />}
                <strong>{preview.demo ? "离线演示预览（未扫描目录）" : "扫描完成"}</strong>
              </div>
              <dl>
                <div><dt>媒体资产</dt><dd>{preview.assets}</dd></div>
                <div><dt>风格锚点</dt><dd>{preview.anchors}</dd></div>
                <div><dt>显式关系</dt><dd>{preview.relations}</dd></div>
                <div><dt>复制文件</dt><dd>{preview.copiedFiles}</dd></div>
              </dl>
              {preview.warnings.map((warning) => (
                <p className="preview-warning" key={warning}><Warning size={16} /> {warning}</p>
              ))}
              {preview.demo && (
                <p className="preview-demo-note" role="status">
                  这些数字仅用于界面演示，不代表当前目录的扫描结果；正式登记已禁用。
                </p>
              )}
            </div>
          ) : (
            <div className="preview-empty">
              <span>dry-run 将识别风格圣经、3 个锚点、186 个媒体资产及其 QA 证据。</span>
            </div>
          )}

          {message && <p className="dialog-message" role="status">{message}</p>}
        </div>

        <footer className="dialog-footer">
          <button className="button secondary" type="button" onClick={runPreview} disabled={busy || !path.trim()}>
            {operation === "preview" ? "扫描中…" : "执行 dry-run"}
          </button>
          <button
            className="button primary"
            type="button"
            onClick={runImport}
            disabled={busy || !preview || preview.demo || previewPath !== path.trim() || !destinationPath.trim()}
          >
            {operation === "import" ? "登记中…" : "确认登记项目"}
          </button>
        </footer>
      </section>
    </div>
  );
}
