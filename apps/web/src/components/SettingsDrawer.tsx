import { useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle,
  Eye,
  EyeSlash,
  Key,
  LockKeyOpen,
  ShieldWarning,
  Trash,
  X,
} from "@phosphor-icons/react";
import { ensureProviderProfile, lockProvider, testProvider, unlockProvider } from "../lib/api";
import {
  deleteCredential,
  hasCredential,
  isProviderUrlAllowed,
  normalizeProviderUrl,
  readCredential,
  saveCredential,
} from "../lib/credentials";
import type { ProviderProfile } from "../types";

const defaultProfile: ProviderProfile = {
  id: "local-openai",
  name: "本地生成配置",
  baseUrl: "https://api.openai.com/v1",
  textModel: "gpt-5-mini",
  imageModel: "gpt-image-2",
  quality: "high",
  concurrency: 6,
  retries: 2,
  allowPrivateNetwork: false,
};

function loadProfile(): ProviderProfile {
  try {
    const saved = localStorage.getItem("game-assets.provider-profile");
    if (!saved) return defaultProfile;
    const profile = { ...defaultProfile, ...(JSON.parse(saved) as Partial<ProviderProfile>) };
    const baseUrl = normalizeProviderUrl(profile.baseUrl, profile.allowPrivateNetwork);
    return baseUrl ? { ...profile, baseUrl } : profile;
  } catch {
    return defaultProfile;
  }
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

interface SettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

export function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const backdropRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const [profile, setProfile] = useState<ProviderProfile>(loadProfile);
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [credentialStored, setCredentialStored] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"success" | "warning" | "error">("success");

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    hasCredential(profile.id).then(setCredentialStored).catch(() => setCredentialStored(false));
  }, [open, profile.id]);

  useEffect(() => {
    if (!open) return;

    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const backdrop = backdropRef.current;
    const backgroundElements = backdrop?.parentElement
      ? Array.from(backdrop.parentElement.children).filter(
          (element): element is HTMLElement =>
            element instanceof HTMLElement && element !== backdrop,
        )
      : [];
    const backgroundState = backgroundElements.map((element) => ({
      element,
      inert: element.inert,
      ariaHidden: element.getAttribute("aria-hidden"),
    }));
    backgroundElements.forEach((element) => {
      element.inert = true;
      element.setAttribute("aria-hidden", "true");
    });
    closeButtonRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (element) => element.getAttribute("aria-hidden") !== "true",
      );
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      backgroundState.forEach(({ element, inert, ariaHidden }) => {
        element.inert = inert;
        if (ariaHidden === null) element.removeAttribute("aria-hidden");
        else element.setAttribute("aria-hidden", ariaHidden);
      });
      previousFocusRef.current?.focus();
      previousFocusRef.current = null;
    };
  }, [open]);

  const urlAllowed = useMemo(
    () => isProviderUrlAllowed(profile.baseUrl, profile.allowPrivateNetwork),
    [profile.allowPrivateNetwork, profile.baseUrl],
  );

  const update = <K extends keyof ProviderProfile>(key: K, value: ProviderProfile[K]) => {
    setProfile((current) => ({ ...current, [key]: value }));
    setMessage("");
  };

  const saveAndUnlock = async () => {
    const baseUrl = normalizeProviderUrl(profile.baseUrl, profile.allowPrivateNetwork);
    if (!baseUrl) {
      setMessageTone("error");
      setMessage("Base URL 必须使用 HTTPS（仅 localhost/loopback 可用 HTTP），且不能包含凭据、查询参数或片段。");
      return;
    }

    const normalizedProfile = { ...profile, baseUrl };
    setBusy(true);
    setMessage("");
    try {
      if (apiKey.trim()) {
        await saveCredential(profile.id, apiKey);
        setCredentialStored(true);
        setApiKey("");
      }
      setProfile(normalizedProfile);
      localStorage.setItem("game-assets.provider-profile", JSON.stringify(normalizedProfile));

      let credentialProfileId = normalizedProfile.id;
      let unlockedKey = await readCredential(credentialProfileId);
      try {
        const remoteProfile = await ensureProviderProfile(normalizedProfile);
        if (remoteProfile.id !== credentialProfileId) {
          if (unlockedKey) {
            await saveCredential(remoteProfile.id, unlockedKey);
            await deleteCredential(credentialProfileId);
          }
          credentialProfileId = remoteProfile.id;
          const nextProfile = { ...normalizedProfile, id: remoteProfile.id };
          setProfile(nextProfile);
          localStorage.setItem("game-assets.provider-profile", JSON.stringify(nextProfile));
        }
        if (!unlockedKey) {
          setMessageTone("warning");
          setMessage("配置已同步到本地后端。添加 API Key 后才能启动生成任务。");
          return;
        }
        await unlockProvider(credentialProfileId, unlockedKey);
        setMessageTone("success");
        setMessage("配置已保存，凭据已临时解锁到本地后端内存。");
      } catch (error) {
        setMessageTone("error");
        setMessage(errorMessage(error, "供应商配置同步或凭据解锁失败。"));
      }
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "保存配置失败"));
    } finally {
      setBusy(false);
    }
  };

  const testConnection = async () => {
    const baseUrl = normalizeProviderUrl(profile.baseUrl, profile.allowPrivateNetwork);
    if (!baseUrl) {
      setMessageTone("error");
      setMessage("请先修正 Base URL。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const remoteProfile = await ensureProviderProfile({ ...profile, baseUrl });
      const unlockedKey = await readCredential(profile.id);
      if (unlockedKey) await unlockProvider(remoteProfile.id, unlockedKey);
      const result = await testProvider(remoteProfile.id);
      setMessageTone("success");
      setMessage(result.models?.length ? `连接成功：${result.models.join("、")}` : "本地后端已完成连接与能力探测。");
    } catch (error) {
      setMessageTone("error");
      setMessage(errorMessage(error, "连接与能力探测失败。"));
    } finally {
      setBusy(false);
    }
  };

  const removeCredential = async () => {
    setBusy(true);
    setMessage("");
    let backendLockError: unknown;
    let localDeleteError: unknown;
    try {
      await lockProvider(profile.id);
    } catch (error) {
      backendLockError = error;
    }
    try {
      await deleteCredential(profile.id);
      setApiKey("");
      setCredentialStored(false);
    } catch (error) {
      localDeleteError = error;
    }

    if (!backendLockError && !localDeleteError) {
      setMessageTone("success");
      setMessage("后端内存凭据已锁定，此配置档的本地凭据已删除。");
    } else if (backendLockError && !localDeleteError) {
      setMessageTone("warning");
      setMessage(`本地凭据已删除，但后端锁定失败：${errorMessage(backendLockError, "未知错误")}`);
    } else if (!backendLockError && localDeleteError) {
      setMessageTone("error");
      setMessage(`后端内存凭据已锁定，但本地删除失败：${errorMessage(localDeleteError, "未知错误")}`);
    } else {
      setMessageTone("error");
      setMessage(
        `后端锁定失败：${errorMessage(backendLockError, "未知错误")}；本地删除失败：${errorMessage(localDeleteError, "未知错误")}`,
      );
    }
    setBusy(false);
  };

  if (!open) return null;

  return (
    <div ref={backdropRef} className="drawer-backdrop" onMouseDown={onClose}>
      <aside
        ref={dialogRef}
        className="settings-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="provider-settings-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="drawer-header">
          <div>
            <span className="section-kicker">生成供应商</span>
            <h2 id="provider-settings-title">连接与凭据</h2>
          </div>
          <button
            ref={closeButtonRef}
            className="icon-button"
            type="button"
            onClick={onClose}
            aria-label="关闭供应商设置"
          >
            <X size={19} />
          </button>
        </header>

        <div className="drawer-scroll">
          <section className="drawer-section">
            <div className="credential-status">
              <span className={credentialStored ? "credential-icon stored" : "credential-icon"}>
                {credentialStored ? <CheckCircle size={20} weight="fill" /> : <Key size={20} />}
              </span>
              <div>
                <strong>{credentialStored ? "已保存加密凭据" : "尚未保存 API Key"}</strong>
                <p>Key 只会解锁到 127.0.0.1 后端的内存中。</p>
              </div>
            </div>
          </section>

          <section className="drawer-section settings-form">
            <label>
              <span>配置档名称</span>
              <input value={profile.name} onChange={(event) => update("name", event.target.value)} />
            </label>

            <label>
              <span>Base URL</span>
              <input
                value={profile.baseUrl}
                onChange={(event) => update("baseUrl", event.target.value)}
                aria-invalid={!urlAllowed}
                spellCheck={false}
              />
              {!urlAllowed && <small className="field-error">当前 URL 不符合安全策略。</small>}
            </label>

            <div className="form-grid">
              <label>
                <span>文本模型</span>
                <input value={profile.textModel} onChange={(event) => update("textModel", event.target.value)} />
              </label>
              <label>
                <span>图片模型</span>
                <input value={profile.imageModel} onChange={(event) => update("imageModel", event.target.value)} />
              </label>
              <label>
                <span>图片质量</span>
                <select
                  value={profile.quality}
                  onChange={(event) => update("quality", event.target.value as ProviderProfile["quality"])}
                >
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                </select>
              </label>
              <label>
                <span>并发任务</span>
                <input
                  type="number"
                  min="1"
                  max="12"
                  value={profile.concurrency}
                  onChange={(event) => update("concurrency", Number(event.target.value))}
                />
              </label>
              <label>
                <span>失败重试</span>
                <input
                  type="number"
                  min="0"
                  max="5"
                  value={profile.retries}
                  onChange={(event) => update("retries", Number(event.target.value))}
                />
              </label>
            </div>

            <label>
              <span>{credentialStored ? "替换 API Key" : "API Key"}</span>
              <div className="secret-input">
                <input
                  type={showKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={credentialStored ? "留空以保留现有凭据" : "仅在本机加密保存"}
                  autoComplete="off"
                  spellCheck={false}
                />
                <button type="button" onClick={() => setShowKey((value) => !value)} aria-label={showKey ? "隐藏 API Key" : "显示 API Key"}>
                  {showKey ? <EyeSlash size={18} /> : <Eye size={18} />}
                </button>
              </div>
            </label>

            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={profile.allowPrivateNetwork}
                onChange={(event) => update("allowPrivateNetwork", event.target.checked)}
              />
              <span>
                允许访问局域网或私有地址
                <small>仅在你信任目标服务时开启。本机 localhost HTTP 始终允许。</small>
              </span>
            </label>
          </section>

          <section className="security-note">
            <ShieldWarning size={21} />
            <div>
              <strong>本地持久凭据的边界</strong>
              <p>
                Web Crypto 使用不可导出的 AES-GCM CryptoKey，并将密文存入 IndexedDB；它能避免明文落盘，但无法抵御同源脚本注入。此应用不加载第三方脚本。
              </p>
            </div>
          </section>

          {message && (
            <div className={`settings-message ${messageTone}`} role={messageTone === "error" ? "alert" : "status"}>
              {message}
            </div>
          )}
        </div>

        <footer className="drawer-footer">
          {credentialStored && (
            <button className="button danger-quiet" type="button" onClick={removeCredential} disabled={busy}>
              <Trash size={17} /> 删除凭据
            </button>
          )}
          <span className="drawer-spacer" />
          <button className="button secondary" type="button" onClick={testConnection} disabled={busy}>
            测试连接
          </button>
          <button className="button primary" type="button" onClick={saveAndUnlock} disabled={busy}>
            <LockKeyOpen size={17} /> {busy ? "处理中…" : "保存并解锁"}
          </button>
        </footer>
      </aside>
    </div>
  );
}
