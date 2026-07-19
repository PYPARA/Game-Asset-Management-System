import { describe, expect, it } from "vitest";
import {
  deleteCredential,
  hasCredential,
  isProviderUrlAllowed,
  normalizeProviderUrl,
  readCredential,
  saveCredential,
} from "../lib/credentials";

describe("供应商凭据", () => {
  it("按安全策略验证 Base URL", () => {
    expect(isProviderUrlAllowed("https://api.example.com/v1", false)).toBe(true);
    expect(isProviderUrlAllowed("http://127.0.0.1:9000/v1", false)).toBe(true);
    expect(isProviderUrlAllowed("http://192.168.1.8:9000/v1", false)).toBe(false);
    expect(isProviderUrlAllowed("https://192.168.1.8:9000/v1", false)).toBe(false);
    expect(isProviderUrlAllowed("http://192.168.1.8:9000/v1", true)).toBe(false);
    expect(isProviderUrlAllowed("https://192.168.1.8:9000/v1", true)).toBe(true);
    expect(isProviderUrlAllowed("https://user:secret@example.com/v1", false)).toBe(false);
    expect(isProviderUrlAllowed("https://@example.com/v1", false)).toBe(false);
    expect(isProviderUrlAllowed("https://api.example.com/v1?key=secret", false)).toBe(false);
    expect(isProviderUrlAllowed("https://api.example.com/v1?", false)).toBe(false);
    expect(isProviderUrlAllowed("https://api.example.com/v1#models", false)).toBe(false);
    expect(isProviderUrlAllowed("https://api.example.com/v1#", false)).toBe(false);
    expect(isProviderUrlAllowed("file:///tmp/key", true)).toBe(false);
  });

  it("规范化安全的 Base URL", () => {
    expect(normalizeProviderUrl("  HTTPS://API.Example.COM:443/v1///  ", false)).toBe(
      "https://api.example.com/v1",
    );
    expect(normalizeProviderUrl("http://[::1]:8787/v1/", false)).toBe("http://[::1]:8787/v1");
    expect(normalizeProviderUrl("https://[fd00::1]/v1", false)).toBeNull();
    expect(normalizeProviderUrl("https://[fd00::1]/v1", true)).toBe("https://[fd00::1]/v1");
  });

  it("在 IndexedDB 中保存、解锁并删除加密 Key", async () => {
    await saveCredential("vitest-provider", "sk-not-plaintext");

    expect(await hasCredential("vitest-provider")).toBe(true);
    expect(await readCredential("vitest-provider")).toBe("sk-not-plaintext");

    await deleteCredential("vitest-provider");
    expect(await hasCredential("vitest-provider")).toBe(false);
  });
});
