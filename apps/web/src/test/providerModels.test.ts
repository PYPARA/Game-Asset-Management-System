import { describe, expect, it } from "vitest";
import { guessCapability, normalizeModelCapability } from "../lib/providerModels";

describe("供应商模型类型", () => {
  it("按视频、音频、生图、文本的优先级猜测单一类型", () => {
    expect(guessCapability("seedance-image-pro")).toBe("video");
    expect(guessCapability("voice-image-v2")).toBe("audio");
    expect(guessCapability("gpt-image-2")).toBe("image");
    expect(guessCapability("grok-imagine-1.0")).toBe("image");
    expect(guessCapability("qwen-max")).toBe("text");
  });

  it("保留单一用户类型，并归一旧的空类型和多类型", () => {
    expect(normalizeModelCapability("sora-custom", ["audio"])).toBe("audio");
    expect(normalizeModelCapability("kling-image", [])).toBe("video");
    expect(normalizeModelCapability("flux-pro", ["text", "image"])).toBe("image");
  });
});
