import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsDrawer } from "../components/SettingsDrawer";
import { hasCredential, saveCredential } from "../lib/credentials";
import type { GenerationProviderProfile } from "../types";

const apiMocks = vi.hoisted(() => ({
  archiveProvider: vi.fn(),
  createProviderProfile: vi.fn(),
  ensureProviderProfile: vi.fn(),
  fetchGenerationProviders: vi.fn(),
  fetchProviderDefaults: vi.fn(),
  fetchSystemInfo: vi.fn(),
  lockProvider: vi.fn(),
  refreshProviderModels: vi.fn(),
  restoreProvider: vi.fn(),
  testProvider: vi.fn(),
  unlockProvider: vi.fn(),
  updateProviderDefaults: vi.fn(),
  updateProviderModelOverrides: vi.fn(),
  updateProviderProfile: vi.fn(),
}));

vi.mock("../lib/api", () => apiMocks);

const provider: GenerationProviderProfile = {
  id: "provider-alpha",
  name: "Alpha OpenAI",
  kind: "openai_compatible",
  base_url: "https://api.example.com/v1",
  text_model: "alpha-text",
  image_model: "alpha-image",
  quality: "high",
  concurrency: 3,
  max_retries: 2,
  allow_private_network: false,
  pricing: { text_call: 0.2, image_call: 0.8 },
  is_active: true,
  models: [
    { id: "alpha-text", modalities: ["text"], classification: "provider", available: true },
    { id: "alpha-image", modalities: ["image"], classification: "heuristic", available: true },
    { id: "alpha-old", modalities: [], classification: "unknown", available: false },
  ],
  models_refreshed_at: "2026-07-30T08:00:00Z",
  is_unlocked: false,
};

function queryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderDrawer(node: React.ReactNode) {
  return render(<QueryClientProvider client={queryClient()}>{node}</QueryClientProvider>);
}

function DrawerHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>供应商设置</button>
      <SettingsDrawer open={open} onClose={() => setOpen(false)} />
    </>
  );
}

describe("多供应商设置抽屉", () => {
  beforeEach(() => {
    localStorage.clear();
    for (const mock of Object.values(apiMocks)) mock.mockReset();
    apiMocks.fetchGenerationProviders.mockResolvedValue([provider]);
    apiMocks.fetchProviderDefaults.mockResolvedValue({
      text: { provider_profile_id: provider.id, model: provider.text_model },
      image: { provider_profile_id: provider.id, model: provider.image_model },
      updated_at: "2026-07-30T08:00:00Z",
    });
    apiMocks.fetchSystemInfo.mockResolvedValue({
      projects_root: "/projects",
      state_dir: "/projects/local-state",
      project_workspace: "<project>/workspace",
      credential_store: "browser IndexedDB",
    });
    apiMocks.ensureProviderProfile.mockImplementation(async (draft) => ({ id: draft.id, ...draft }));
    apiMocks.createProviderProfile.mockResolvedValue({ ...provider, id: "provider-new", name: "新供应商" });
    apiMocks.updateProviderProfile.mockResolvedValue(provider);
    apiMocks.archiveProvider.mockResolvedValue({ ...provider, is_active: false });
    apiMocks.restoreProvider.mockResolvedValue(provider);
    apiMocks.lockProvider.mockResolvedValue(undefined);
    apiMocks.refreshProviderModels.mockResolvedValue({
      provider_profile_id: provider.id,
      models: provider.models,
      refreshed_at: provider.models_refreshed_at,
    });
    apiMocks.testProvider.mockResolvedValue({ models: ["alpha-text", "alpha-image"] });
    apiMocks.unlockProvider.mockResolvedValue(undefined);
    apiMocks.updateProviderDefaults.mockImplementation(async (value) => value);
    apiMocks.updateProviderModelOverrides.mockResolvedValue({
      provider_profile_id: provider.id,
      models: provider.models,
      refreshed_at: provider.models_refreshed_at,
    });
  });

  it("圈定焦点、支持 Escape，并在关闭后恢复触发器焦点", async () => {
    const user = userEvent.setup();
    renderDrawer(<DrawerHarness />);
    const trigger = screen.getByRole("button", { name: "供应商设置" });
    await user.click(trigger);

    const close = screen.getByRole("button", { name: "关闭供应商设置" });
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "完成" })).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("新增态不会被首个供应商覆盖，并在保存前规范化 Base URL", async () => {
    const user = userEvent.setup();
    renderDrawer(<SettingsDrawer open onClose={() => undefined} />);
    await screen.findByRole("button", { name: /Alpha OpenAI/ });
    await user.click(screen.getByRole("button", { name: "新增供应商" }));

    const name = screen.getByLabelText("配置档名称");
    expect(name).toHaveValue("新供应商");
    const baseUrl = screen.getByLabelText("Base URL");
    await user.clear(baseUrl);
    await user.type(baseUrl, "HTTPS://API.Example.COM:443/v1///");
    await user.click(screen.getByRole("button", { name: "保存并解锁" }));

    await waitFor(() => expect(apiMocks.createProviderProfile).toHaveBeenCalled());
    expect(apiMocks.createProviderProfile.mock.calls[0][0].baseUrl).toBe("https://api.example.com/v1");
    expect(apiMocks.updateProviderProfile).not.toHaveBeenCalled();
  });

  it("保存文字与图片默认路由，并允许手动修正模型分类", async () => {
    const user = userEvent.setup();
    renderDrawer(<SettingsDrawer open onClose={() => undefined} />);
    await screen.findByRole("button", { name: /Alpha OpenAI/ });

    const modelInputs = screen.getAllByLabelText("模型");
    await user.clear(modelInputs[0]);
    await user.type(modelInputs[0], "alpha-text-v2");
    await user.click(screen.getByRole("button", { name: "保存默认" }));
    expect(apiMocks.updateProviderDefaults).toHaveBeenCalledWith(expect.objectContaining({
      text: { provider_profile_id: provider.id, model: "alpha-text-v2" },
    }));

    const alphaTextRow = screen.getByText("alpha-text").closest(".provider-model-row");
    expect(alphaTextRow).not.toBeNull();
    await user.click(within(alphaTextRow as HTMLElement).getByRole("button", { name: "图片", pressed: false }));
    await waitFor(() => expect(apiMocks.updateProviderModelOverrides).toHaveBeenCalledWith(
      provider.id,
      [{ id: "alpha-text", modalities: ["text", "image"] }],
    ));
  });

  it("支持逐供应商刷新模型、归档和恢复", async () => {
    const user = userEvent.setup();
    let active = true;
    apiMocks.fetchGenerationProviders.mockImplementation(async () => [{ ...provider, is_active: active }]);
    apiMocks.archiveProvider.mockImplementation(async () => {
      active = false;
      return { ...provider, is_active: false };
    });
    apiMocks.restoreProvider.mockImplementation(async () => {
      active = true;
      return provider;
    });
    renderDrawer(<SettingsDrawer open onClose={() => undefined} />);
    await screen.findByRole("button", { name: /Alpha OpenAI/ });

    await user.click(screen.getByRole("button", { name: "拉取模型" }));
    await waitFor(() => expect(apiMocks.refreshProviderModels).toHaveBeenCalledWith(provider.id));
    await user.click(screen.getByRole("button", { name: "归档" }));
    await waitFor(() => expect(apiMocks.archiveProvider).toHaveBeenCalledWith(provider.id));
    await user.click(await screen.findByRole("button", { name: "恢复" }));
    expect(apiMocks.restoreProvider).toHaveBeenCalledWith(provider.id);
  });

  it("先锁定后端再删除浏览器凭据，锁定失败时仍完成本地删除", async () => {
    const user = userEvent.setup();
    await saveCredential(provider.id, "sk-delete-locally");
    const indexedDbDelete = vi.spyOn(IDBObjectStore.prototype, "delete");
    apiMocks.lockProvider.mockRejectedValueOnce(new Error("503 lock unavailable"));
    renderDrawer(<SettingsDrawer open onClose={() => undefined} />);
    await screen.findByText("本地凭据已保存");

    await user.click(screen.getByRole("button", { name: "删除凭据" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "本地凭据已删除，但后端锁定失败：503 lock unavailable",
    );
    expect(await hasCredential(provider.id)).toBe(false);
    expect(apiMocks.lockProvider.mock.invocationCallOrder[0]).toBeLessThan(
      indexedDbDelete.mock.invocationCallOrder[0],
    );
  });

  it("拒绝不安全 URL，并保留连接鉴权错误的原始消息", async () => {
    const user = userEvent.setup();
    renderDrawer(<SettingsDrawer open onClose={() => undefined} />);
    await screen.findByRole("button", { name: /Alpha OpenAI/ });
    const input = screen.getByLabelText("Base URL");
    await user.clear(input);
    await user.type(input, "https://user:secret@example.com/v1?token=x#models");
    await user.click(screen.getByRole("button", { name: "保存并解锁" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不能包含凭据、查询参数或片段");
    expect(apiMocks.updateProviderProfile).not.toHaveBeenCalled();

    await user.clear(input);
    await user.type(input, provider.base_url);
    await user.type(screen.getByPlaceholderText("仅在本机加密保存"), "sk-invalid");
    apiMocks.testProvider.mockRejectedValueOnce(new Error("401 Unauthorized: invalid API key"));
    await user.click(screen.getByRole("button", { name: "连接测试" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("401 Unauthorized: invalid API key");
    expect(screen.getByText(/无法抵御同源脚本注入/)).toBeInTheDocument();
  });
});
