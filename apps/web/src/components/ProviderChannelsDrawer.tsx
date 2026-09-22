import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArrowCounterClockwise,
  ArrowLeft,
  CheckCircle,
  Eye,
  EyeSlash,
  Key,
  LockKeyOpen,
  PencilSimple,
  Plus,
  SlidersHorizontal,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import {
  archiveProvider,
  createProviderProfile,
  ensureProviderProfile,
  fetchProviderModelsDirect,
  fetchGenerationProviders,
  fetchProviderDefaults,
  lockProvider,
  restoreProvider,
  unlockProvider,
  updateProviderProfile,
} from "../lib/api";
import type { ProviderModelsResponse } from "../lib/api";
import {
  deleteCredential,
  hasCredential,
  isProviderUrlAllowed,
  normalizeProviderUrl,
  readCredential,
  saveCredential,
} from "../lib/credentials";
import { useModalFocus } from "../hooks/useModalFocus";
import { guessCapability, modelCapabilityLabel, normalizeModelCapability } from "../lib/providerModels";
import type {
  GenerationProviderProfile,
  ProviderModelModality,
  ProviderModelRecord,
  ProviderModelsSync,
  ProviderProfile,
} from "../types";
import { ModelPickerDialog } from "./ModelPickerDialog";
import { SelectMenu } from "./SelectMenu";

const NEW_PROVIDER_ID = "__new_provider__";
const MODEL_TYPES: ProviderModelModality[] = ["text", "image", "video", "audio"];

const EMPTY_PROFILE: ProviderProfile = {
  id: "",
  name: "新渠道",
  baseUrl: "https://api.openai.com/v1",
  allowPrivateNetwork: false,
  credentialMode: "required",
  modelDiscoveryMode: "auto",
  modelsPath: "models",
};

const SYNC_LABELS: Record<string, string> = {
  never: "尚未拉取模型",
  synced: "模型列表已同步",
  empty: "供应商返回空列表",
  manual_required: "连接可用，需手动登记模型",
  error: "模型列表拉取失败",
};

const EMPTY_SYNC: ProviderModelsSync = {
  state: "never",
  checked_at: null,
  endpoint: null,
  status_code: null,
  message: null,
  hint: null,
  request_id: null,
};

function toDraft(profile: GenerationProviderProfile): ProviderProfile {
  return {
    id: profile.id,
    name: profile.name,
    baseUrl: profile.base_url,
    allowPrivateNetwork: profile.allow_private_network,
    credentialMode: profile.credential_mode ?? "required",
    modelDiscoveryMode: profile.model_discovery_mode ?? "auto",
    modelsPath: profile.models_path ?? "models",
  };
}

function errorMessage(error: unknown, fallback: string): string {
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

type DirectProviderFailure = Error & {
  status: number | null;
  endpoint: string;
  requestId?: string;
  hint?: string;
};

function isDirectProviderFailure(error: unknown): error is DirectProviderFailure {
  return error instanceof Error
    && typeof (error as Partial<DirectProviderFailure>).endpoint === "string"
    && "status" in error;
}

function mergeDirectModels(
  existing: ProviderModelRecord[],
  discovered: Array<Record<string, unknown>>,
): { models: ProviderModelRecord[]; newModelIds: string[] } {
  const catalog = new Map(existing.map((model) => [model.id, {
    ...model,
    modalities: [normalizeModelCapability(model.id, model.modalities)],
    available: false,
  }]));
  const existingIds = new Set(existing.map((model) => model.id));
  const newModelIds: string[] = [];
  for (const raw of discovered) {
    const id = String(raw.id ?? "").trim();
    if (!id) continue;
    const previous = catalog.get(id);
    catalog.set(id, {
      id,
      modalities: previous?.modalities ?? [guessCapability(id)],
      classification: previous?.classification === "manual" ? "manual" : "heuristic",
      available: true,
      enabled: previous?.enabled !== false,
    });
    if (!existingIds.has(id)) newModelIds.push(id);
  }
  return {
    models: [...catalog.values()].sort((left, right) => left.id.localeCompare(right.id)),
    newModelIds: newModelIds.sort((left, right) => left.localeCompare(right)),
  };
}

function localModelsResponse(
  provider: GenerationProviderProfile,
  models: ProviderModelRecord[],
  sync: ProviderModelsSync,
  newModelIds: string[] = [],
  refreshedAt: string | null = provider.models_refreshed_at,
): ProviderModelsResponse {
  return {
    provider_profile_id: provider.id,
    models,
    refreshed_at: refreshedAt,
    new_model_ids: newModelIds,
    cleared_default_routes: [],
    model_catalog_api_version: provider.model_catalog_api_version,
    models_sync: sync,
  };
}

function providerReady(provider: GenerationProviderProfile | undefined): boolean {
  if (!provider) return false;
  return provider.kind === "fake"
    || provider.credential_mode === "none"
    || provider.credential_mode === "optional"
    || provider.is_unlocked;
}

function formatSyncTime(value: string | null | undefined): string {
  if (!value) return "尚未同步";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN", { hour12: false });
}

function statusCopy(provider: GenerationProviderProfile | undefined, credentialStored = false): string {
  if (!provider) return "新渠道，保存后可连接";
  if (!provider.is_active) return "已归档";
  if (providerReady(provider)) return provider.credential_mode === "none" ? "连接可用 · 无 API Key" : "连接可用";
  return credentialStored ? "已保存 API Key · 等待解锁" : "等待 API Key";
}

function enabledTypeCounts(provider: GenerationProviderProfile) {
  return Object.fromEntries(MODEL_TYPES.map((type) => [
    type,
    provider.models.filter((model) =>
      model.enabled !== false && normalizeModelCapability(model.id, model.modalities) === type,
    ).length,
  ])) as Record<ProviderModelModality, number>;
}

let legacyMigration: Promise<void> | null = null;

async function migrateLegacyProvider(): Promise<void> {
  const stored = localStorage.getItem("game-assets.provider-profile");
  if (!stored) return;
  try {
    const parsed = JSON.parse(stored) as Partial<ProviderProfile>;
    const legacy: ProviderProfile = {
      ...EMPTY_PROFILE,
      ...parsed,
      id: parsed.id ?? "",
    };
    const remote = await ensureProviderProfile(legacy);
    if (legacy.id && legacy.id !== remote.id) {
      const credential = await readCredential(legacy.id);
      if (credential) {
        await saveCredential(remote.id, credential);
        await deleteCredential(legacy.id);
      }
    }
    localStorage.removeItem("game-assets.provider-profile");
  } catch {
    // A malformed legacy record must not prevent the provider list from opening.
  }
}

interface ProviderChannelsDrawerProps {
  open: boolean;
  onClose: () => void;
}

export function ProviderChannelsDrawer({ open, onClose }: ProviderChannelsDrawerProps) {
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [view, setView] = useState<"list" | "edit">("list");
  const [selectedId, setSelectedId] = useState("");
  const [highlightedId, setHighlightedId] = useState("");
  const [draft, setDraft] = useState<ProviderProfile>({ ...EMPTY_PROFILE });
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [credentialStored, setCredentialStored] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [archivedOpen, setArchivedOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [newModelIds, setNewModelIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"success" | "warning" | "error">("success");

  const providersQuery = useQuery({
    queryKey: ["generation-providers"],
    queryFn: fetchGenerationProviders,
    enabled: open,
  });
  const defaultsQuery = useQuery({
    queryKey: ["provider-defaults"],
    queryFn: fetchProviderDefaults,
    enabled: open,
  });

  const providers = providersQuery.data ?? [];
  const activeProviders = providers.filter((provider) => provider.is_active);
  const archivedProviders = providers.filter((provider) => !provider.is_active);
  const selected = providers.find((provider) => provider.id === selectedId);
  const isNew = selectedId === NEW_PROVIDER_ID;
  const currentSync = selected?.models_sync ?? EMPTY_SYNC;
  const enabledModels = (selected?.models ?? []).filter((model) => model.enabled !== false);
  const defaultRoutes = useMemo(() => MODEL_TYPES.flatMap((modality) => {
    const route = defaultsQuery.data?.[modality];
    return route && route.provider_profile_id === selected?.id
      ? [{ modality, model: route.model }]
      : [];
  }), [defaultsQuery.data, selected?.id]);

  useModalFocus({
    open,
    dialogRef,
    initialFocusRef: closeRef,
    onClose: () => {
      if (pickerOpen) setPickerOpen(false);
      else onClose();
    },
  });

  useEffect(() => {
    if (!open) return;
    setView("list");
    setSelectedId("");
    setPickerOpen(false);
    setMessage("");
    if (!legacyMigration) legacyMigration = migrateLegacyProvider();
    void legacyMigration
      .then(() => queryClient.invalidateQueries({ queryKey: ["generation-providers"] }))
      .catch(() => undefined);
  }, [open, queryClient]);

  useEffect(() => {
    if (!selected) return;
    setDraft(toDraft(selected));
    setApiKey("");
    setShowKey(false);
    setAdvancedOpen(false);
    setNewModelIds([]);
    void hasCredential(selected.id).then(setCredentialStored).catch(() => setCredentialStored(false));
  }, [selected?.id]);

  const update = <K extends keyof ProviderProfile>(key: K, value: ProviderProfile[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setMessage("");
  };

  const applyModelsResponse = (response: ProviderModelsResponse) => {
    setNewModelIds(response.new_model_ids ?? []);
    queryClient.setQueryData<GenerationProviderProfile[]>(["generation-providers"], (current) =>
      current?.map((provider) => provider.id === response.provider_profile_id
        ? {
            ...provider,
            models: response.models.map((model) => ({ ...model, enabled: model.enabled !== false })),
            models_refreshed_at: response.refreshed_at,
            models_sync: response.models_sync ?? provider.models_sync,
            model_catalog_api_version: response.model_catalog_api_version,
          }
        : provider,
      ),
    );
  };

  const invalidateProviderData = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["generation-providers"] }),
      queryClient.invalidateQueries({ queryKey: ["provider-defaults"] }),
    ]);
  };

  const beginNewProvider = () => {
    setView("edit");
    setSelectedId(NEW_PROVIDER_ID);
    setDraft({ ...EMPTY_PROFILE });
    setApiKey("");
    setCredentialStored(false);
    setShowKey(false);
    setNewModelIds([]);
    setAdvancedOpen(false);
    setMessage("");
  };

  const beginEdit = (provider: GenerationProviderProfile) => {
    setSelectedId(provider.id);
    setDraft(toDraft(provider));
    setView("edit");
    setMessage("");
  };

  const openModels = (provider: GenerationProviderProfile) => {
    setSelectedId(provider.id);
    setDraft(toDraft(provider));
    setNewModelIds([]);
    setMessage("");
    setPickerOpen(true);
  };

  const returnToList = () => {
    setView("list");
    setMessage("");
  };

  const persistConnection = async (): Promise<GenerationProviderProfile | null> => {
    const baseUrl = normalizeProviderUrl(draft.baseUrl, draft.allowPrivateNetwork);
    if (!baseUrl) {
      setMessageTone("error");
      setMessage("接口地址必须使用 HTTPS（仅 localhost/loopback 可用 HTTP），且不能包含凭据、查询参数或片段。");
      return null;
    }
    setBusy(true);
    setMessage("");
    try {
      const normalized = { ...draft, baseUrl };
      const saved = draft.id
        ? await updateProviderProfile(draft.id, normalized)
        : await createProviderProfile(normalized);
      const inputKey = apiKey.trim();
      const storedKey = inputKey || await readCredential(saved.id);
      if (inputKey) {
        await saveCredential(saved.id, inputKey);
        setCredentialStored(true);
        setApiKey("");
      }
      if (saved.kind !== "fake" && storedKey) await unlockProvider(saved.id, storedKey);
      queryClient.setQueryData<GenerationProviderProfile[]>(["generation-providers"], (current = []) => {
        const exists = current.some((provider) => provider.id === saved.id);
        return exists
          ? current.map((provider) => provider.id === saved.id ? saved : provider)
          : [...current, saved];
      });
      setSelectedId(saved.id);
      setHighlightedId(saved.id);
      setDraft(toDraft(saved));
      setView("edit");
      const credentialMode = saved.credential_mode ?? draft.credentialMode;
      setMessageTone(credentialMode === "required" && !storedKey ? "warning" : "success");
      setMessage(credentialMode === "required" && !storedKey
        ? "渠道已保存；填写 API Key 后再拉取模型列表。"
        : "渠道已保存。模型列表只会在你明确点击拉取时访问供应商。 ");
      await invalidateProviderData();
      return saved;
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "渠道保存失败。"));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const refreshModels = async (): Promise<ProviderModelsResponse | null> => {
    let provider = selected;
    if (!provider && view === "edit") {
      const saved = await persistConnection();
      provider = saved ?? undefined;
    }
    if (!provider) return null;
    setBusy(true);
    setMessage("");
    try {
      const key = apiKey.trim() || await readCredential(provider.id);
      if (provider.kind !== "fake" && !key && provider.credential_mode === "required") {
        throw new Error("请先保存 API Key，或在高级设置中将凭据模式改为可选/无凭据。");
      }
      const checkedAt = new Date().toISOString();
      let result: ProviderModelsResponse;
      if (provider.kind === "fake") {
        const merged = mergeDirectModels(provider.models, []);
        result = localModelsResponse(provider, merged.models, {
          state: "synced",
          checked_at: checkedAt,
          endpoint: `${provider.base_url.replace(/\/$/, "")}/${provider.models_path ?? "models"}`,
          status_code: 200,
          message: "模型目录已同步。",
          hint: null,
          request_id: null,
        }, merged.newModelIds, checkedAt);
      } else {
        try {
          const direct = await fetchProviderModelsDirect({
            baseUrl: provider.base_url,
            modelsPath: provider.models_path,
            credentialMode: provider.credential_mode,
          }, key ?? "");
          const merged = mergeDirectModels(provider.models, direct.models);
          result = localModelsResponse(provider, merged.models, {
            state: direct.models.length > 0 ? "synced" : "empty",
            checked_at: checkedAt,
            endpoint: direct.endpoint,
            status_code: direct.statusCode,
            message: direct.models.length > 0 ? "模型目录已同步。" : "供应商返回了空模型列表。",
            hint: direct.models.length > 0 ? null : "请检查供应商模型权限，或手动登记模型 ID。",
            request_id: direct.requestId,
          }, merged.newModelIds, checkedAt);
        } catch (error) {
          const diagnostic = isDirectProviderFailure(error)
            ? error
            : Object.assign(new Error(error instanceof Error ? error.message : "模型列表拉取失败。"), {
                status: null,
                endpoint: provider.base_url,
                hint: "检查 Base URL、模型列表路径和本机网络。",
              }) as DirectProviderFailure;
          const manualRequired = diagnostic.status === 404 || diagnostic.status === 405;
          const failure = localModelsResponse(provider, provider.models, {
            state: manualRequired ? "manual_required" : "error",
            checked_at: checkedAt,
            endpoint: diagnostic.endpoint,
            status_code: diagnostic.status,
            message: manualRequired ? "供应商未提供可用的模型列表接口。" : diagnostic.message,
            hint: manualRequired ? "连接仍可使用；请在模型目录中手动登记模型 ID。" : diagnostic.hint ?? null,
            request_id: diagnostic.requestId ?? null,
          });
          if (manualRequired) return failure;
          throw diagnostic;
        }
      }
      return result;
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "模型列表拉取失败；旧缓存仍然保留。"));
      throw error;
    } finally {
      setBusy(false);
    }
  };

  const removeCredential = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      let lockError: unknown = null;
      try {
        await lockProvider(selected.id);
      } catch (error) {
        lockError = error;
      }
      await deleteCredential(selected.id);
      setApiKey("");
      setCredentialStored(false);
      setMessageTone(lockError ? "warning" : "success");
      setMessage(lockError
        ? `本地凭据已删除，但后端锁定失败：${errorMessage(lockError, "未知错误")}`
        : "后端内存凭据已锁定，本地加密凭据已删除。");
      await invalidateProviderData();
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "本地凭据删除失败。"));
    } finally {
      setBusy(false);
    }
  };

  const setArchived = async (provider: GenerationProviderProfile, archive: boolean) => {
    setBusy(true);
    try {
      await (archive ? archiveProvider(provider.id) : restoreProvider(provider.id));
      setHighlightedId(provider.id);
      setMessageTone("success");
      setMessage(archive ? "渠道已归档；关联的系统默认路由已清空。" : "渠道已恢复，可以重新用于新任务。");
      if (view === "edit") setView("list");
      await invalidateProviderData();
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, archive ? "渠道归档失败。" : "渠道恢复失败。"));
    } finally {
      setBusy(false);
    }
  };

  const providerRow = (provider: GenerationProviderProfile) => {
    const counts = enabledTypeCounts(provider);
    const syncState = provider.models_sync?.state ?? "never";
    return (
      <article className={`provider-list-row ${highlightedId === provider.id ? "highlighted" : ""}`} key={provider.id}>
        <div className="provider-list-identity">
          <span className={`provider-list-status ${providerReady(provider) ? "ready" : provider.is_active ? "waiting" : "archived"}`} />
          <span><strong>{provider.name}</strong><code>{provider.base_url}</code></span>
        </div>
        <div className="provider-list-connection">
          <strong>{statusCopy(provider)}</strong>
          <small>{SYNC_LABELS[syncState]} · {formatSyncTime(provider.models_refreshed_at)}</small>
        </div>
        <div className="provider-list-model-counts" aria-label="已启用模型数量">
          {MODEL_TYPES.map((type) => <span className={type} key={type}><b>{counts[type]}</b>{modelCapabilityLabel(type)}</span>)}
        </div>
        <div className="provider-list-actions">
          {provider.is_active ? <button className="button ghost compact-button" type="button" onClick={() => openModels(provider)} disabled={busy}><SlidersHorizontal size={14} /> 管理模型</button> : null}
          <button className="button secondary compact-button" type="button" onClick={() => beginEdit(provider)} disabled={busy}><PencilSimple size={14} /> 编辑</button>
          <button className="icon-button" type="button" onClick={() => void setArchived(provider, provider.is_active)} disabled={busy} aria-label={provider.is_active ? `归档 ${provider.name}` : `恢复 ${provider.name}`} title={provider.is_active ? "归档" : "恢复"}>{provider.is_active ? <Archive size={15} /> : <ArrowCounterClockwise size={15} />}</button>
        </div>
      </article>
    );
  };

  const syncState = String(currentSync.state ?? "never");
  const isArchived = selected?.is_active === false;

  if (!open) return null;

  return (
    <div className="drawer-backdrop provider-channel-backdrop" onMouseDown={onClose}>
      <aside
        ref={dialogRef}
        className="provider-channels-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="provider-channels-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="provider-channel-header">
          {view === "list" ? (
            <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭供应商渠道"><X size={19} /></button>
          ) : (
            <button ref={closeRef} className="icon-button" type="button" onClick={returnToList} aria-label="返回供应商列表"><ArrowLeft size={19} /></button>
          )}
          <div className="provider-channel-title">
            <span className="section-kicker">本机连接</span>
            <h2 id="provider-channels-title">{view === "list" ? "供应商渠道" : isNew ? "新建供应商" : "编辑供应商"}</h2>
          </div>
          <div className="drawer-spacer" />
          {view === "list" ? (
            <button className="button primary" type="button" onClick={beginNewProvider}><Plus size={15} /> 新建供应商</button>
          ) : (
            <>
              <button className="button secondary" type="button" onClick={returnToList} disabled={busy}>取消</button>
              <button className="button primary" type="button" onClick={() => void persistConnection()} disabled={busy || isArchived}><LockKeyOpen size={15} /> {busy ? "保存中…" : "保存"}</button>
            </>
          )}
        </header>

        <div className="provider-channel-scroll">
          {view === "list" ? (
            <div className="provider-list-page">
              <section className="provider-list-section" aria-label="活动供应商">
                <div className="provider-list-heading"><div><strong>活动供应商</strong><small>{activeProviders.length} 个渠道</small></div></div>
                {providersQuery.isLoading ? <div className="provider-list-empty"><strong>正在读取供应商…</strong></div> : activeProviders.length > 0 ? <div className="provider-list-rows">{activeProviders.map(providerRow)}</div> : <div className="provider-list-empty"><strong>还没有供应商</strong><p>新建供应商后，再按需拉取或手动登记模型。</p><button className="button primary" type="button" onClick={beginNewProvider}><Plus size={15} /> 新建供应商</button></div>}
              </section>
              {archivedProviders.length > 0 ? (
                <details className="provider-archived-section" open={archivedOpen} onToggle={(event) => setArchivedOpen(event.currentTarget.open)}>
                  <summary><span>已归档供应商</span><small>{archivedProviders.length} 个</small></summary>
                  <div className="provider-list-rows">{archivedProviders.map(providerRow)}</div>
                </details>
              ) : null}
              {message ? <div className={`channel-message ${messageTone}`} role={messageTone === "error" ? "alert" : "status"}>{messageTone === "success" ? <CheckCircle size={16} /> : <WarningCircle size={16} />}{message}</div> : null}
            </div>
          ) : (
            <div className="provider-editor-page">
              <section className="channel-form-card" aria-label="渠道连接">
                <div className="channel-card-heading">
                  <div><strong>{isNew ? "新建渠道" : draft.name}</strong><small>{statusCopy(selected, credentialStored)}</small></div>
                  {selected ? <button className="button ghost compact-button" type="button" onClick={() => void setArchived(selected, selected.is_active)} disabled={busy}>{selected.is_active ? <><Archive size={14} /> 归档</> : <><ArrowCounterClockwise size={14} /> 恢复</>}</button> : null}
                </div>
                <div className="channel-form-grid">
                  <label><span>渠道名称</span><input value={draft.name} onChange={(event) => update("name", event.target.value)} aria-label="渠道名称" /></label>
                  <label><span>协议</span><SelectMenu ariaLabel="协议" value="openai-compatible" options={[{ value: "openai-compatible", label: "OpenAI-compatible" }]} onChange={() => undefined} disabled /></label>
                  <label className="full-width"><span>接口地址</span><input value={draft.baseUrl} onChange={(event) => update("baseUrl", event.target.value)} aria-label="接口地址" aria-invalid={!isProviderUrlAllowed(draft.baseUrl, draft.allowPrivateNetwork)} spellCheck={false} />{!isProviderUrlAllowed(draft.baseUrl, draft.allowPrivateNetwork) ? <small className="channel-field-error">地址不符合本机安全策略。</small> : null}</label>
                  <label className="full-width"><span>API Key</span><div className="secret-input"><Key size={15} /><input type={showKey ? "text" : "password"} value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={credentialStored ? "留空保留当前凭据" : draft.credentialMode === "none" ? "该渠道不需要 API Key" : "仅在本机加密保存"} autoComplete="off" spellCheck={false} aria-label="API Key" /><button type="button" onClick={() => setShowKey((current) => !current)} aria-label={showKey ? "隐藏 API Key" : "显示 API Key"}>{showKey ? <EyeSlash size={17} /> : <Eye size={17} />}</button></div></label>
                </div>
                <div className={`channel-connection-status ${providerReady(selected) ? "ready" : isArchived ? "archived" : "waiting"}`} role={syncState === "error" ? "alert" : "status"}>
                  <span className="status-dot" />
                  <div><strong>{statusCopy(selected, credentialStored)}</strong><small>{credentialStored ? "API Key 已写入浏览器加密存储" : "保存后才会解锁本机后端"}</small></div>
                  {selected && credentialStored ? <button className="text-button danger-text" type="button" onClick={() => void removeCredential()} disabled={busy}>删除凭据</button> : null}
                </div>
              </section>

              <section className="channel-models-card" aria-label="渠道模型">
                <div className="channel-card-heading channel-model-heading">
                  <div><strong>模型摘要</strong><small>{enabledModels.length} 个已启用 · 四类模型可自由调整</small></div>
                  <button className="button secondary" type="button" onClick={() => selected ? setPickerOpen(true) : setMessage("请先保存渠道，再管理模型。")} disabled={busy || isArchived}><SlidersHorizontal size={15} /> 管理模型</button>
                </div>
                <div className="channel-model-summary">
                  {MODEL_TYPES.map((type) => {
                    const count = selected ? enabledTypeCounts(selected)[type] : 0;
                    return <span className={`model-summary-count ${type}`} key={type}><b>{count}</b><small>{modelCapabilityLabel(type)}</small></span>;
                  })}
                </div>
                {selected ? <div className={`channel-sync-summary ${syncState}`}>
                  <div><strong>{SYNC_LABELS[syncState] ?? "模型状态"}</strong><small>{formatSyncTime(selected.models_refreshed_at)}</small></div>
                  {currentSync.status_code ? <span>HTTP {currentSync.status_code}</span> : null}
                  {currentSync.endpoint ? <code>{currentSync.endpoint}</code> : null}
                  {currentSync.message ? <p>{currentSync.message}</p> : null}
                  {currentSync.hint ? <small>修复提示：{currentSync.hint}</small> : null}
                </div> : <div className="channel-empty-models"><strong>保存后管理模型</strong><small>模型列表不会在保存时自动拉取。</small></div>}
              </section>

              <details className="channel-advanced" open={advancedOpen} onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}>
                <summary><span><SlidersHorizontal size={15} /> 高级设置</span><small>凭据、发现方式、模型路径和私有网络</small></summary>
                <div className="channel-advanced-grid">
                  <label><span>凭据模式</span><SelectMenu ariaLabel="凭据模式" value={draft.credentialMode} options={[{ value: "required", label: "需要 API Key" }, { value: "optional", label: "API Key 可选" }, { value: "none", label: "无 API Key" }]} onChange={(value) => update("credentialMode", value as ProviderProfile["credentialMode"])} /></label>
                  <label><span>模型发现</span><SelectMenu ariaLabel="模型发现" value={draft.modelDiscoveryMode} options={[{ value: "auto", label: "自动拉取" }, { value: "manual", label: "手动登记" }]} onChange={(value) => update("modelDiscoveryMode", value as ProviderProfile["modelDiscoveryMode"])} /></label>
                  <label><span>模型列表路径</span><input value={draft.modelsPath} onChange={(event) => update("modelsPath", event.target.value)} placeholder="models" spellCheck={false} /></label>
                </div>
                <label className="channel-checkbox-line"><input type="checkbox" checked={draft.allowPrivateNetwork} onChange={(event) => update("allowPrivateNetwork", event.target.checked)} /><span>允许访问局域网或私有地址<small>浏览器会直接请求模型供应商；只在信任目标服务时开启。</small></span></label>
              </details>

              {message ? <div className={`channel-message ${messageTone}`} role={messageTone === "error" ? "alert" : "status"}>{messageTone === "success" ? <CheckCircle size={16} /> : <WarningCircle size={16} />}{message}</div> : null}
            </div>
          )}
        </div>

        <ModelPickerDialog
          open={pickerOpen}
          providerId={selected?.id ?? ""}
          models={selected?.models ?? []}
          newModelIds={newModelIds}
          catalogApiVersion={selected?.model_catalog_api_version ?? 1}
          defaultRoutes={defaultRoutes}
          onClose={() => setPickerOpen(false)}
          onRefresh={async () => {
            const result = await refreshModels();
            if (!result) throw new Error("请先保存渠道，再拉取模型列表。");
            return result;
          }}
          onCommitted={(result) => {
            applyModelsResponse(result);
            const cleared = result.cleared_default_routes ?? [];
            setMessageTone(cleared.length > 0 ? "warning" : "success");
            setMessage(cleared.length > 0
              ? `模型配置已保存，并清空${cleared.map(modelCapabilityLabel).join("、")}默认路由。`
              : "模型配置已保存。");
            void invalidateProviderData();
          }}
        />
      </aside>
    </div>
  );
}
