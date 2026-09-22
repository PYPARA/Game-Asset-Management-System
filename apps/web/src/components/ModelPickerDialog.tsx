import { useEffect, useMemo, useRef, useState } from "react";
import { useModalFocus } from "../hooks/useModalFocus";
import { MagnifyingGlass, Plus, ArrowsClockwise, X, WarningCircle, Check, Trash } from "@phosphor-icons/react";
import { updateProviderModelOverrides } from "../lib/api";
import type { ProviderModelsResponse } from "../lib/api";
import { SelectMenu } from "./SelectMenu";
import {
  guessCapability,
  modelCapabilityLabel,
  normalizeModelCapability,
  PROVIDER_MODEL_TYPES,
} from "../lib/providerModels";
import type { ProviderModelModality, ProviderModelRecord, ProviderModelsSync } from "../types";

interface ModelPickerDialogProps {
  open: boolean;
  providerId: string;
  models: ProviderModelRecord[];
  newModelIds: string[];
  catalogApiVersion: number;
  defaultRoutes: Array<{ modality: ProviderModelModality; model: string }>;
  onClose: () => void;
  onRefresh: () => Promise<ProviderModelsResponse>;
  onCommitted: (result: ProviderModelsResponse) => void;
}

type ModelTab = "new" | "existing";

function normalizeModels(models: ProviderModelRecord[]): ProviderModelRecord[] {
  const byId = new Map<string, ProviderModelRecord>();
  for (const model of models) {
    const id = model.id.trim();
    if (!id) continue;
    byId.set(id, {
      ...model,
      id,
      modalities: [normalizeModelCapability(id, model.modalities)],
      enabled: model.enabled !== false,
    });
  }
  return [...byId.values()].sort((left, right) => left.id.localeCompare(right.id));
}

function modelError(error: unknown, fallback: string): string {
  if (!(error instanceof Error) || !error.message.trim()) return fallback;
  const diagnostic = error as Error & {
    endpoint?: string;
    status?: number;
    requestId?: string;
    hint?: string;
  };
  const details = [
    diagnostic.endpoint ? `端点：${diagnostic.endpoint}` : "",
    diagnostic.status ? `HTTP ${diagnostic.status}` : "",
    diagnostic.requestId ? `请求 ID：${diagnostic.requestId}` : "",
    diagnostic.hint ? `建议：${diagnostic.hint}` : "",
  ].filter(Boolean);
  return details.length > 0 ? `${error.message}（${details.join("；")}）` : error.message;
}

export function ModelPickerDialog({
  open,
  providerId,
  models,
  newModelIds,
  catalogApiVersion,
  defaultRoutes,
  onClose,
  onRefresh,
  onCommitted,
}: ModelPickerDialogProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [workingModels, setWorkingModels] = useState<ProviderModelRecord[]>([]);
  const [tab, setTab] = useState<ModelTab>("existing");
  const [search, setSearch] = useState("");
  const [manualId, setManualId] = useState("");
  const [manualCapability, setManualCapability] = useState<ProviderModelModality>("text");
  const [manualCapabilityLocked, setManualCapabilityLocked] = useState(false);
  const [removedModelIds, setRemovedModelIds] = useState<string[]>([]);
  const [latestNewIds, setLatestNewIds] = useState<string[]>(newModelIds);
  const [latestSync, setLatestSync] = useState<ProviderModelsSync | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [routesToClear, setRoutesToClear] = useState<ProviderModelModality[]>([]);

  useModalFocus({ open, dialogRef, initialFocusRef: searchRef, onClose });

  useEffect(() => {
    if (!open) return;
    const nextModels = normalizeModels(models);
    setWorkingModels(nextModels);
    setLatestNewIds(newModelIds);
    setTab(newModelIds.length > 0 ? "new" : "existing");
    setSearch("");
    setManualId("");
    setManualCapability("text");
    setManualCapabilityLocked(false);
    setRemovedModelIds([]);
    setLatestSync(null);
    setBusy(false);
    setMessage("");
    setRoutesToClear([]);
  }, [open]);

  const defaultIds = useMemo(
    () => new Set(defaultRoutes.map((route) => route.model)),
    [defaultRoutes],
  );
  const newIds = useMemo(() => new Set(latestNewIds), [latestNewIds]);
  const selectedCount = workingModels.filter((model) => model.enabled !== false).length;
  const currentModels = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return workingModels.filter((model) => {
      const isNew = newIds.has(model.id);
      if (tab === "new" && !isNew) return false;
      if (tab === "existing" && isNew) return false;
      return !needle || model.id.toLocaleLowerCase().includes(needle);
    });
  }, [newIds, search, tab, workingModels]);
  const currentModelIds = useMemo(
    () => new Set(currentModels.map((model) => model.id)),
    [currentModels],
  );

  const setEnabled = (modelId: string, enabled: boolean) => {
    setWorkingModels((current) => current.map((model) =>
      model.id === modelId ? { ...model, enabled } : model,
    ));
    setMessage("");
    setRoutesToClear([]);
  };

  const setCurrentListEnabled = (enabled: boolean) => {
    setWorkingModels((current) => current.map((model) => {
      if (!currentModelIds.has(model.id)) return model;
      return { ...model, enabled };
    }));
    setMessage("");
    setRoutesToClear([]);
  };

  const setCapability = (modelId: string, capability: ProviderModelModality) => {
    setWorkingModels((current) => current.map((model) =>
      model.id === modelId ? { ...model, modalities: [capability] } : model,
    ));
    setMessage("");
    setRoutesToClear([]);
  };

  const removeModel = (modelId: string) => {
    setWorkingModels((current) => current.filter((model) => model.id !== modelId));
    setLatestNewIds((current) => current.filter((id) => id !== modelId));
    setRemovedModelIds((current) => current.includes(modelId) ? current : [...current, modelId]);
    setMessage(`已标记删除 ${modelId}；点击“确定”后从渠道缓存移除。`);
    setRoutesToClear([]);
  };

  const addManualModel = () => {
    const id = manualId.trim();
    if (!id) return;
    setWorkingModels((current) => {
      const existing = current.find((model) => model.id === id);
      if (existing) {
        return current.map((model) => model.id === id
          ? { ...model, modalities: [manualCapability], enabled: true, classification: "manual" }
          : model,
        );
      }
      return [
        ...current,
        {
          id,
          modalities: [manualCapability],
          classification: "manual",
          available: false,
          enabled: true,
        },
      ];
    });
    setRemovedModelIds((current) => current.filter((modelId) => modelId !== id));
    setManualId("");
    setManualCapability("text");
    setManualCapabilityLocked(false);
    setTab("existing");
    setMessage(`已加入 ${id}；确定后写入模型目录，当前标记为未验证。`);
    setRoutesToClear([]);
  };

  const refreshModels = async () => {
    setBusy(true);
    setMessage("");
    try {
      const result = await onRefresh();
      const returned = normalizeModels(result.models);
      const returnedIds = new Set(returned.map((model) => model.id));
      const localById = new Map(workingModels.map((model) => [model.id, model]));
      const mergedReturned = returned.map((model) => {
        const local = localById.get(model.id);
        if (!local) return model;
        return {
          ...model,
          modalities: local.modalities,
          enabled: local.enabled,
          classification: local.classification === "manual" ? "manual" : model.classification,
        };
      });
      const localManual = workingModels.filter((model) =>
        model.classification === "manual" && !model.available && !returnedIds.has(model.id),
      );
      setWorkingModels(normalizeModels([...mergedReturned, ...localManual]));
      setRemovedModelIds([]);
      setLatestNewIds(result.new_model_ids ?? []);
      setLatestSync(result.models_sync ?? null);
      setTab((result.new_model_ids ?? []).length > 0 ? "new" : "existing");
      const availableCount = returned.filter((model) => model.available).length;
      setMessage(result.models_sync?.state === "manual_required"
        ? "连接可用，但供应商未提供模型列表；请继续手动登记模型。"
        : `模型列表已刷新，当前返回 ${availableCount} 个模型。`);
    } catch (error) {
      setMessage(modelError(error, "模型列表刷新失败；旧缓存仍然保留。"));
    } finally {
      setBusy(false);
    }
  };

  const affectedDefaultRoutes = (): ProviderModelModality[] => defaultRoutes.flatMap((route) => {
    const model = workingModels.find((item) => item.id === route.model);
    if (
      model &&
      model.enabled !== false &&
      normalizeModelCapability(model.id, model.modalities) === route.modality
    ) return [];
    return [route.modality];
  });

  const commit = async (confirmedRouteClearing = false) => {
    const affected = affectedDefaultRoutes();
    if (affected.length > 0 && !confirmedRouteClearing) {
      setRoutesToClear(affected);
      setMessage("");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const result = await updateProviderModelOverrides(
        providerId,
        workingModels.map((model) => ({
          id: model.id,
          modalities: model.modalities,
          classification: model.classification,
          available: model.available,
          enabled: model.enabled !== false,
        })),
        {
          catalogRefreshed: latestSync?.state === "synced" || latestSync?.state === "empty",
          requestId: latestSync?.request_id,
          removedModelIds,
          catalogApiVersion,
        },
      );
      onCommitted(result);
      onClose();
    } catch (error) {
      setMessage(modelError(error, "模型选择保存失败。"));
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  return (
    <div className="dialog-backdrop model-picker-backdrop" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className="model-picker-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="model-picker-title"
        tabIndex={-1}
        data-nested-modal="true"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="model-picker-header">
          <div>
            <span className="section-kicker">模型白名单</span>
            <h2 id="model-picker-title">选择渠道模型</h2>
          </div>
          <span className="model-picker-count">已选 {selectedCount} / {workingModels.length}</span>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭模型选择">
            <X size={18} />
          </button>
        </header>

        <div className="model-picker-body">
          <div className="model-picker-tools">
            <label className="model-search-control">
              <MagnifyingGlass size={16} />
              <input
                ref={searchRef}
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索模型"
                aria-label="搜索模型"
              />
            </label>
            <label className="manual-model-input">
              <input
                value={manualId}
                onChange={(event) => {
                  const value = event.target.value;
                  setManualId(value);
                  if (!manualCapabilityLocked) setManualCapability(guessCapability(value));
                }}
                placeholder="输入模型 ID"
                aria-label="手动输入模型 ID"
                spellCheck={false}
              />
            </label>
            <div className={`manual-model-type model-type-control ${manualCapability}`}>
              <span className="sr-only">手动模型类型</span>
              <SelectMenu
                ariaLabel="手动模型类型"
                value={manualCapability}
                options={PROVIDER_MODEL_TYPES.map((capability) => ({ value: capability, label: modelCapabilityLabel(capability) }))}
                onChange={(value) => {
                  setManualCapability(value as ProviderModelModality);
                  setManualCapabilityLocked(true);
                }}
                className="model-type-select-menu"
              />
            </div>
            <button className="button secondary compact-button" type="button" onClick={addManualModel} disabled={!manualId.trim() || busy}>
              <Plus size={15} /> 添加模型
            </button>
            <button className="button secondary compact-button" type="button" onClick={() => void refreshModels()} disabled={busy}>
              <ArrowsClockwise size={15} /> {busy ? "处理中…" : "拉取模型列表"}
            </button>
          </div>
          <p className="model-picker-help">供应商未提供模型列表时，可以手动添加模型 ID；手动模型会标记为未验证。</p>

          <div className="model-picker-tabs" role="tablist" aria-label="模型来源">
            <button type="button" role="tab" aria-selected={tab === "new"} className={tab === "new" ? "active" : ""} onClick={() => setTab("new")}>
              新获取的模型 <span>{workingModels.filter((model) => newIds.has(model.id)).length}</span>
            </button>
            <button type="button" role="tab" aria-selected={tab === "existing"} className={tab === "existing" ? "active" : ""} onClick={() => setTab("existing")}>
              已有模型 <span>{workingModels.filter((model) => !newIds.has(model.id)).length}</span>
            </button>
          </div>

          <div className="model-picker-list-heading">
            <span>当前列表已选 {currentModels.filter((model) => model.enabled !== false).length} / {currentModels.length}</span>
            <div>
              <button type="button" onClick={() => setCurrentListEnabled(true)} disabled={busy || currentModels.length === 0}>全选当前列表</button>
              <button type="button" onClick={() => setCurrentListEnabled(false)} disabled={busy || currentModels.length === 0}>取消当前列表</button>
            </div>
          </div>

          <div className="model-picker-list" aria-label="模型列表">
            {currentModels.length === 0 ? (
              <div className="model-picker-empty">
                <WarningCircle size={23} />
                <strong>{tab === "new" ? "本次还没有新获取模型" : "暂无符合条件的已有模型"}</strong>
                <p>{tab === "new" ? "点击“拉取模型列表”获取最新模型。" : "可以搜索缓存模型，或手动添加模型 ID。"}</p>
              </div>
            ) : currentModels.map((model) => {
              const enabled = model.enabled !== false;
              const defaultModel = defaultIds.has(model.id);
              const capability = normalizeModelCapability(model.id, model.modalities);
              return (
                <div key={model.id} className={`model-picker-row ${enabled ? "enabled" : "disabled"}`}>
                  <input
                    type="checkbox"
                    checked={enabled}
                    disabled={busy}
                    onChange={(event) => setEnabled(model.id, event.target.checked)}
                    aria-label={`启用模型 ${model.id}`}
                  />
                  <span className="model-picker-row-copy">
                    <strong>{model.id}</strong>
                    <small>
                      {model.classification === "manual" ? "手动 · 未验证" : model.available ? "供应商返回" : "已下线 · 保留缓存"}
                      {defaultModel ? " · 系统默认" : ""}
                    </small>
                  </span>
                  <div
                    className={`model-type-control ${capability}`}
                  >
                    <span className="sr-only">{model.id} 的模型类型</span>
                    <SelectMenu
                      ariaLabel={`${model.id} 的模型类型`}
                      value={capability}
                      options={PROVIDER_MODEL_TYPES.map((value) => ({ value, label: modelCapabilityLabel(value) }))}
                      disabled={busy}
                      onChange={(value) => setCapability(model.id, value as ProviderModelModality)}
                      className="model-type-select-menu"
                    />
                  </div>
                  <button
                    className="model-delete-button"
                    type="button"
                    disabled={busy}
                    onClick={() => removeModel(model.id)}
                    aria-label={`删除模型 ${model.id}`}
                    title="从渠道缓存删除"
                  >
                    <Trash size={14} />
                  </button>
                  {enabled ? <Check size={15} className="model-enabled-mark" /> : null}
                </div>
              );
            })}
          </div>
          {routesToClear.length > 0 ? (
            <div className="model-route-confirmation" role="alert">
              <WarningCircle size={18} />
              <div>
                <strong>这项修改会清空系统默认路由</strong>
                <p>{routesToClear.map(modelCapabilityLabel).join("、")} 默认路由将变为“未设置”。模型修改仍会正常保存。</p>
              </div>
            </div>
          ) : null}
          {message ? <div className="model-picker-message" role="status"><WarningCircle size={15} />{message}</div> : null}
        </div>

        <footer className="model-picker-footer">
          <span>取消勾选会停用模型；垃圾桶会删除缓存。已确认任务仍使用冻结快照。</span>
          <div className="drawer-spacer" />
          {routesToClear.length > 0 ? (
            <>
              <button className="button secondary" type="button" onClick={() => setRoutesToClear([])} disabled={busy}>返回修改</button>
              <button className="button primary" type="button" onClick={() => void commit(true)} disabled={busy}>确认并清空默认路由</button>
            </>
          ) : (
            <>
              <button className="button secondary" type="button" onClick={onClose} disabled={busy}>取消</button>
              <button className="button primary" type="button" onClick={() => void commit()} disabled={busy}>确定</button>
            </>
          )}
        </footer>
      </section>
    </div>
  );
}
