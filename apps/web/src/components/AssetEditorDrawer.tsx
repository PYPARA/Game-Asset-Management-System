import { useEffect, useMemo, useState } from "react";
import { ArrowsLeftRight, CheckCircle, FloppyDisk, WarningCircle, X } from "@phosphor-icons/react";
import { fieldLabel } from "../lib/labels";
import type { GameAsset } from "../types";

interface AssetEditorDrawerProps {
  asset: GameAsset | null;
  allAssets: GameAsset[];
  onClose: () => void;
  onSave: (asset: GameAsset, content: unknown) => Promise<void>;
}

function stableJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function sameValue(left: unknown, right: unknown): boolean {
  return stableJson(left) === stableJson(right);
}

function relationValue(asset: GameAsset, field: string): string {
  if (field === "participants") return asset.key.replace(/^entity\.character\./, "");
  return asset.key;
}

function relationOptions(field: string, assets: GameAsset[]): GameAsset[] {
  if (field === "participants") return assets.filter((asset) => asset.subtype === "character");
  if (field === "nodes") return assets.filter((asset) => asset.kind === "content" && asset.subtype === "event");
  return assets.filter((asset) => asset.kind !== "production");
}

function validateDraft(asset: GameAsset, baseline: unknown, draft: unknown, assets: GameAsset[]): string[] {
  const errors: string[] = [];
  if (asset.revisionFormat === "markdown") {
    if (typeof draft !== "string" || !draft.trim()) errors.push("Markdown 内容不能为空。");
    return errors;
  }
  if (!draft || typeof draft !== "object" || Array.isArray(draft)) {
    return ["JSON 修订必须是对象。"];
  }
  const record = draft as Record<string, unknown>;
  const base = baseline && typeof baseline === "object" && !Array.isArray(baseline)
    ? baseline as Record<string, unknown>
    : {};
  if (typeof record.id !== "string" || !record.id.trim()) errors.push("原始 ID 不能为空。");
  if (!sameValue(record.id, base.id)) errors.push("原始 ID 不允许修改。");
  if (!sameValue(record.source, base.source)) errors.push("来源信息不允许修改。");
  for (const field of ["participants", "nodes", "references"]) {
    if (!(field in record)) continue;
    const values = record[field];
    if (!Array.isArray(values) || values.some((value) => typeof value !== "string")) {
      errors.push(`${fieldLabel(field)}必须是字符串列表。`);
      continue;
    }
    const allowed = new Set(relationOptions(field, assets).map((item) => relationValue(item, field)));
    const missing = values.filter((value) => !allowed.has(String(value)));
    if (missing.length) errors.push(`${fieldLabel(field)}包含不存在的资产：${missing[0]}`);
  }
  return errors;
}

function ComplexField({
  name,
  value,
  onChange,
}: {
  name: string;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const [text, setText] = useState(stableJson(value));
  const [error, setError] = useState("");
  useEffect(() => setText(stableJson(value)), [value]);
  return (
    <label className="editor-field editor-field-wide">
      <span>{fieldLabel(name)}</span>
      <textarea
        rows={Math.min(10, Math.max(4, text.split("\n").length))}
        value={text}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          try {
            onChange(JSON.parse(next));
            setError("");
          } catch {
            setError("JSON 格式无效");
          }
        }}
      />
      {error && <small className="editor-error">{error}</small>}
    </label>
  );
}

export function AssetEditorDrawer({ asset, allAssets, onClose, onSave }: AssetEditorDrawerProps) {
  const [mode, setMode] = useState<"form" | "json">("form");
  const [draft, setDraft] = useState<unknown>(asset?.revisionContent ?? null);
  const [rawJson, setRawJson] = useState("");
  const [rawError, setRawError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const content = asset?.revisionContent ?? null;
    setDraft(content);
    setRawJson(stableJson(content));
    setRawError("");
    setMode(asset?.revisionFormat === "markdown" ? "form" : "form");
  }, [asset?.id, asset?.candidateRevisionId]);

  const baseline = asset?.revisionContent ?? null;
  const dirty = !sameValue(baseline, draft);
  const errors = useMemo(
    () => asset ? validateDraft(asset, baseline, draft, allAssets) : [],
    [allAssets, asset, baseline, draft],
  );

  if (!asset) return null;

  const close = () => {
    if (dirty && !window.confirm("当前修改尚未保存，确定关闭编辑器吗？")) return;
    onClose();
  };
  const updateObjectField = (name: string, value: unknown) => {
    const next = { ...(draft as Record<string, unknown>), [name]: value };
    setDraft(next);
    setRawJson(stableJson(next));
  };
  const save = async () => {
    if (!dirty || errors.length || rawError) return;
    setSaving(true);
    try {
      await onSave(asset, draft);
      onClose();
    } finally {
      setSaving(false);
    }
  };
  const record = draft && typeof draft === "object" && !Array.isArray(draft)
    ? draft as Record<string, unknown>
    : {};

  return (
    <div className="editor-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && close()}>
      <section className="asset-editor-drawer" role="dialog" aria-modal="true" aria-label={`编辑 ${asset.name}`}>
        <header className="asset-editor-header">
          <div><small>保存后进入审核，不会覆盖当前版本</small><strong>{asset.name}</strong><span>{asset.key}</span></div>
          <button className="icon-button" type="button" onClick={close} aria-label="关闭编辑器"><X size={19} /></button>
        </header>

        <div className="asset-editor-toolbar">
          {asset.revisionFormat === "json" && (
            <div className="segmented-control" aria-label="编辑方式">
              <button type="button" className={mode === "form" ? "active" : ""} onClick={() => setMode("form")}>字段表单</button>
              <button type="button" className={mode === "json" ? "active" : ""} onClick={() => setMode("json")}>原始 JSON</button>
            </div>
          )}
          <span className={dirty ? "editor-dirty" : "editor-clean"}>{dirty ? "有未保存修改" : "尚未修改"}</span>
        </div>

        <div className="asset-editor-body">
          <div className="asset-editor-fields">
            {asset.revisionFormat === "markdown" ? (
              <label className="editor-field editor-markdown-field">
                <span>Markdown 正文</span>
                <textarea value={typeof draft === "string" ? draft : ""} onChange={(event) => setDraft(event.target.value)} />
              </label>
            ) : mode === "json" ? (
              <label className="editor-field editor-json-field">
                <span>原始 JSON</span>
                <textarea
                  spellCheck={false}
                  value={rawJson}
                  onChange={(event) => {
                    const next = event.target.value;
                    setRawJson(next);
                    try {
                      setDraft(JSON.parse(next));
                      setRawError("");
                    } catch {
                      setRawError("JSON 格式无效，无法保存。");
                    }
                  }}
                />
                {rawError && <small className="editor-error">{rawError}</small>}
              </label>
            ) : (
              <div className="editor-form-grid">
                {Object.entries(record).map(([name, value]) => {
                  if (name === "id" || name === "source") {
                    return <div className="editor-readonly-field" key={name}><span>{fieldLabel(name)}</span><pre>{typeof value === "string" ? value : stableJson(value)}</pre></div>;
                  }
                  if (["participants", "nodes", "references"].includes(name) && Array.isArray(value)) {
                    const options = relationOptions(name, allAssets);
                    return (
                      <label className="editor-field editor-field-wide" key={name}>
                        <span>{fieldLabel(name)}</span>
                        <select
                          multiple
                          value={value.map(String)}
                          onChange={(event) => updateObjectField(name, Array.from(event.currentTarget.selectedOptions, (option) => option.value))}
                        >
                          {options.map((option) => <option key={option.id} value={relationValue(option, name)}>{option.name} · {option.subtypeLabel}</option>)}
                        </select>
                        <small>按住 Command/Ctrl 可多选；保存时会同步资产关系。</small>
                      </label>
                    );
                  }
                  if (typeof value === "string") {
                    const long = value.length > 80 || ["text", "description", "summary", "synopsis", "notes"].includes(name);
                    return (
                      <label className={`editor-field ${long ? "editor-field-wide" : ""}`} key={name}>
                        <span>{fieldLabel(name)}</span>
                        {long
                          ? <textarea rows={5} value={value} onChange={(event) => updateObjectField(name, event.target.value)} />
                          : <input value={value} onChange={(event) => updateObjectField(name, event.target.value)} />}
                      </label>
                    );
                  }
                  if (typeof value === "number") return <label className="editor-field" key={name}><span>{fieldLabel(name)}</span><input type="number" value={value} onChange={(event) => updateObjectField(name, Number(event.target.value))} /></label>;
                  if (typeof value === "boolean") return <label className="editor-boolean-field" key={name}><input type="checkbox" checked={value} onChange={(event) => updateObjectField(name, event.target.checked)} /><span>{fieldLabel(name)}</span></label>;
                  return <ComplexField key={name} name={name} value={value} onChange={(next) => updateObjectField(name, next)} />;
                })}
              </div>
            )}
          </div>

          <aside className="asset-editor-diff" aria-label="修改差异">
            <header><ArrowsLeftRight size={17} /><strong>版本差异</strong></header>
            <div className="diff-column baseline"><span>基准版本</span><pre>{asset.revisionFormat === "markdown" ? String(baseline ?? "") : stableJson(baseline)}</pre></div>
            <div className="diff-column draft"><span>待保存候选</span><pre>{asset.revisionFormat === "markdown" ? String(draft ?? "") : stableJson(draft)}</pre></div>
          </aside>
        </div>

        <footer className="asset-editor-footer">
          <div className={errors.length || rawError ? "editor-validation invalid" : "editor-validation valid"}>
            {errors.length || rawError ? <WarningCircle size={17} weight="fill" /> : <CheckCircle size={17} weight="fill" />}
            <span>{rawError || errors[0] || "字段与引用检查通过"}</span>
          </div>
          <button className="button secondary" type="button" onClick={close}>取消</button>
          <button className="button primary" type="button" disabled={!dirty || Boolean(errors.length) || Boolean(rawError) || saving} onClick={() => void save()}>
            <FloppyDisk size={16} /> {saving ? "正在保存…" : "保存为候选"}
          </button>
        </footer>
      </section>
    </div>
  );
}
