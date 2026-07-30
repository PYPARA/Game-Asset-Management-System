import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Workbench } from "../Workbench";
import { deleteCredential, saveCredential } from "../lib/credentials";

function json(payload: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

interface MockAssetApiOptions {
  providers?: Array<Record<string, unknown>>;
  failUnlockIds?: Set<string>;
}

function mockAssetApi(jobs: unknown[] = [], options: MockAssetApiOptions = {}) {
  const assets = [
    { id: "asset-shen", key: "portrait.shen-yan.neutral", title: "沈渊（文官）· 中立姿态" },
    { id: "asset-han", key: "portrait.han-lie.resolute", title: "韩烈（大将军）· 坚毅" },
  ];
  const unlocked = new Set<string>();
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/projects") {
        return json([{ id: "project-1", name: "真实项目", root_path: "/tmp/project" }]);
      }
      if (url === "/api/projects/project-1/scan") return json({ errors: [] });
      if (url.startsWith("/api/assets?")) {
        return json(
          assets.map((asset) => ({
            ...asset,
            kind: "media",
            subtype: "角色 / 立绘",
            tags: ["角色"],
            content_status: "candidate",
            generation_status: "idle",
            latest_candidate_revision_id: `revision-${asset.id}`,
            updated_at: "2026-07-19T12:00:00Z",
          })),
        );
      }
      if (url.startsWith("/api/jobs?")) return json(jobs);
      if (url === "/api/providers") {
        return json((options.providers ?? []).map((provider) => ({
          ...provider,
          is_unlocked: unlocked.has(String(provider.id)),
        })));
      }
      if (url === "/api/provider-defaults") return json({ text: null, image: null, updated_at: null });
      const unlockMatch = /^\/api\/providers\/([^/]+)\/unlock$/.exec(url);
      if (unlockMatch && init?.method === "POST") {
        const providerId = decodeURIComponent(unlockMatch[1]);
        if (options.failUnlockIds?.has(providerId)) {
          return json({ detail: `unlock failed for ${providerId}` }, 401);
        }
        unlocked.add(providerId);
        return json({ detail: "provider unlocked for this process" });
      }
      if (url === "/api/system") {
        return json({
          projects_root: "/tmp/projects",
          state_dir: "/tmp/state",
          project_workspace: "<project>/workspace",
          credential_store: "browser IndexedDB",
        });
      }
      if (url.startsWith("/api/relations?")) return json([]);
      if (url.startsWith("/api/revisions?asset_id=")) {
        const assetId = new URL(url, "http://local").searchParams.get("asset_id");
        return json([
          {
            id: `revision-${assetId}`,
            asset_id: assetId,
            sequence: 1,
            format: "webp",
            content: { prompt: "真实候选" },
            review_status: "pending",
            created_at: "2026-07-19T12:00:00Z",
          },
        ]);
      }
      if (url.includes("/renditions")) {
        const revisionId = url.split("/api/revisions/")[1].split("/renditions")[0];
        return json([
          {
            id: `rendition-${revisionId}`,
            revision_id: revisionId,
            media_type: "image/webp",
            width: 1024,
            height: 1536,
            byte_size: 1024,
          },
        ]);
      }
      if (url.startsWith("/api/qa-runs?")) {
        const renditionId = new URL(url, "http://local").searchParams.get("rendition_id");
        return json([
          {
            id: `qa-${renditionId}`,
            rendition_id: renditionId,
            verdict: "pass",
            checks: [{ name: "decodable", passed: true, message: "通过" }],
            created_at: "2026-07-19T12:00:00Z",
          },
        ]);
      }
      if (url === "/api/reviews" && init?.method === "POST") return json({ id: "review-1" }, 201);
      throw new Error(`unexpected request: ${url}`);
    });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderWorkbench() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Workbench />
    </QueryClientProvider>,
  );
}

describe("制作台", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("backend offline")));
  });

  it("在后端离线时只展示错误状态", async () => {
    renderWorkbench();

    expect(await screen.findByText("服务异常")).toBeInTheDocument();
    expect(screen.getByText("无法加载资产数据")).toBeInTheDocument();
    expect(screen.getByText("backend offline")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试连接" })).toBeInTheDocument();
    expect(screen.queryByText("portrait.shen-yan.neutral")).not.toBeInTheDocument();
  });

  it("后端在线但尚无项目时展示真实空状态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => [],
      }),
    );

    renderWorkbench();

    expect(await screen.findByText("本地模式")).toBeInTheDocument();
    expect(screen.getByText("等待创建 Project")).toBeInTheDocument();
    expect(screen.getByText("Game-Projects 中尚无 Project")).toBeInTheDocument();
    expect(screen.queryByText("portrait.shen-yan.neutral")).not.toBeInTheDocument();
    expect(screen.queryByText("服务异常")).not.toBeInTheDocument();
  });

  it("按名称与 Key 搜索资产", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    const search = await screen.findByPlaceholderText("搜索资产 Key / 名称 / 标签");
    await user.type(search, "韩烈");

    const table = screen.getByRole("table");
    expect(within(table).getByText("portrait.han-lie.resolute")).toBeInTheDocument();
    expect(within(table).queryByText("portrait.shen-yan.neutral")).not.toBeInTheDocument();
  });

  it("支持选择并批量批准候选版本", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(await screen.findByRole("checkbox", { name: "选择 沈渊（文官）· 中立姿态" }));
    await user.click(screen.getByRole("button", { name: "批量批准版本" }));

    const row = within(screen.getByRole("table")).getByText("portrait.shen-yan.neutral").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLTableRowElement).getByText("已审查")).toBeInTheDocument();
    expect(screen.getByText("已批准 1 项资产。")).toBeInTheDocument();
  });

  it("打开供应商抽屉并明确凭据安全边界", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(screen.getByRole("button", { name: "供应商设置" }));

    expect(screen.getByRole("dialog", { name: "供应商与模型机架" })).toBeInTheDocument();
    expect(screen.getByText("本机安全边界")).toBeInTheDocument();
    expect(screen.getByText(/无法抵御同源脚本注入/)).toBeInTheDocument();
  });

  it("启动时逐个解锁活动供应商，并隔离单个凭据失败", async () => {
    const user = userEvent.setup();
    const failedProviderId = "provider-unlock-failed";
    const readyProviderId = "provider-unlock-ready";
    await Promise.all([
      saveCredential(failedProviderId, "sk-failed"),
      saveCredential(readyProviderId, "sk-ready"),
    ]);
    const providers = [
      {
        id: failedProviderId,
        name: "锁定供应商",
        kind: "openai_compatible",
        base_url: "https://locked.example/v1",
        text_model: "locked-text",
        image_model: "locked-image",
        quality: "high",
        concurrency: 2,
        max_retries: 2,
        allow_private_network: false,
        is_active: true,
        models: [{ id: "locked-text", modalities: ["text"], classification: "manual", available: true }],
        models_refreshed_at: new Date().toISOString(),
      },
      {
        id: readyProviderId,
        name: "已解锁供应商",
        kind: "openai_compatible",
        base_url: "https://ready.example/v1",
        text_model: "ready-text",
        image_model: "ready-image",
        quality: "high",
        concurrency: 2,
        max_retries: 2,
        allow_private_network: false,
        is_active: true,
        models: [{ id: "ready-text", modalities: ["text"], classification: "manual", available: true }],
        models_refreshed_at: new Date().toISOString(),
      },
    ];
    const fetchMock = mockAssetApi([], {
      providers,
      failUnlockIds: new Set([failedProviderId]),
    });

    try {
      renderWorkbench();
      await screen.findByText("本地模式");
      await waitFor(() => {
        const unlockCalls = fetchMock.mock.calls.filter(([input, init]) =>
          String(input).endsWith("/unlock") && init?.method === "POST",
        );
        expect(unlockCalls).toHaveLength(2);
      });
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/providers/${failedProviderId}/unlock`,
        expect.objectContaining({ method: "POST", body: JSON.stringify({ api_key: "sk-failed" }) }),
      );
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/providers/${readyProviderId}/unlock`,
        expect.objectContaining({ method: "POST", body: JSON.stringify({ api_key: "sk-ready" }) }),
      );

      await user.click(screen.getByRole("button", { name: "供应商设置" }));
      expect(await screen.findByRole("button", { name: /锁定供应商.*凭据锁定/ })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /已解锁供应商.*已解锁/ })).toBeInTheDocument();
    } finally {
      await Promise.all([
        deleteCredential(failedProviderId),
        deleteCredential(readyProviderId),
      ]);
    }
  });

  it("启用计划编辑器和批量需要重做入口", async () => {
    mockAssetApi([
      {
        id: "job-1",
        plan_id: "plan-1",
        task_id: "draw-hero",
        status: "candidate_ready",
        progress: 1,
        result_revision_id: "revision-asset-shen",
      },
    ]);
    const user = userEvent.setup();
    renderWorkbench();

    const createPlan = await screen.findByRole("button", { name: "新建生成计划" });
    await waitFor(() => expect(createPlan).toBeEnabled());
    await user.click(createPlan);
    expect(await screen.findByRole("dialog", { name: "编排生成计划" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭生成计划" }));

    await user.click(screen.getByRole("checkbox", { name: "选择 沈渊（文官）· 中立姿态" }));
    expect(screen.getByRole("button", { name: "需要重做" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "检查运行" })).toBeEnabled();
  });
});
