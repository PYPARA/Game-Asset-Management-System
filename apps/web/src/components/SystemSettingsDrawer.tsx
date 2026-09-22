import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Database,
  FileLock,
  Image,
  ShieldCheck,
  SpeakerHigh,
  TextT,
  VideoCamera,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import {
  fetchGenerationProviders,
  fetchProviderDefaults,
  fetchSystemInfo,
  updateProviderDefaults,
} from "../lib/api";
import type { SystemInfo } from "../lib/api";
import { useModalFocus } from "../hooks/useModalFocus";
import type {
  GenerationProviderProfile,
  ProviderDefaults,
  ProviderModelModality,
} from "../types";
import { SelectMenu } from "./SelectMenu";

const EMPTY_DEFAULTS: ProviderDefaults = {
  text: null,
  image: null,
  video: null,
  audio: null,
  max_concurrency: 3,
  max_transport_retries: 2,
  updated_at: null,
};

const ROUTE_META = {
  text: { label: "文本默认路由", short: "文本", Icon: TextT },
  image: { label: "生图默认路由", short: "生图", Icon: Image },
  video: { label: "视频默认路由", short: "视频", Icon: VideoCamera },
  audio: { label: "音频默认路由", short: "音频", Icon: SpeakerHigh },
} satisfies Record<
  ProviderModelModality,
  { label: string; short: string; Icon: typeof TextT }
>;

const ROUTE_MODALITIES: ProviderModelModality[] = ["text", "image", "video", "audio"];

function routeModels(
  provider: GenerationProviderProfile | undefined,
  modality: ProviderModelModality,
) {
  return (provider?.models ?? []).filter(
    (model) => model.enabled !== false && model.modalities.length === 1 && model.modalities[0] === modality,
  );
}

function routeError(error: unknown, fallback: string): string {
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

interface SystemSettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

export function SystemSettingsDrawer({ open, onClose }: SystemSettingsDrawerProps) {
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [draft, setDraft] = useState<ProviderDefaults>(EMPTY_DEFAULTS);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"success" | "error">("success");

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

  const activeProviders = (providersQuery.data ?? []).filter((provider) => provider.is_active);

  useModalFocus({ open, dialogRef, initialFocusRef: closeRef, onClose });

  useEffect(() => {
    if (defaultsQuery.data) setDraft(defaultsQuery.data);
  }, [defaultsQuery.data]);

  const updateRoute = (
    modality: ProviderModelModality,
    providerId: string,
    model: string,
  ) => {
    setDraft((current) => ({
      ...current,
      [modality]: providerId ? { provider_profile_id: providerId, model } : null,
    }));
    setMessage("");
  };

  const save = async () => {
    if (!Number.isInteger(draft.max_concurrency) || draft.max_concurrency < 1 || draft.max_concurrency > 32) {
      setMessageTone("error");
      setMessage("默认计划并发必须是 1–32 的整数。");
      return;
    }
    if (!Number.isInteger(draft.max_transport_retries) || draft.max_transport_retries < 0 || draft.max_transport_retries > 8) {
      setMessageTone("error");
      setMessage("默认网络重试必须是 0–8 的整数。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const normalized: ProviderDefaults = { ...draft };
      for (const modality of ROUTE_MODALITIES) {
        const route = normalized[modality];
        const provider = activeProviders.find((item) => item.id === route?.provider_profile_id);
        if (!route || !provider || !routeModels(provider, modality).some((model) => model.id === route.model)) {
          normalized[modality] = null;
        }
      }
      const saved = await updateProviderDefaults(normalized);
      setDraft(saved);
      setMessageTone("success");
      setMessage("默认路由和新计划运行默认值已保存；已确认任务继续使用冻结快照。");
      await queryClient.invalidateQueries({ queryKey: ["provider-defaults"] });
    } catch (error) {
      setMessageTone("error");
      setMessage(routeError(error, "系统设置保存失败。"));
    } finally {
      setBusy(false);
    }
  };

  const routeEditor = (modality: ProviderModelModality) => {
    const route = draft[modality];
    const provider = activeProviders.find((item) => item.id === route?.provider_profile_id);
    const models = routeModels(provider, modality);
    const selectedModel = models.some((model) => model.id === route?.model) ? route?.model ?? "" : "";
    const { Icon, label } = ROUTE_META[modality];
    return (
      <section className={`system-route-card ${modality}`} aria-label={label} key={modality}>
        <div className="system-route-heading">
          <span className="system-route-icon"><Icon size={17} /></span>
          <div><strong>{label}</strong><small>只影响新建任务</small></div>
        </div>
        <label>
          <span>渠道</span>
          <SelectMenu
            ariaLabel="渠道"
            value={route?.provider_profile_id ?? ""}
            onChange={(value) => updateRoute(modality, value, "")}
            options={[
              { value: "", label: "未设置" },
              ...activeProviders.map((item) => ({ value: item.id, label: item.name })),
            ]}
          />
        </label>
        <label>
          <span>模型</span>
          <SelectMenu
            ariaLabel="模型"
            value={selectedModel}
            disabled={!provider || models.length === 0}
            onChange={(value) => updateRoute(modality, provider?.id ?? "", value)}
            options={[
              { value: "", label: provider ? models.length > 0 ? "选择已启用模型" : "暂无已启用模型" : "先选择渠道" },
              ...models.map((model) => ({
                value: model.id,
                label: `${model.id}${model.classification === "manual" ? " · 未验证" : ""}`,
              })),
            ]}
          />
        </label>
        {route?.model && !selectedModel ? (
          <p className="system-route-warning">
            <WarningCircle size={14} /> 当前模型已停用、删除或类型不匹配；保存时会清除此路由。
          </p>
        ) : null}
      </section>
    );
  };

  if (!open) return null;

  return (
    <div className="drawer-backdrop system-settings-backdrop" onMouseDown={onClose}>
      <aside
        ref={dialogRef}
        className="system-settings-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="system-settings-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="drawer-header system-settings-header">
          <div><span className="section-kicker">全局行为</span><h2 id="system-settings-title">系统设置</h2></div>
          <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭系统设置"><X size={19} /></button>
        </header>
        <div className="system-settings-scroll">
          <section className="system-routes-section">
            <div className="system-section-heading">
              <div><strong>四类默认路由</strong><small>新任务创建时使用；任务确认后渠道和模型会冻结。</small></div>
            </div>
            <div className="system-route-grid">{ROUTE_MODALITIES.map(routeEditor)}</div>
            {providersQuery.isError || defaultsQuery.isError ? (
              <p className="system-inline-error">供应商或默认路由读取失败，请检查本机后端连接。</p>
            ) : null}
          </section>

          <section className="system-runtime-section">
            <div className="system-section-heading">
              <div><strong>新计划运行默认值</strong><small>生成计划打开时继承；可在计划高级设置中单次覆盖。</small></div>
            </div>
            <div className="system-runtime-grid">
              <label><span>默认计划并发</span><input type="number" min={1} max={32} value={draft.max_concurrency} onChange={(event) => setDraft((current) => ({ ...current, max_concurrency: Number(event.target.value) }))} /><small>范围 1–32，默认 3</small></label>
              <label><span>默认网络重试</span><input type="number" min={0} max={8} value={draft.max_transport_retries} onChange={(event) => setDraft((current) => ({ ...current, max_transport_retries: Number(event.target.value) }))} /><small>范围 0–8，默认 2</small></label>
            </div>
          </section>

          <section className="system-boundary-section">
            <div className="system-section-heading"><div><strong><ShieldCheck size={17} /> 本机安全边界</strong><small>凭据和运行索引只服务于当前本机。</small></div></div>
            <p>API Key 只保存在浏览器加密存储，并在验证或运行时短暂解锁到本机后端进程内存；不会写入 SQLite、Project、Job 快照或错误日志。</p>
            <div className="system-info-grid">
              <div><FileLock size={15} /><span><small>凭据存储</small><strong>{systemQuery.data?.credential_store ?? "浏览器 IndexedDB"}</strong></span></div>
              <div><Database size={15} /><span><small>运行索引</small><strong>{systemQuery.data?.state_dir ?? "正在读取…"}</strong></span></div>
            </div>
            <small className="system-boundary-footnote">浏览器加密存储无法抵御同源脚本注入；请只在可信本机环境中使用。</small>
          </section>
          {message ? <div className={`system-settings-message ${messageTone}`} role={messageTone === "error" ? "alert" : "status"}>{messageTone === "error" ? <WarningCircle size={16} /> : <ShieldCheck size={16} />}{message}</div> : null}
        </div>
        <footer className="drawer-footer system-settings-footer"><span>默认值只影响新计划</span><div className="drawer-spacer" /><button className="button secondary" type="button" onClick={onClose} disabled={busy}>取消</button><button className="button primary" type="button" onClick={() => void save()} disabled={busy}>{busy ? "保存中…" : "保存设置"}</button></footer>
      </aside>
    </div>
  );
}
