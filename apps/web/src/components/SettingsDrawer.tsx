import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArrowCounterClockwise,
  CheckCircle,
  Database,
  Eye,
  EyeSlash,
  FolderOpen,
  Key,
  LockKeyOpen,
  Plus,
  Robot,
  ShieldWarning,
  Sparkle,
  X,
} from "@phosphor-icons/react";
import {
  archiveProvider,
  createProviderProfile,
  ensureProviderProfile,
  fetchGenerationProviders,
  fetchProviderDefaults,
  fetchSystemInfo,
  lockProvider,
  refreshProviderModels,
  restoreProvider,
  testProvider,
  unlockProvider,
  updateProviderDefaults,
  updateProviderModelOverrides,
  updateProviderProfile,
} from "../lib/api";
import type { SystemInfo } from "../lib/api";
import {
  deleteCredential,
  hasCredential,
  isProviderUrlAllowed,
  normalizeProviderUrl,
  readCredential,
  saveCredential,
} from "../lib/credentials";
import { useModalFocus } from "../hooks/useModalFocus";
import type {
  GenerationProviderProfile,
  ProviderDefaults,
  ProviderModelModality,
  ProviderProfile,
} from "../types";

const EMPTY_PROFILE: ProviderProfile = {
  id: "",
  name: "新供应商",
  baseUrl: "https://api.openai.com/v1",
  textModel: "gpt-5-mini",
  imageModel: "gpt-image-2",
  quality: "high",
  concurrency: 6,
  retries: 2,
  allowPrivateNetwork: false,
};

const EMPTY_DEFAULTS: ProviderDefaults = { text: null, image: null, updated_at: null };
const NEW_PROVIDER_ID = "__new_provider__";

function toDraft(profile: GenerationProviderProfile): ProviderProfile {
  return {
    id: profile.id,
    name: profile.name,
    baseUrl: profile.base_url,
    textModel: profile.text_model,
    imageModel: profile.image_model,
    quality: profile.quality,
    concurrency: profile.concurrency,
    retries: profile.max_retries,
    allowPrivateNetwork: profile.allow_private_network,
  };
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function modelCacheIsStale(refreshedAt: string | null): boolean {
  if (!refreshedAt) return true;
  const refreshed = Date.parse(refreshedAt);
  return !Number.isFinite(refreshed) || Date.now() - refreshed > 24 * 60 * 60 * 1000;
}

function formatRefreshTime(value: string | null): string {
  if (!value) return "尚未拉取";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN", { hour12: false });
}

let legacyMigration: Promise<void> | null = null;

async function migrateLegacyProvider(): Promise<void> {
  const stored = localStorage.getItem("game-assets.provider-profile");
  if (!stored) return;
  const legacy = { ...EMPTY_PROFILE, ...(JSON.parse(stored) as Partial<ProviderProfile>) };
  const remote = await ensureProviderProfile(legacy);
  if (legacy.id && legacy.id !== remote.id) {
    const credential = await readCredential(legacy.id);
    if (credential) {
      await saveCredential(remote.id, credential);
      await deleteCredential(legacy.id);
    }
  }
  localStorage.removeItem("game-assets.provider-profile");
}

interface SettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

export function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<ProviderProfile>(EMPTY_PROFILE);
  const [defaultsDraft, setDefaultsDraft] = useState<ProviderDefaults>(EMPTY_DEFAULTS);
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [credentialStored, setCredentialStored] = useState(false);
  const [modelSearch, setModelSearch] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [manualModalities, setManualModalities] = useState<ProviderModelModality[]>(["text"]);
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
  const systemQuery = useQuery<SystemInfo>({
    queryKey: ["system-info"],
    queryFn: fetchSystemInfo,
    enabled: open,
  });

  const providers = providersQuery.data ?? [];
  const activeProviders = providers.filter((provider) => provider.is_active);
  const archivedProviders = providers.filter((provider) => !provider.is_active);
  const selected = providers.find((provider) => provider.id === selectedId);
  const filteredModels = useMemo(() => {
    const needle = modelSearch.trim().toLocaleLowerCase();
    return (selected?.models ?? []).filter((model) => !needle || model.id.toLocaleLowerCase().includes(needle));
  }, [modelSearch, selected?.models]);

  useModalFocus({ open, dialogRef, initialFocusRef: closeButtonRef, onClose });

  useEffect(() => {
    if (!open) return;
    if (!legacyMigration) legacyMigration = migrateLegacyProvider();
    void legacyMigration
      .then(() => queryClient.invalidateQueries({ queryKey: ["generation-providers"] }))
      .catch(() => undefined);
  }, [open, queryClient]);

  useEffect(() => {
    if (!open || providers.length === 0) return;
    if (selectedId === NEW_PROVIDER_ID) return;
    if (selectedId && providers.some((provider) => provider.id === selectedId)) return;
    setSelectedId((activeProviders[0] ?? providers[0]).id);
  }, [activeProviders, open, providers, selectedId]);

  useEffect(() => {
    if (!selected) return;
    setDraft(toDraft(selected));
    setApiKey("");
    setModelSearch("");
    setManualModel("");
    hasCredential(selected.id).then(setCredentialStored).catch(() => setCredentialStored(false));
  }, [selected]);

  useEffect(() => {
    if (defaultsQuery.data) setDefaultsDraft(defaultsQuery.data);
  }, [defaultsQuery.data]);

  const urlAllowed = useMemo(
    () => isProviderUrlAllowed(draft.baseUrl, draft.allowPrivateNetwork),
    [draft.allowPrivateNetwork, draft.baseUrl],
  );

  const update = <K extends keyof ProviderProfile>(key: K, value: ProviderProfile[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setMessage("");
  };

  const invalidateProviderData = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["generation-providers"] }),
      queryClient.invalidateQueries({ queryKey: ["provider-defaults"] }),
    ]);
  };

  const saveAndUnlock = async () => {
    const baseUrl = normalizeProviderUrl(draft.baseUrl, draft.allowPrivateNetwork);
    if (!baseUrl) {
      setMessageTone("error");
      setMessage("Base URL 必须使用 HTTPS（仅 localhost/loopback 可用 HTTP），且不能包含凭据、查询参数或片段。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const normalized = { ...draft, baseUrl };
      const saved = draft.id
        ? await updateProviderProfile(draft.id, normalized)
        : await createProviderProfile(normalized);
      let unlockedKey = apiKey.trim() || await readCredential(saved.id);
      if (apiKey.trim()) {
        await saveCredential(saved.id, apiKey.trim());
        setCredentialStored(true);
        setApiKey("");
      }
      if (saved.kind !== "fake" && unlockedKey) await unlockProvider(saved.id, unlockedKey);
      if (saved.kind === "fake" || unlockedKey) {
        try {
          await refreshProviderModels(saved.id);
          setMessageTone("success");
          setMessage("供应商已保存、解锁并刷新模型目录。");
        } catch (error) {
          setMessageTone("warning");
          setMessage(`供应商已保存，但模型刷新失败并保留旧缓存：${errorMessage(error, "未知错误")}`);
        }
      } else {
        setMessageTone("warning");
        setMessage("供应商已保存。添加 API Key 后才能解锁并拉取模型。");
      }
      setSelectedId(saved.id);
      setDraft(toDraft(saved));
      await invalidateProviderData();
      unlockedKey = "";
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "供应商保存失败。"));
    } finally {
      setBusy(false);
    }
  };

  const testConnection = async () => {
    if (!selected) return;
    setBusy(true);
    setMessage("");
    try {
      const key = apiKey.trim() || await readCredential(selected.id);
      if (selected.kind !== "fake" && !key) throw new Error("请先保存 API Key。");
      if (key) await unlockProvider(selected.id, key);
      const result = await testProvider(selected.id);
      setMessageTone("success");
      setMessage(result.models?.length ? `连接成功，已同步 ${result.models.length} 个模型。` : "连接成功，供应商未返回模型。 ");
      await invalidateProviderData();
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "连接与模型探测失败。"));
    } finally {
      setBusy(false);
    }
  };

  const refreshModels = async () => {
    if (!selected) return;
    setBusy(true);
    setMessage("");
    try {
      const key = await readCredential(selected.id);
      if (selected.kind !== "fake" && key) await unlockProvider(selected.id, key);
      const result = await refreshProviderModels(selected.id);
      setMessageTone("success");
      setMessage(`模型目录已刷新：${result.models.filter((model) => model.available).length} 个当前可用。`);
      await invalidateProviderData();
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "模型目录刷新失败；旧缓存未被覆盖。"));
    } finally {
      setBusy(false);
    }
  };

  const updateModelClassification = async (modelId: string, modalities: ProviderModelModality[]) => {
    if (!selected) return;
    setBusy(true);
    try {
      await updateProviderModelOverrides(selected.id, [{ id: modelId, modalities }]);
      await invalidateProviderData();
      setMessageTone("success");
      setMessage(`已保存 ${modelId} 的任务类型。`);
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "模型分类保存失败。"));
    } finally {
      setBusy(false);
    }
  };

  const toggleModelModality = (modelId: string, modality: ProviderModelModality) => {
    const model = selected?.models.find((item) => item.id === modelId);
    if (!model) return;
    const modalities = model.modalities.includes(modality)
      ? model.modalities.filter((value) => value !== modality)
      : [...model.modalities, modality];
    void updateModelClassification(modelId, modalities);
  };

  const addManualModel = () => {
    const modelId = manualModel.trim();
    if (!modelId || manualModalities.length === 0) return;
    void updateModelClassification(modelId, manualModalities);
    setManualModel("");
  };

  const saveDefaults = async () => {
    setBusy(true);
    setMessage("");
    try {
      const saved = await updateProviderDefaults(defaultsDraft);
      setDefaultsDraft(saved);
      setMessageTone("success");
      setMessage("全局文字与图片默认路由已保存；现有任务不会被改写。");
      await invalidateProviderData();
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "默认路由保存失败。"));
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

  const setArchived = async (archive: boolean) => {
    if (!selected) return;
    setBusy(true);
    try {
      const result = archive ? await archiveProvider(selected.id) : await restoreProvider(selected.id);
      setMessageTone("success");
      setMessage(archive ? "供应商已归档；历史任务和快照仍保留。" : "供应商已恢复，可重新分配给任务。 ");
      setSelectedId(result.id);
      await invalidateProviderData();
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, archive ? "供应商归档失败。" : "供应商恢复失败。"));
    } finally {
      setBusy(false);
    }
  };

  const beginNewProvider = () => {
    setSelectedId(NEW_PROVIDER_ID);
    setDraft({ ...EMPTY_PROFILE });
    setApiKey("");
    setCredentialStored(false);
    setMessage("");
  };

  const routeEditor = (modality: ProviderModelModality) => {
    const route = defaultsDraft[modality];
    const provider = activeProviders.find((item) => item.id === route?.provider_profile_id);
    const fallbackModel = modality === "text" ? provider?.text_model : provider?.image_model;
    const modelOptions = (provider?.models ?? []).filter((model) => model.modalities.length === 0 || model.modalities.includes(modality));
    return (
      <div className={`default-route-card ${modality}`}>
        <div><span>{modality === "text" ? "T" : "I"}</span><strong>{modality === "text" ? "文字默认" : "图片默认"}</strong></div>
        <label><span>供应商</span><select value={route?.provider_profile_id ?? ""} onChange={(event) => {
          const next = activeProviders.find((item) => item.id === event.target.value);
          setDefaultsDraft((current) => ({
            ...current,
            [modality]: next ? {
              provider_profile_id: next.id,
              model: modality === "text" ? next.text_model : next.image_model,
            } : null,
          }));
        }}><option value="">未设置</option>{activeProviders.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label><span>模型</span><input list={`default-${modality}-models`} value={route?.model ?? fallbackModel ?? ""} disabled={!provider} onChange={(event) => setDefaultsDraft((current) => ({
          ...current,
          [modality]: provider ? { provider_profile_id: provider.id, model: event.target.value } : null,
        }))} /><datalist id={`default-${modality}-models`}>{modelOptions.map((model) => <option key={model.id} value={model.id} />)}</datalist></label>
      </div>
    );
  };

  if (!open) return null;

  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside ref={dialogRef} className="settings-drawer provider-rack-drawer" role="dialog" aria-modal="true" aria-labelledby="provider-settings-title" tabIndex={-1} onMouseDown={(event) => event.stopPropagation()}>
        <header className="drawer-header provider-rack-header">
          <div><span className="section-kicker">多供应商路由 · M2.1</span><h2 id="provider-settings-title">供应商与模型机架</h2></div>
          <button ref={closeButtonRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭供应商设置"><X size={19} /></button>
        </header>

        <div className="provider-rack-layout">
          <nav className="provider-list-rail" aria-label="供应商列表">
            <button className="button secondary provider-add-button" type="button" onClick={beginNewProvider}><Plus size={16} /> 新增供应商</button>
            <div className="provider-list-heading"><span>活动供应商</span><strong>{activeProviders.length}</strong></div>
            {activeProviders.map((provider) => (
              <button key={provider.id} type="button" className={`provider-list-item ${selectedId === provider.id ? "active" : ""}`} onClick={() => setSelectedId(provider.id)}>
                <span className={provider.is_unlocked ? "provider-pulse online" : "provider-pulse"} />
                <div><strong>{provider.name}</strong><small>{provider.base_url}</small><em>{provider.text_model} / {provider.image_model} · {provider.is_unlocked ? "已解锁" : "凭据锁定"}</em></div>
                <span className="provider-default-badges">{defaultsDraft.text?.provider_profile_id === provider.id ? <b>T</b> : null}{defaultsDraft.image?.provider_profile_id === provider.id ? <b>I</b> : null}</span>
              </button>
            ))}
            {archivedProviders.length > 0 ? <><div className="provider-list-heading archived"><span>已归档</span><strong>{archivedProviders.length}</strong></div>{archivedProviders.map((provider) => (
              <button key={provider.id} type="button" className={`provider-list-item archived ${selectedId === provider.id ? "active" : ""}`} onClick={() => setSelectedId(provider.id)}><Archive size={15} /><div><strong>{provider.name}</strong><small>仅保留历史路由</small></div></button>
            ))}</> : null}
          </nav>

          <div className="provider-rack-content">
            <section className="provider-route-board">
              <div className="provider-route-board-heading"><div><Sparkle size={18} /><span><strong>新任务默认路由</strong><small>任务建立后即冻结，不随默认值变化</small></span></div><button className="button secondary" type="button" onClick={saveDefaults} disabled={busy}>保存默认</button></div>
              <div className="default-route-grid">{routeEditor("text")}{routeEditor("image")}</div>
            </section>

            <div className="provider-detail-grid">
              <section className="provider-connection-panel">
                <div className="panel-heading"><div><Robot size={19} /><span><strong>{draft.id ? draft.name : "新供应商"}</strong><small>{selected?.is_active === false ? "已归档，不可分配给新任务" : credentialStored ? "本地凭据已保存" : "等待 API Key"}</small></span></div>{selected ? <button className="button ghost" type="button" onClick={() => setArchived(selected.is_active)} disabled={busy}>{selected.is_active ? <><Archive size={15} /> 归档</> : <><ArrowCounterClockwise size={15} /> 恢复</>}</button> : null}</div>

                <div className="settings-form provider-profile-form">
                  <label><span>配置档名称</span><input value={draft.name} onChange={(event) => update("name", event.target.value)} /></label>
                  <label><span>Base URL</span><input value={draft.baseUrl} onChange={(event) => update("baseUrl", event.target.value)} aria-invalid={!urlAllowed} spellCheck={false} />{!urlAllowed ? <small className="field-error">当前 URL 不符合安全策略。</small> : null}</label>
                  <div className="form-grid">
                    <label><span>默认文字模型</span><input list="provider-text-models" value={draft.textModel} onChange={(event) => update("textModel", event.target.value)} /><datalist id="provider-text-models">{selected?.models.filter((model) => model.modalities.length === 0 || model.modalities.includes("text")).map((model) => <option key={model.id} value={model.id} />)}</datalist></label>
                    <label><span>默认图片模型</span><input list="provider-image-models" value={draft.imageModel} onChange={(event) => update("imageModel", event.target.value)} /><datalist id="provider-image-models">{selected?.models.filter((model) => model.modalities.length === 0 || model.modalities.includes("image")).map((model) => <option key={model.id} value={model.id} />)}</datalist></label>
                    <label><span>图片质量</span><select value={draft.quality} onChange={(event) => update("quality", event.target.value as ProviderProfile["quality"])}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select></label>
                    <label><span>并发任务</span><input type="number" min={1} max={32} value={draft.concurrency} onChange={(event) => update("concurrency", Number(event.target.value))} /></label>
                    <label><span>失败重试</span><input type="number" min={0} max={8} value={draft.retries} onChange={(event) => update("retries", Number(event.target.value))} /></label>
                  </div>
                  <label><span>{credentialStored ? "替换 API Key" : "API Key"}</span><div className="secret-input"><input type={showKey ? "text" : "password"} value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={credentialStored ? "留空保留现有凭据" : "仅在本机加密保存"} autoComplete="off" spellCheck={false} /><button type="button" onClick={() => setShowKey((value) => !value)} aria-label={showKey ? "隐藏 API Key" : "显示 API Key"}>{showKey ? <EyeSlash size={18} /> : <Eye size={18} />}</button></div></label>
                  <label className="checkbox-line"><input type="checkbox" checked={draft.allowPrivateNetwork} onChange={(event) => update("allowPrivateNetwork", event.target.checked)} /><span>允许访问局域网或私有地址<small>仅在信任目标服务时开启；localhost HTTP 始终允许。</small></span></label>
                  <div className="provider-form-actions"><button className="button primary" type="button" onClick={saveAndUnlock} disabled={busy || selected?.is_active === false}><LockKeyOpen size={16} /> {busy ? "正在保存…" : "保存并解锁"}</button><button className="button secondary" type="button" onClick={testConnection} disabled={busy || !selected}>连接测试</button>{selected && credentialStored ? <button className="button ghost danger" type="button" onClick={removeCredential} disabled={busy}>删除凭据</button> : null}</div>
                </div>
              </section>

              <section className="provider-model-panel">
                <div className="panel-heading"><div><Sparkle size={19} /><span><strong>模型目录</strong><small>{formatRefreshTime(selected?.models_refreshed_at ?? null)}{selected && modelCacheIsStale(selected.models_refreshed_at) ? " · 已过期" : ""}</small></span></div><button className="button secondary" type="button" onClick={refreshModels} disabled={busy || !selected || selected.is_active === false}>拉取模型</button></div>
                <input className="provider-model-search" value={modelSearch} onChange={(event) => setModelSearch(event.target.value)} placeholder="筛选模型 ID" aria-label="筛选模型" />
                <div className="provider-model-list" aria-label="模型分类目录">
                  {filteredModels.length === 0 ? <div className="provider-model-empty"><Sparkle size={24} /><strong>暂无模型缓存</strong><p>保存凭据后拉取，也可以在下方手动登记模型。</p></div> : filteredModels.slice(0, 120).map((model) => (
                    <div className={`provider-model-row ${model.available ? "" : "stale"}`} key={model.id}><span><strong>{model.id}</strong><small>{model.available ? "当前可用" : "未在最近刷新中出现"} · {model.classification}</small></span><div><button type="button" className={model.modalities.includes("text") ? "active" : ""} aria-pressed={model.modalities.includes("text")} onClick={() => toggleModelModality(model.id, "text")} disabled={busy}>文字</button><button type="button" className={model.modalities.includes("image") ? "active" : ""} aria-pressed={model.modalities.includes("image")} onClick={() => toggleModelModality(model.id, "image")} disabled={busy}>图片</button></div></div>
                  ))}
                </div>
                <div className="manual-model-entry"><input value={manualModel} onChange={(event) => setManualModel(event.target.value)} placeholder="手动输入模型 ID" /><label><input type="checkbox" checked={manualModalities.includes("text")} onChange={() => setManualModalities((current) => current.includes("text") ? current.filter((value) => value !== "text") : [...current, "text"])} />文字</label><label><input type="checkbox" checked={manualModalities.includes("image")} onChange={() => setManualModalities((current) => current.includes("image") ? current.filter((value) => value !== "image") : [...current, "image"])} />图片</label><button className="button secondary" type="button" onClick={addManualModel} disabled={busy || !selected || !manualModel.trim() || manualModalities.length === 0}>登记</button></div>
              </section>
            </div>

            <section className="provider-local-boundary">
              <div><Database size={19} /><span><strong>本机安全边界</strong><small>正式成果进入 Project；模型缓存和运行索引可重建。</small></span></div>
              <dl><div><dt><FolderOpen size={14} /> Projects</dt><dd>{systemQuery.data?.projects_root ?? "正在读取…"}</dd></div><div><dt><Database size={14} /> 运行索引</dt><dd>{systemQuery.data?.state_dir ?? "正在读取…"}</dd></div><div><dt><Key size={14} /> 供应商凭据</dt><dd>{systemQuery.data?.credential_store ?? "browser IndexedDB"}</dd></div></dl>
              <p><ShieldWarning size={16} /> API Key 只解锁到 127.0.0.1 后端内存，不写入 SQLite、Project 或运行事件；浏览器加密存储无法抵御同源脚本注入。</p>
            </section>
          </div>
        </div>

        <footer className="drawer-footer provider-rack-footer">{message ? <div className={`settings-message ${messageTone}`} role={messageTone === "error" ? "alert" : "status"}>{messageTone === "success" ? <CheckCircle size={16} /> : <ShieldWarning size={16} />}{message}</div> : <span>活动供应商 {activeProviders.length} · 模型目录按供应商独立缓存</span>}<div className="drawer-spacer" /><button className="button secondary" type="button" onClick={onClose}>完成</button></footer>
      </aside>
    </div>
  );
}
