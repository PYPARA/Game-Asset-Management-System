import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchWorkbench,
  fetchAssetDetails,
  fetchProviderModelsDirect,
  ProviderDirectError,
  createAssetRevision,
  createProviderProfile,
  createProject,
  submitReview,
  updateProject,
  updateProviderModelOverrides,
} from "../lib/api";

function json(payload: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("真实 API 契约", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("模型目录从浏览器直接请求供应商 Base URL，并解析常见响应格式", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("https://provider.example/v1/models");
      expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer direct-secret");
      return json({ data: [{ id: "text-model" }, { model_id: "image-model" }] });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchProviderModelsDirect(
      { baseUrl: "https://provider.example/v1", modelsPath: "models" },
      "direct-secret",
    )).resolves.toMatchObject({
      endpoint: "https://provider.example/v1/models",
      models: [{ id: "text-model" }, { id: "image-model" }],
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("新渠道请求不携带固定文字或图片模型", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      expect(body).not.toHaveProperty("text_model");
      expect(body).not.toHaveProperty("image_model");
      expect(body).not.toHaveProperty("concurrency");
      expect(body).not.toHaveProperty("max_retries");
      expect(body).not.toHaveProperty("pricing");
      return json({
        id: "provider-new",
        name: body.name,
        kind: "openai_compatible",
        base_url: body.base_url,
        text_model: "",
        image_model: "",
        models: [],
      }, 201);
    });
    vi.stubGlobal("fetch", fetchMock);

    await createProviderProfile({
      id: "",
      name: "空渠道",
      baseUrl: "https://provider.example/v1",
      allowPrivateNetwork: false,
      credentialMode: "required",
      modelDiscoveryMode: "auto",
      modelsPath: "models",
    });
  });

  it("模型确认通过本地 PATCH 原子保存类型与删除项", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/providers/provider-1/models");
      expect(init?.method).toBe("PATCH");
      expect(JSON.parse(String(init?.body))).toMatchObject({
        models: [{ id: "seedance-pro", modalities: ["video"] }],
        removed_model_ids: ["gpt-5-mini"],
      });
      return json({
        provider_profile_id: "provider-1",
        models: [],
        refreshed_at: null,
        model_catalog_api_version: 2,
        cleared_default_routes: [],
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    await updateProviderModelOverrides(
      "provider-1",
      [{ id: "seedance-pro", modalities: ["video"], classification: "heuristic", available: true }],
      { removedModelIds: ["gpt-5-mini"], catalogApiVersion: 2 },
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("旧后端忽略删除字段时明确失败，不显示模型删除成功", async () => {
    const fetchMock = vi.fn(() => json({
      provider_profile_id: "provider-1",
      models: [{
        id: "gpt-5-mini",
        modalities: ["text"],
        classification: "manual",
        available: false,
        enabled: true,
      }],
      refreshed_at: null,
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(updateProviderModelOverrides(
      "provider-1",
      [],
      { removedModelIds: ["gpt-5-mini"], catalogApiVersion: 2 },
    )).rejects.toThrow("本机后端未加载模型删除接口的新版本");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("目录接口版本过旧时在发送删除请求前停止", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(updateProviderModelOverrides(
      "provider-1",
      [],
      { removedModelIds: ["gpt-5-mini"], catalogApiVersion: 1 },
    )).rejects.toThrow("本机后端未加载模型删除接口的新版本");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("直连供应商的鉴权错误保留诊断但不泄漏 API Key", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({ error: { message: "invalid key direct-secret" } }),
      { status: 401, headers: { "Content-Type": "application/json", "x-request-id": "provider-req-1" } },
    ))));

    await expect(fetchProviderModelsDirect(
      { baseUrl: "https://provider.example/v1" },
      "direct-secret",
    )).rejects.toMatchObject({
      status: 401,
      endpoint: "https://provider.example/v1/models",
      requestId: "provider-req-1",
      hint: "检查 API Key 和供应商权限。",
    });
    try {
      await fetchProviderModelsDirect({ baseUrl: "https://provider.example/v1" }, "direct-secret");
    } catch (error) {
      expect(error).toBeInstanceOf(ProviderDirectError);
      expect((error as Error).message).not.toContain("direct-secret");
    }
  });

  it("后端在线但没有项目时显示真实空状态", async () => {
    vi.stubGlobal("fetch", vi.fn(() => json([])));

    const result = await fetchWorkbench();
    expect(result).toMatchObject({
      project: { id: "", name: "尚未登记项目" },
      assets: [],
    });
  });

  it("后端不可用时透传错误，不返回任何替代资产", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("backend offline")));

    await expect(fetchWorkbench()).rejects.toThrow("backend offline");
  });

  it("工作台读取已建索引时不重复触发 Project 全量扫描", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/projects") return json([{ id: "project-1", name: "真实项目" }]);
      if (url.startsWith("/api/assets?")) return json([]);
      if (url.startsWith("/api/jobs?")) return json([]);
      if (url.startsWith("/api/relations?")) return json([]);
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await fetchWorkbench();

    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/scan"))).toBe(false);
  });

  it("保留硬 QA 失败任务状态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/projects") return json([{ id: "project-1", name: "真实项目" }]);
        if (url === "/api/projects/project-1/scan") return json({ errors: [] });
        if (url.startsWith("/api/assets?")) return json([]);
        if (url.startsWith("/api/relations?")) return json([]);
        if (url.startsWith("/api/jobs?")) {
          return json([
            {
              id: "job-qa-failed",
              task_id: "draw-invalid",
              status: "qa_failed",
              progress: 1,
              result_revision_id: "revision-invalid",
            },
          ]);
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const result = await fetchWorkbench();

    expect(result.job).toMatchObject({
      id: "job-qa-failed",
      name: "draw-invalid",
      status: "qa_failed",
      completed: 0,
      passed: 0,
    });
  });

  it("严格区分待审候选与已批准修订", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/projects") {
          return json([{ id: "project-1", name: "真实项目", root_path: "/tmp/project", asset_count: 1 }]);
        }
        if (url === "/api/projects/project-1/scan") return json({ errors: [] });
        if (url.startsWith("/api/assets?")) {
          return json([
            {
              id: "asset-1",
              project_id: "project-1",
              key: "story.chapter.one",
              title: "第一章",
              kind: "content",
              subtype: "chapter",
              tags: [],
              content_status: "approved",
              generation_status: "idle",
              publication_status: "ready",
              current_revision_id: "revision-approved",
              latest_candidate_revision_id: "revision-candidate",
              updated_at: "2026-07-19T12:00:00Z",
            },
          ]);
        }
        if (url.startsWith("/api/jobs?")) return json([]);
        if (url.startsWith("/api/relations?")) return json([]);
        if (url.startsWith("/api/revisions?asset_id=")) {
          return json([
            {
              id: "revision-candidate",
              asset_id: "asset-1",
              sequence: 2,
              format: "markdown",
              content: { prompt: "真实候选" },
              review_status: "pending",
              created_at: "2026-07-19T12:00:00Z",
            },
            {
              id: "revision-approved",
              asset_id: "asset-1",
              sequence: 1,
              format: "markdown",
              content: { prompt: "已批准正文" },
              review_status: "approved",
              created_at: "2026-07-18T12:00:00Z",
            },
          ]);
        }
        if (url.includes("/renditions")) return json([]);
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const result = await fetchWorkbench();
    expect(result.assets).toHaveLength(1);
    expect(result.assets[0].detailsLoaded).toBe(false);
    const details = await fetchAssetDetails(result.assets[0]);
    expect(details).toMatchObject({
      candidateRevisionId: "revision-candidate",
      approvedRevisionId: "revision-approved",
      reviewStatus: "pending",
      reviewReady: true,
      reviewBlockReason: undefined,
      prompt: "真实候选",
    });
    expect(details.revisions.map((revision) => revision.label)).toEqual([
      "r02（候选）",
      "r01（已批准）",
    ]);
    expect(details.thumbnails).toEqual([]);
  });

  it("为内容和实体生成非黑屏预览，并隐藏未知历史时间", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/projects") return json([{ id: "project-1", name: "真实项目" }]);
        if (url === "/api/projects/project-1/scan") return json({ errors: [] });
        if (url.startsWith("/api/assets?")) return json([
          {
            id: "content-1", key: "content.event.v3.test", title: "测试事件", kind: "content", subtype: "event",
            tags: [], content_status: "candidate", generation_status: "idle", publication_status: "unpublished",
            updated_at: "2026-07-19T00:00:00Z",
            asset_metadata: { production_stage: "imported", time_accuracy: "unknown", domain: "court", preview_summary: "一段真实剧情摘要" },
          },
          {
            id: "character-1", key: "entity.character.hero", title: "主角", kind: "entity", subtype: "character",
            tags: [], content_status: "candidate", generation_status: "idle", publication_status: "unpublished",
            updated_at: "2026-07-19T00:00:00Z", asset_metadata: { production_stage: "imported", time_accuracy: "unknown" },
          },
          {
            id: "location-1", key: "entity.location.palace", title: "宣德殿", kind: "entity", subtype: "location",
            tags: [], content_status: "candidate", generation_status: "idle", publication_status: "unpublished",
            updated_at: "2026-07-19T00:00:00Z", asset_metadata: { production_stage: "imported", time_accuracy: "unknown" },
          },
          {
            id: "portrait-1", key: "portrait.hero.neutral", title: "主角立绘", kind: "media", subtype: "portrait",
            tags: [], content_status: "candidate", generation_status: "succeeded", publication_status: "unpublished",
            latest_candidate_revision_id: "revision-portrait", updated_at: "2026-07-19T00:00:00Z",
            asset_metadata: { production_stage: "reviewed", time_accuracy: "unknown" },
          },
        ]);
        if (url.startsWith("/api/jobs?")) return json([]);
        if (url.startsWith("/api/relations?")) return json([{ source_asset_id: "portrait-1", target_asset_id: "character-1", relation_type: "depicts" }]);
        if (url.includes("/api/revisions?asset_id=portrait-1")) return json([{ id: "revision-portrait", asset_id: "portrait-1", sequence: 1, format: "media", content: {}, review_status: "pending", created_at: "2026-07-19T00:00:00Z" }]);
        if (url.includes("/api/revisions/revision-portrait/renditions")) return json([{ id: "rendition-portrait", revision_id: "revision-portrait", media_type: "image/webp", width: 1024, height: 1536, byte_size: 100 }]);
        if (url.includes("/api/qa-runs?")) return json([{ id: "qa-1", rendition_id: "rendition-portrait", verdict: "pass", checks: [], created_at: "2026-01-01T00:00:00Z" }]);
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const result = await fetchWorkbench();
    const content = result.assets.find((asset) => asset.id === "content-1");
    const character = result.assets.find((asset) => asset.id === "character-1");
    const location = result.assets.find((asset) => asset.id === "location-1");
    const portrait = result.assets.find((asset) => asset.id === "portrait-1");
    expect(content).toMatchObject({
      updatedLabel: "历史时间未知",
      productionStage: "imported",
      preview: { kind: "content", meta: "朝堂", summary: "一段真实剧情摘要" },
    });
    expect(character?.preview).toMatchObject({ kind: "image", label: "关联立绘" });
    expect(location).toMatchObject({
      subtypeLabel: "地点",
      preview: { kind: "placeholder", label: "地点实体" },
    });
    expect(character?.thumbnails).toEqual(["/api/renditions/rendition-portrait/content"]);
    expect(portrait).toMatchObject({ productionStage: "reviewed", updatedLabel: "历史时间未知" });
  });

  it("编辑结构化资产时通过修订接口保存新候选", async () => {
    const asset = {
      id: "content-1",
      key: "content.event.test",
      name: "旧标题",
      kind: "content" as const,
      category: "content",
      subtype: "event",
      subtypeLabel: "事件",
      tags: [],
      reviewStatus: "pending" as const,
      productionStage: "imported",
      timeAccuracy: "unknown" as const,
      detailsLoaded: true,
      candidateRevisionId: "revision-1",
      reviewReady: true,
      preview: { kind: "content" as const, label: "事件", meta: "朝堂", summary: "旧摘要" },
      qaPassed: 0,
      qaTotal: 0,
      updatedAt: "",
      updatedLabel: "历史时间未知",
      updatedBy: "历史导入",
      thumbnails: [],
      linkedMedia: [],
      relatedAssets: [],
      revisionFormat: "json" as const,
      revisionContent: { id: "test", title: "旧标题" },
      revisions: [],
      prompt: "",
      negativePrompt: "",
      model: "unknown",
      recipe: "unknown",
      seed: "unknown",
      qa: [],
    };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/revisions" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toMatchObject({
          asset_id: "content-1",
          format: "json",
          content: { id: "test", title: "新标题" },
          parent_revision_id: "revision-1",
        });
        return json({ id: "revision-2", asset_id: "content-1", sequence: 2, format: "json", content: { id: "test", title: "新标题" }, review_status: "pending", created_at: "2026-07-19T13:00:00Z" }, 201);
      }
      if (url.includes("/api/revisions?asset_id=content-1")) return json([{ id: "revision-2", asset_id: "content-1", sequence: 2, format: "json", content: { id: "test", title: "新标题" }, review_status: "pending", created_at: "2026-07-19T13:00:00Z" }]);
      if (url.includes("/renditions")) return json([]);
      throw new Error(`unexpected request: ${url}`);
    }));
    const result = await createAssetRevision(asset, { id: "test", title: "新标题" });
    expect(result).toMatchObject({ candidateRevisionId: "revision-2", reviewReady: true, revisionContent: { title: "新标题" } });
  });

  it("批量审核逐项报告部分失败", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        const revisionId = JSON.parse(String(init?.body)).revision_id;
        return revisionId === "candidate-ok"
          ? json({ id: "review-1" }, 201)
          : json({ detail: "hard QA must pass" }, 409);
      }),
    );

    await expect(submitReview(["candidate-ok", "candidate-bad"], "approve")).resolves.toEqual({
      succeeded: ["candidate-ok"],
      failed: [{ revisionId: "candidate-bad", message: "hard QA must pass" }],
    });
  });

  it("标准 Project 创建后立即执行扫描", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/projects") {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({
          directory_name: "sample",
          name: "Sample",
          default_language: "zh-CN",
        });
        return json({ id: "project-1", name: "Sample", root_path: "/projects/sample" }, 201);
      }
      if (url === "/api/projects/project-1/scan") {
        return json({ assets_indexed: 315, revisions_indexed: 315, errors: [] });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(createProject("sample", "Sample", "zh-CN")).resolves.toMatchObject({
      scan: { assets_indexed: 315, revisions_indexed: 315 },
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("更新已导入项目的显示名称", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/projects/project-imported");
      expect(init?.method).toBe("PATCH");
      expect(JSON.parse(String(init?.body))).toEqual({ name: "新名称" });
      return json({ id: "project-imported", name: "新名称", root_path: "/projects/imported" });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(updateProject("project-imported", "新名称")).resolves.toMatchObject({
      id: "project-imported",
      name: "新名称",
      path: "/projects/imported",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
