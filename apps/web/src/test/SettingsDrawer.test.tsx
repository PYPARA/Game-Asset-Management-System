import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProviderChannelsDrawer } from "../components/ProviderChannelsDrawer";
import { SystemSettingsDrawer } from "../components/SystemSettingsDrawer";
import { hasCredential, saveCredential } from "../lib/credentials";
import type { GenerationProviderProfile } from "../types";

const apiMocks = vi.hoisted(() => ({
  archiveProvider: vi.fn(),
  createProviderProfile: vi.fn(),
  ensureProviderProfile: vi.fn(),
  fetchProviderModelsDirect: vi.fn(),
  fetchGenerationProviders: vi.fn(),
  fetchProviderDefaults: vi.fn(),
  fetchSystemInfo: vi.fn(),
  lockProvider: vi.fn(),
  restoreProvider: vi.fn(),
  unlockProvider: vi.fn(),
  updateProviderProfile: vi.fn(),
  updateProviderDefaults: vi.fn(),
  updateProviderModelOverrides: vi.fn(),
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
  credential_mode: "required",
  model_discovery_mode: "auto",
  models_path: "models",
  is_active: true,
  models: [
    { id: "alpha-text", modalities: ["text"], classification: "provider", available: true, enabled: true },
    { id: "alpha-image", modalities: ["image"], classification: "heuristic", available: true, enabled: true },
    { id: "alpha-old", modalities: [], classification: "unknown", available: false, enabled: false },
    { id: "seedance-pro", modalities: ["video"], classification: "heuristic", available: true, enabled: true },
    { id: "voice-pro", modalities: ["audio"], classification: "heuristic", available: true, enabled: true },
  ],
  models_refreshed_at: "2026-07-30T08:00:00Z",
  models_sync: { state: "synced", checked_at: "2026-07-30T08:00:00Z", endpoint: "https://api.example.com/v1/models", status_code: 200, message: "模型目录已同步。", hint: null, request_id: null },
  model_catalog_api_version: 2,
  is_unlocked: false,
};

function renderDrawer(node: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>);
}

function DrawerHarness() {
  const [open, setOpen] = useState(false);
  return <><button type="button" onClick={() => setOpen(true)}>供应商渠道</button><ProviderChannelsDrawer open={open} onClose={() => setOpen(false)} /></>;
}

describe("供应商渠道抽屉", () => {
  beforeEach(() => {
    localStorage.clear();
    for (const mock of Object.values(apiMocks)) mock.mockReset();
    apiMocks.fetchGenerationProviders.mockResolvedValue([provider]);
    apiMocks.fetchProviderDefaults.mockResolvedValue({
      text: { provider_profile_id: provider.id, model: provider.text_model },
      image: { provider_profile_id: provider.id, model: provider.image_model },
      video: { provider_profile_id: provider.id, model: "seedance-pro" },
      audio: { provider_profile_id: provider.id, model: "voice-pro" },
      max_concurrency: 3,
      max_transport_retries: 2,
      updated_at: "2026-07-30T08:00:00Z",
    });
    apiMocks.fetchSystemInfo.mockResolvedValue({
      projects_root: "/tmp/projects",
      state_dir: "/tmp/state",
      project_workspace: "<project>/workspace",
      credential_store: "browser IndexedDB",
    });
    apiMocks.ensureProviderProfile.mockImplementation(async (draft) => ({ id: draft.id, ...draft }));
    apiMocks.createProviderProfile.mockResolvedValue({ ...provider, id: "provider-new", name: "新渠道" });
    apiMocks.updateProviderProfile.mockResolvedValue(provider);
    apiMocks.updateProviderDefaults.mockResolvedValue({
      text: { provider_profile_id: provider.id, model: provider.text_model },
      image: { provider_profile_id: provider.id, model: provider.image_model },
      video: { provider_profile_id: provider.id, model: "seedance-pro" },
      audio: { provider_profile_id: provider.id, model: "voice-pro" },
      max_concurrency: 3,
      max_transport_retries: 2,
      updated_at: "2026-08-03T08:00:00Z",
    });
    apiMocks.archiveProvider.mockResolvedValue({ ...provider, is_active: false });
    apiMocks.restoreProvider.mockResolvedValue(provider);
    apiMocks.lockProvider.mockResolvedValue(undefined);
    apiMocks.unlockProvider.mockResolvedValue(undefined);
    apiMocks.fetchProviderModelsDirect.mockResolvedValue({
      endpoint: "https://api.example.com/v1/models",
      statusCode: 200,
      requestId: "req-1",
      models: [{ id: "alpha-text" }, { id: "new-text" }],
    });
    apiMocks.updateProviderModelOverrides.mockResolvedValue({
      provider_profile_id: provider.id,
      models: provider.models,
      refreshed_at: provider.models_refreshed_at,
      new_model_ids: [],
      cleared_default_routes: [],
      model_catalog_api_version: 2,
      models_sync: provider.models_sync,
    });
  });

  it("独立打开、支持 Escape，并在关闭后恢复触发器焦点", async () => {
    const user = userEvent.setup();
    renderDrawer(<DrawerHarness />);
    const trigger = screen.getByRole("button", { name: "供应商渠道" });
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "供应商渠道" });
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByText("活动供应商")).toBeInTheDocument();
    expect(within(dialog).getByText("Alpha OpenAI")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭供应商渠道" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("保存当前输入的 API Key，并允许新增渠道", async () => {
    const user = userEvent.setup();
    renderDrawer(<ProviderChannelsDrawer open onClose={() => undefined} />);
    await screen.findByText("Alpha OpenAI");
    await user.click(screen.getByRole("button", { name: "新建供应商" }));
    expect(screen.queryByText("并发任务")).not.toBeInTheDocument();
    expect(screen.queryByText(/单次价格/)).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("接口地址"));
    await user.type(screen.getByLabelText("接口地址"), "HTTPS://API.Example.COM:443/v1///");
    await user.type(screen.getByLabelText("API Key"), "sk-new-key");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(apiMocks.createProviderProfile).toHaveBeenCalled());
    expect(apiMocks.createProviderProfile.mock.calls[0][0].baseUrl).toBe("https://api.example.com/v1");
    expect(apiMocks.createProviderProfile.mock.calls[0][0]).not.toHaveProperty("textModel");
    expect(apiMocks.createProviderProfile.mock.calls[0][0]).not.toHaveProperty("imageModel");
    expect(screen.queryByText("gpt-5-mini")).not.toBeInTheDocument();
    await waitFor(() => expect(apiMocks.unlockProvider).toHaveBeenCalledWith("provider-new", "sk-new-key"));
    expect(await hasCredential("provider-new")).toBe(true);
  });

  it("模型弹窗是嵌套焦点层，Escape 只关闭模型弹窗", async () => {
    const user = userEvent.setup();
    renderDrawer(<ProviderChannelsDrawer open onClose={() => undefined} />);
    await screen.findByText("Alpha OpenAI");
    const pickerTrigger = screen.getByRole("button", { name: "管理模型" });
    await user.click(pickerTrigger);
    const picker = screen.getByRole("dialog", { name: "选择渠道模型" });
    await waitFor(() => expect(within(picker).getByRole("textbox", { name: "搜索模型" })).toHaveFocus());
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "选择渠道模型" })).not.toBeInTheDocument());
    expect(screen.getByRole("dialog", { name: "供应商渠道" })).toBeInTheDocument();
    expect(pickerTrigger).toHaveFocus();
  });

  it("模型弹窗支持直接拉取、搜索、手动模型和批量启用", async () => {
    const user = userEvent.setup();
    renderDrawer(<ProviderChannelsDrawer open onClose={() => undefined} />);
    await screen.findByText("Alpha OpenAI");
    await user.click(screen.getByRole("button", { name: "编辑" }));
    await user.type(screen.getByLabelText("API Key"), "sk-model-list");
    await user.click(screen.getByRole("button", { name: "管理模型" }));
    const picker = screen.getByRole("dialog", { name: "选择渠道模型" });
    expect(picker).toBeInTheDocument();
    await user.type(within(picker).getByRole("textbox", { name: "搜索模型" }), "alpha-text");
    expect(within(picker).getAllByText("alpha-text").length).toBeGreaterThan(0);
    await user.clear(within(picker).getByRole("textbox", { name: "搜索模型" }));
    await user.click(within(picker).getByRole("tab", { name: /已有模型/ }));
    await user.click(within(picker).getByRole("button", { name: "拉取模型列表" }));
    await waitFor(() => expect(apiMocks.fetchProviderModelsDirect).toHaveBeenCalledWith(
      expect.objectContaining({ baseUrl: provider.base_url, modelsPath: provider.models_path }),
      "sk-model-list",
    ));
    expect(await within(picker).findByText("new-text")).toBeInTheDocument();
    await user.type(within(picker).getByRole("textbox", { name: "手动输入模型 ID" }), "manual-model");
    await user.click(within(picker).getByRole("button", { name: "添加模型" }));
    await user.click(within(picker).getByRole("button", { name: "确定" }));
    await waitFor(() => expect(apiMocks.updateProviderModelOverrides).toHaveBeenCalled());
    expect(apiMocks.updateProviderModelOverrides.mock.calls[0][1]).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "manual-model", enabled: true, modalities: ["text"], classification: "manual", available: false }),
    ]));
    expect(apiMocks.updateProviderModelOverrides.mock.calls[0][2]).toMatchObject({
      catalogRefreshed: true,
      requestId: "req-1",
    });
  });

  it("模型拉取会优先使用当前输入的 API Key", async () => {
    const user = userEvent.setup();
    renderDrawer(<ProviderChannelsDrawer open onClose={() => undefined} />);
    await screen.findByText("Alpha OpenAI");
    await user.click(screen.getByRole("button", { name: "编辑" }));
    await user.type(screen.getByLabelText("API Key"), "sk-direct");
    await user.click(screen.getByRole("button", { name: "管理模型" }));
    await user.click(screen.getByRole("button", { name: "拉取模型列表" }));
    await waitFor(() => expect(apiMocks.fetchProviderModelsDirect).toHaveBeenCalledWith(
      expect.objectContaining({ baseUrl: provider.base_url }),
      "sk-direct",
    ));
    expect(apiMocks.unlockProvider).not.toHaveBeenCalled();
  });

  it("系统默认模型仍可改类型或删除，并在确认后原子清空受影响路由", async () => {
    const user = userEvent.setup();
    apiMocks.updateProviderModelOverrides.mockResolvedValueOnce({
      provider_profile_id: provider.id,
      models: provider.models.filter((model) => model.id !== "alpha-old").map((model) =>
        model.id === "alpha-text" ? { ...model, modalities: ["video"] } : model,
      ),
      refreshed_at: provider.models_refreshed_at,
      new_model_ids: [],
      cleared_default_routes: ["text"],
      model_catalog_api_version: 2,
      models_sync: provider.models_sync,
    });
    renderDrawer(<ProviderChannelsDrawer open onClose={() => undefined} />);
    await screen.findByText("Alpha OpenAI");
    await user.click(screen.getByRole("button", { name: "管理模型" }));
    const picker = screen.getByRole("dialog", { name: "选择渠道模型" });

    expect(within(picker).getByRole("combobox", { name: "alpha-text 的模型类型" })).toBeEnabled();
    expect(within(picker).getByRole("button", { name: "删除模型 alpha-text" })).toBeEnabled();
    const capabilitySelect = within(picker).getByRole("combobox", { name: "alpha-text 的模型类型" });
    await user.click(capabilitySelect);
    await user.click(within(picker).getByRole("option", { name: "视频" }));
    await user.click(within(picker).getByRole("button", { name: "删除模型 alpha-old" }));
    await user.click(within(picker).getByRole("button", { name: "确定" }));
    expect(apiMocks.updateProviderModelOverrides).not.toHaveBeenCalled();
    expect(within(picker).getByText(/文本.*默认路由将变为/)).toBeInTheDocument();
    await user.click(within(picker).getByRole("button", { name: "返回修改" }));
    expect(apiMocks.updateProviderModelOverrides).not.toHaveBeenCalled();
    await user.click(within(picker).getByRole("button", { name: "确定" }));
    apiMocks.fetchGenerationProviders.mockResolvedValue([{
      ...provider,
      models: provider.models.filter((model) => model.id !== "alpha-old").map((model) =>
        model.id === "alpha-text" ? { ...model, modalities: ["video"] } : model,
      ),
    }]);
    await user.click(within(picker).getByRole("button", { name: "确认并清空默认路由" }));

    await waitFor(() => expect(apiMocks.updateProviderModelOverrides).toHaveBeenCalled());
    expect(apiMocks.updateProviderModelOverrides.mock.calls[0][1]).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "alpha-text", modalities: ["video"] }),
    ]));
    expect(apiMocks.updateProviderModelOverrides.mock.calls[0][2]).toMatchObject({
      removedModelIds: ["alpha-old"],
      catalogApiVersion: 2,
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "选择渠道模型" })).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "管理模型" }));
    const reopened = screen.getByRole("dialog", { name: "选择渠道模型" });
    expect(within(reopened).queryByRole("button", { name: "删除模型 alpha-old" })).not.toBeInTheDocument();
  });

  it("归档、恢复和删除凭据仍可用", async () => {
    const user = userEvent.setup();
    await saveCredential(provider.id, "sk-delete-locally");
    apiMocks.lockProvider.mockRejectedValueOnce(new Error("503 lock unavailable"));
    renderDrawer(<ProviderChannelsDrawer open onClose={() => undefined} />);
    await screen.findByText("Alpha OpenAI");
    await user.click(screen.getByRole("button", { name: "编辑" }));
    await user.click(await screen.findByRole("button", { name: "删除凭据" }));
    expect(await screen.findByText("本地凭据已删除，但后端锁定失败：503 lock unavailable")).toBeInTheDocument();
    expect(await hasCredential(provider.id)).toBe(false);
    await user.click(screen.getByRole("button", { name: "归档" }));
    await waitFor(() => expect(apiMocks.archiveProvider).toHaveBeenCalledWith(provider.id));
  });

  it("拒绝不安全接口地址并保留诊断错误", async () => {
    const user = userEvent.setup();
    renderDrawer(<ProviderChannelsDrawer open onClose={() => undefined} />);
    await screen.findByText("Alpha OpenAI");
    await user.click(screen.getByRole("button", { name: "编辑" }));
    const endpoint = screen.getByLabelText("接口地址");
    await user.clear(endpoint);
    await user.type(endpoint, "https://user:secret@example.com/v1?token=x#models");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("接口地址必须使用 HTTPS");
    expect(apiMocks.updateProviderProfile).not.toHaveBeenCalled();
  });

  it("系统设置只展示已启用模型，并保存默认路由", async () => {
    const user = userEvent.setup();
    renderDrawer(<SystemSettingsDrawer open onClose={() => undefined} />);
    const dialog = await screen.findByRole("dialog", { name: "系统设置" });
    expect(within(dialog).getByText("本机安全边界")).toBeInTheDocument();
    expect(within(dialog).getAllByRole("combobox", { name: "渠道" })).toHaveLength(4);
    const modelSelects = within(dialog).getAllByRole("combobox", { name: "模型" });
    expect(modelSelects).toHaveLength(4);
    expect(within(dialog).queryByRole("option", { name: "alpha-old" })).not.toBeInTheDocument();
    await user.click(modelSelects[2]);
    expect(await within(dialog).findByRole("option", { name: "seedance-pro" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("option", { name: "seedance-pro" }));
    await user.click(modelSelects[3]);
    expect(await within(dialog).findByRole("option", { name: "voice-pro" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("option", { name: "voice-pro" }));
    expect(within(dialog).queryByText("API Key")).not.toBeInTheDocument();
    const concurrency = within(dialog).getByRole("spinbutton", { name: /默认计划并发/ });
    const retries = within(dialog).getByRole("spinbutton", { name: /默认网络重试/ });
    await user.clear(concurrency);
    await user.type(concurrency, "6");
    await user.clear(retries);
    await user.type(retries, "5");

    await user.click(within(dialog).getByRole("button", { name: "保存设置" }));
    await waitFor(() => expect(apiMocks.updateProviderDefaults).toHaveBeenCalled());
    expect(apiMocks.updateProviderDefaults.mock.calls[0][0]).toMatchObject({
      text: { provider_profile_id: provider.id, model: provider.text_model },
      image: { provider_profile_id: provider.id, model: provider.image_model },
      video: { provider_profile_id: provider.id, model: "seedance-pro" },
      audio: { provider_profile_id: provider.id, model: "voice-pro" },
      max_concurrency: 6,
      max_transport_retries: 5,
    });
    expect(await within(dialog).findByText("默认路由和新计划运行默认值已保存；已确认任务继续使用冻结快照。")).toBeInTheDocument();
  });

  it("未设置的系统路由选择渠道后仍要求手动选择模型", async () => {
    const user = userEvent.setup();
    apiMocks.fetchProviderDefaults.mockResolvedValue({ text: null, image: null, video: null, audio: null, max_concurrency: 3, max_transport_retries: 2, updated_at: null });
    apiMocks.updateProviderDefaults.mockResolvedValue({ text: null, image: null, video: null, audio: null, max_concurrency: 3, max_transport_retries: 2, updated_at: null });
    renderDrawer(<SystemSettingsDrawer open onClose={() => undefined} />);
    const dialog = await screen.findByRole("dialog", { name: "系统设置" });
    const [textProvider] = within(dialog).getAllByRole("combobox", { name: "渠道" });
    const [textModel] = within(dialog).getAllByRole("combobox", { name: "模型" });

    await user.click(textProvider);
    await user.click(within(dialog).getByRole("option", { name: "Alpha OpenAI" }));
    expect(textModel).toHaveTextContent("选择已启用模型");
    await user.click(textModel);
    expect(within(dialog).getByRole("option", { name: "alpha-text" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "保存设置" }));
    await waitFor(() => expect(apiMocks.updateProviderDefaults).toHaveBeenCalledWith(expect.objectContaining({
      text: null,
      image: null,
      video: null,
      audio: null,
    })));
  });
});
