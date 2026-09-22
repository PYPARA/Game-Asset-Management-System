import type { ProviderModelModality } from "../types";

export const PROVIDER_MODEL_TYPES: ProviderModelModality[] = ["text", "image", "video", "audio"];

const VIDEO_HINTS = ["seedance", "video", "sora", "veo", "kling", "wan", "hailuo"];
const AUDIO_HINTS = ["audio", "tts", "speech", "voice", "music", "sound"];
const IMAGE_HINTS = ["seedream", "gpt-image", "image", "imagine", "dall-e", "imagen", "flux", "sdxl", "midjourney"];

export function guessCapability(modelId: string): ProviderModelModality {
  const lowered = modelId.toLocaleLowerCase();
  if (VIDEO_HINTS.some((token) => lowered.includes(token))) return "video";
  if (AUDIO_HINTS.some((token) => lowered.includes(token))) return "audio";
  if (IMAGE_HINTS.some((token) => lowered.includes(token))) return "image";
  return "text";
}

export function normalizeModelCapability(
  modelId: string,
  modalities: readonly unknown[] | null | undefined,
): ProviderModelModality {
  const valid = [...new Set((modalities ?? []).filter(
    (value): value is ProviderModelModality => PROVIDER_MODEL_TYPES.includes(value as ProviderModelModality),
  ))];
  return valid.length === 1 ? valid[0] : guessCapability(modelId);
}

export function modelCapabilityLabel(capability: ProviderModelModality): string {
  if (capability === "image") return "生图";
  if (capability === "video") return "视频";
  if (capability === "audio") return "音频";
  return "文本";
}
