import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsDrawer } from "../components/SettingsDrawer";
import { deleteCredential, hasCredential, saveCredential } from "../lib/credentials";

const apiMocks = vi.hoisted(() => ({
  ensureProviderProfile: vi.fn(),
  lockProvider: vi.fn(),
  testProvider: vi.fn(),
  unlockProvider: vi.fn(),
}));

vi.mock("../lib/api", () => apiMocks);

function storeProfile(id: string, baseUrl = "https://api.openai.com/v1") {
  localStorage.setItem(
    "game-assets.provider-profile",
    JSON.stringify({
      id,
      name: "测试配置",
      baseUrl,
      textModel: "gpt-5-mini",
      imageModel: "gpt-image-2",
      quality: "high",
      concurrency: 6,
      retries: 2,
      allowPrivateNetwork: false,
    }),
  );
}

function DrawerHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        供应商设置
      </button>
      <SettingsDrawer open={open} onClose={() => setOpen(false)} />
    </>
  );
}

describe("供应商设置抽屉", () => {
  beforeEach(() => {
    apiMocks.ensureProviderProfile.mockReset();
    apiMocks.ensureProviderProfile.mockImplementation(async (profile) => ({
      id: profile.id,
      name: profile.name,
      base_url: profile.baseUrl,
    }));
    apiMocks.lockProvider.mockReset();
    apiMocks.lockProvider.mockResolvedValue(undefined);
    apiMocks.testProvider.mockReset();
    apiMocks.testProvider.mockResolvedValue({ models: ["gpt-5-mini"] });
    apiMocks.unlockProvider.mockReset();
    apiMocks.unlockProvider.mockResolvedValue(undefined);
  });

  it("圈定焦点、支持 Escape，并在关闭后恢复触发器焦点", async () => {
    const user = userEvent.setup();
    render(<DrawerHarness />);

    const trigger = screen.getByRole("button", { name: "供应商设置" });
    await user.click(trigger);

    const close = screen.getByRole("button", { name: "关闭供应商设置" });
    expect(close).toHaveFocus();

    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "保存并解锁" })).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("保存前规范化 Base URL", async () => {
    const user = userEvent.setup();
    storeProfile("normalize-test");
    render(<SettingsDrawer open onClose={() => undefined} />);

    const input = screen.getByLabelText("Base URL");
    await user.clear(input);
    await user.type(input, "HTTPS://API.Example.COM:443/v1///");
    await user.click(screen.getByRole("button", { name: "保存并解锁" }));

    await waitFor(() => expect(apiMocks.ensureProviderProfile).toHaveBeenCalled());
    expect(apiMocks.ensureProviderProfile.mock.calls[0][0].baseUrl).toBe("https://api.example.com/v1");
    const saved = JSON.parse(localStorage.getItem("game-assets.provider-profile") ?? "{}") as {
      baseUrl?: string;
    };
    expect(saved.baseUrl).toBe("https://api.example.com/v1");
  });

  it("拒绝把带凭据、查询或片段的 Base URL 写入 localStorage", async () => {
    const user = userEvent.setup();
    storeProfile("invalid-url-test");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    render(<SettingsDrawer open onClose={() => undefined} />);

    const input = screen.getByLabelText("Base URL");
    await user.clear(input);
    await user.type(input, "https://user:secret@example.com/v1?token=x#models");
    await user.click(screen.getByRole("button", { name: "保存并解锁" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("不能包含凭据、查询参数或片段");
    expect(setItem).not.toHaveBeenCalled();
    expect(apiMocks.ensureProviderProfile).not.toHaveBeenCalled();
  });

  it("先锁定后端，再删除 IndexedDB 中的凭据", async () => {
    const user = userEvent.setup();
    storeProfile("remove-order-test");
    await saveCredential("remove-order-test", "sk-delete-me");
    const indexedDbDelete = vi.spyOn(IDBObjectStore.prototype, "delete");
    render(<SettingsDrawer open onClose={() => undefined} />);

    await screen.findByText("已保存加密凭据");
    await user.click(screen.getByRole("button", { name: "删除凭据" }));

    await waitFor(async () => expect(await hasCredential("remove-order-test")).toBe(false));
    expect(apiMocks.lockProvider).toHaveBeenCalledWith("remove-order-test");
    expect(apiMocks.lockProvider.mock.invocationCallOrder[0]).toBeLessThan(
      indexedDbDelete.mock.invocationCallOrder[0],
    );
    expect(screen.getByRole("status")).toHaveTextContent("后端内存凭据已锁定");
  });

  it("后端锁定失败时仍删除本地凭据，并明确告知两个结果", async () => {
    const user = userEvent.setup();
    storeProfile("partial-delete-test");
    await saveCredential("partial-delete-test", "sk-delete-locally");
    apiMocks.lockProvider.mockRejectedValueOnce(new Error("503 lock unavailable"));
    render(<SettingsDrawer open onClose={() => undefined} />);

    await screen.findByText("已保存加密凭据");
    await user.click(screen.getByRole("button", { name: "删除凭据" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "本地凭据已删除，但后端锁定失败：503 lock unavailable",
    );
    expect(await hasCredential("partial-delete-test")).toBe(false);
  });

  it("保留鉴权、配额等连接错误的原始消息", async () => {
    const user = userEvent.setup();
    storeProfile("auth-error-test");
    apiMocks.ensureProviderProfile.mockRejectedValueOnce(new Error("401 Unauthorized: invalid API key"));
    render(<SettingsDrawer open onClose={() => undefined} />);

    await user.click(screen.getByRole("button", { name: "测试连接" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("401 Unauthorized: invalid API key");
    expect(screen.queryByText(/后端不可用/)).not.toBeInTheDocument();
  });
});
