import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Workbench } from "../Workbench";
import { deleteCredential, saveCredential } from "../lib/credentials";
import "../styles.css";

function json(payload: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function generationConversationRecord(id: string) {
  return {
    id,
    project_id: "project-1",
    plan_id: null,
    thread_id: null,
    purpose: "generation_planning",
    title: null,
    agent_model: null,
    status: "awaiting_user",
    context_hash: "context-hash",
    context: { seed_asset_ids: [] },
    draft: {
      version: 1,
      title: "",
      summary: "",
      tasks: [],
      questions: [],
      assumptions: [],
      warnings: [],
      settings: {
        extra_call_budget: 2,
        max_paid_remediation_rounds: 2,
        max_transport_retries: 2,
        max_concurrency: 3,
      },
      context_hash: "context-hash",
    },
    draft_hash: "draft-hash",
    draft_version: 1,
    budget_limit: 8,
    budget_used: 0,
    turn_count: 0,
    diagnostic_reason: null,
    stop_reason: null,
    created_at: "2026-08-10T00:00:00Z",
    updated_at: "2026-08-10T00:00:00Z",
    completed_at: null,
    archived_at: null,
  };
}

interface MockAssetApiOptions {
  providers?: Array<Record<string, unknown>>;
  failUnlockIds?: Set<string>;
  assets?: Array<Record<string, unknown>>;
}

function mockAssetApi(jobs: unknown[] = [], options: MockAssetApiOptions = {}) {
  const assets = options.assets ?? [
    { id: "asset-shen", key: "portrait.shen-yan.neutral", title: "沈渊（文官）· 中立姿态" },
    { id: "asset-han", key: "portrait.han-lie.resolute", title: "韩烈（大将军）· 坚毅" },
  ];
  const unlocked = new Set<string>();
  const generationConversations: Array<Record<string, unknown>> = [];
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
            kind: asset.kind ?? "media",
            subtype: asset.subtype ?? "角色 / 立绘",
            tags: asset.tags ?? ["角色"],
            content_status: asset.content_status ?? "candidate",
            generation_status: asset.generation_status ?? "idle",
            latest_candidate_revision_id: asset.latest_candidate_revision_id === undefined
              ? `revision-${asset.id}`
              : asset.latest_candidate_revision_id,
            updated_at: "2026-07-19T12:00:00Z",
          })),
        );
      }
      if (url.startsWith("/api/jobs?")) return json(jobs);
      if (url === "/api/projects/project-1/narrative-map") {
        return json({
          project_id: "project-1",
          project_name: "真实项目",
          chapters: [],
          scenes: [],
          unassigned_scene_ids: [],
          coverage: { required: 0, ready: 0, candidate: 0, planned: 0, missing: 0, ratio: 1 },
        });
      }
      if (url === "/api/providers") {
        return json((options.providers ?? []).map((provider) => ({
          ...provider,
          is_unlocked: unlocked.has(String(provider.id)),
        })));
      }
      if (url === "/api/generation-conversations" && init?.method === "POST") {
        const created = generationConversationRecord(`session-${generationConversations.length + 1}`);
        generationConversations.push(created);
        return json(created, 201);
      }
      if (url.startsWith("/api/generation-conversations?")) return json(generationConversations);
      if (url.startsWith("/api/generation-conversations/") && url.endsWith("/events?after=0")) return json([]);
      if (url.startsWith("/api/generation-conversations/")) return json(generationConversations[0] ?? generationConversationRecord("session-1"));
      if (url === "/api/generation-agent/capabilities") {
        return json({ available: false, adapter: null, version: null, models: [], diagnostic: {} });
      }
      if (url === "/api/provider-defaults") return json({ text: null, image: null, video: null, audio: null, max_concurrency: 3, max_transport_retries: 2, updated_at: null });
      if (url.startsWith("/api/releases?")) return json([]);
      if (url === "/api/projects/project-1/export-config") {
        return json({ project_id: "project-1", project: {}, local: { game_root: null } });
      }
      if (url.startsWith("/api/deliveries?")) return json([]);
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
    window.localStorage.removeItem("gams.followedAssets.project-1");
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
    expect(screen.getByLabelText("资产检查器")).toHaveTextContent("portrait.han-lie.resolute");
    expect(screen.getByLabelText("资产检查器")).not.toHaveTextContent("portrait.shen-yan.neutral");
  });

  it("从待审查返回资产库时会清除临时状态筛选", async () => {
    mockAssetApi([], {
      assets: [
        {
          id: "pending-portrait",
          key: "portrait.pending.neutral",
          title: "待审查立绘",
          kind: "media",
          subtype: "portrait",
          content_status: "candidate",
          latest_candidate_revision_id: "revision-pending-portrait",
        },
        {
          id: "approved-portrait",
          key: "portrait.approved.neutral",
          title: "已审查立绘",
          kind: "media",
          subtype: "portrait",
          content_status: "approved",
          latest_candidate_revision_id: null,
          current_revision_id: "revision-approved-portrait",
        },
      ],
    });
    const user = userEvent.setup();
    renderWorkbench();

    const reviewShortcut = await screen.findByRole("button", { name: "待审查 1" });
    await user.click(reviewShortcut);
    expect(reviewShortcut).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("1 项结果")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("portrait.pending.neutral")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).queryByText("portrait.approved.neutral")).not.toBeInTheDocument();

    // The section heading is also a deliberate “back to the catalogue”
    // affordance; it must clear the transient workspace predicate just like
    // a leaf entry does.
    await user.click(screen.getByRole("button", { name: "打开资产库" }));
    expect(reviewShortcut).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("2 项结果")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("portrait.pending.neutral")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("portrait.approved.neutral")).toBeInTheDocument();
  });

  it("可关注资产并从已关注工作区找回", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    const follow = await screen.findByRole("button", { name: "关注资产" });
    await user.click(follow);
    expect(screen.getByRole("button", { name: "取消关注资产" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("已关注「沈渊（文官）· 中立姿态」。")).toBeInTheDocument();
    await waitFor(() => expect(window.localStorage.getItem("gams.followedAssets.project-1")).toBe(JSON.stringify(["asset-shen"])));

    await user.click(screen.getByRole("button", { name: "已关注" }));
    expect(screen.getByText("1 项结果")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("portrait.shen-yan.neutral")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).queryByText("portrait.han-lie.resolute")).not.toBeInTheDocument();
  });

  it("已发布工作区只显示真正进入 Release 的资产", async () => {
    mockAssetApi([], {
      assets: [
        {
          id: "published-portrait",
          key: "portrait.published.neutral",
          title: "已发布立绘",
          kind: "media",
          subtype: "portrait",
          content_status: "approved",
          publication_status: "published",
          latest_candidate_revision_id: null,
        },
        {
          id: "ready-portrait",
          key: "portrait.ready.neutral",
          title: "待发布立绘",
          kind: "media",
          subtype: "portrait",
          content_status: "approved",
          publication_status: "ready",
          latest_candidate_revision_id: null,
        },
      ],
    });
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(await screen.findByRole("button", { name: "已发布" }));
    expect(screen.getByText("1 项结果")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("portrait.published.neutral")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).queryByText("portrait.ready.neutral")).not.toBeInTheDocument();
  });

  it("没有创建者元数据时明确说明我创建的不可用", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(await screen.findByRole("button", { name: "我创建的" }));
    expect(screen.getByText("当前项目没有创建者信息")).toBeInTheDocument();
    expect(screen.getByText(/不会猜测资产归属/)).toBeInTheDocument();
    expect(screen.getByText("当前本机模式没有创建者信息，暂时无法筛选“我创建的”。")).toBeInTheDocument();
  });

  it("在资产制作台与暗色叙事地图之间切换", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    const { container } = renderWorkbench();

    await screen.findByText("本地模式");
    expect(container.firstElementChild).toHaveClass("asset-mode");

    await user.click(screen.getByRole("button", { name: "叙事地图" }));
    expect(container.firstElementChild).toHaveClass("narrative-mode");
    expect(screen.getByText("Narrative Atlas")).toBeInTheDocument();
    expect(await screen.findByLabelText("叙事地图工作台")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "资产制作台" }));
    expect(container.firstElementChild).toHaveClass("asset-mode");
    expect(screen.getByText("游戏资产制作台")).toBeInTheDocument();
  });

  it("媒体叶子任务视图互斥，并把未知 subtype 放入其他类型", async () => {
    mockAssetApi([], {
      assets: [
        {
          id: "scene-one",
          key: "content.scene.one",
          title: "场景一",
          kind: "content",
          subtype: "scene",
          latest_candidate_revision_id: null,
        },
        {
          id: "portrait-one",
          key: "media.portrait.one",
          title: "角色立绘",
          kind: "media",
          subtype: "character_portrait",
        },
        {
          id: "ending-one",
          key: "media.ending.background.one",
          title: "结局插画",
          kind: "media",
          subtype: "ending_illustration_background",
        },
        {
          id: "icon-one",
          key: "media.icon.one",
          title: "物品图标",
          kind: "media",
          subtype: "item_icon",
        },
        {
          id: "future-one",
          key: "media.future.one",
          title: "未来媒体",
          kind: "media",
          subtype: "future_media",
        },
      ],
    });
    const user = userEvent.setup();
    renderWorkbench();

    expect(await screen.findByRole("button", { name: "场景 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "角色立绘 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "结局插画 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "场景背景 0" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "其他类型 1" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "结局插画 1" }));
    expect(screen.getByText("1 项结果")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("media.ending.background.one")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).queryByText("media.future.one")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "其他类型 1" }));
    expect(screen.getByText("1 项结果")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("media.future.one")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).queryByText("media.ending.background.one")).not.toBeInTheDocument();
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

  it("独立打开供应商渠道抽屉", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(screen.getByRole("button", { name: "供应商渠道" }));

    expect(screen.getByRole("dialog", { name: "供应商渠道" })).toBeInTheDocument();
    expect(screen.getByText("活动供应商")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "新建供应商" }).length).toBeGreaterThan(0);
    expect(screen.queryByText("本机安全边界")).not.toBeInTheDocument();
  });

  it("独立打开系统设置并展示本机安全边界", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(screen.getByRole("button", { name: "系统设置" }));

    expect(screen.getByRole("dialog", { name: "系统设置" })).toBeInTheDocument();
    expect(screen.getByText("本机安全边界")).toBeInTheDocument();
    expect(screen.getByText(/无法抵御同源脚本注入/)).toBeInTheDocument();
  });

  it("打开 Release 交付抽屉并读取本机 checkout 配置", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(await screen.findByRole("button", { name: "Release 与游戏交付" }));

    expect(screen.getByRole("dialog", { name: "Release 与游戏交付" })).toBeInTheDocument();
    expect(screen.getByText("绑定游戏 checkout")).toBeInTheDocument();
    expect(screen.queryByText("当前只有 Release v1；请用 M3 CLI/API 创建 Manifest v2。")).not.toBeInTheDocument();
    expect(screen.getByText("尚无外部交付记录。")).toBeInTheDocument();
  });

  it("打开版本控制说明，并在关闭后恢复触发器焦点", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    const trigger = await screen.findByRole("button", { name: /本地提交/ });
    await user.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: "版本控制边界" });
    expect(dialog).toHaveTextContent("Git 操作由你确认");
    expect(dialog).toHaveTextContent("workspace/");
    expect(dialog).toHaveTextContent("不会替你推送到远端");
    await waitFor(() => expect(screen.getByRole("button", { name: "关闭版本控制说明" })).toHaveFocus());

    await user.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "版本控制边界" })).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
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

      await user.click(screen.getByRole("button", { name: "供应商渠道" }));
      const drawer = screen.getByRole("dialog", { name: "供应商渠道" });
      const failedRow = within(drawer).getByText("锁定供应商").closest("article");
      expect(failedRow).not.toBeNull();
      await user.click(within(failedRow as HTMLElement).getByRole("button", { name: "编辑" }));
      expect(within(drawer).getByLabelText("渠道名称")).toHaveValue("锁定供应商");
      await waitFor(() => expect(within(drawer).getAllByText("已保存 API Key · 等待解锁").length).toBeGreaterThan(0));
      await user.click(within(drawer).getByRole("button", { name: "返回供应商列表" }));
      const readyRow = within(drawer).getByText("已解锁供应商").closest("article");
      expect(readyRow).not.toBeNull();
      await user.click(within(readyRow as HTMLElement).getByRole("button", { name: "编辑" }));
      expect(within(drawer).getByLabelText("渠道名称")).toHaveValue("已解锁供应商");
      expect(within(drawer).getAllByText("连接可用").length).toBeGreaterThan(0);
    } finally {
      await Promise.all([
        deleteCredential(failedProviderId),
        deleteCredential(readyProviderId),
      ]);
    }
  });

  it("从顶栏打开生成中心，批量审查区不再创建生成任务", async () => {
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

    const centerButton = await screen.findByRole("button", { name: "生成中心" });
    await waitFor(() => expect(centerButton).toBeEnabled());
    expect(screen.queryByRole("button", { name: "新建生成任务" })).not.toBeInTheDocument();
    await user.click(centerButton);
    expect(await screen.findByText("选择一个会话，或开始新的规划。"))
      .toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "新建会话" }).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "收起生成中心" }));
    await waitFor(() => expect(document.activeElement).toBe(centerButton));

    await user.click(screen.getByRole("checkbox", { name: "选择 沈渊（文官）· 中立姿态" }));
    expect(screen.getByRole("button", { name: "需要重做" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "检查运行" })).toBeEnabled();
  });

  it("生成中心打开时，供应商渠道弹窗显示在生成工作台上方", async () => {
    mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    const centerButton = await screen.findByRole("button", { name: "生成中心" });
    await waitFor(() => expect(centerButton).toBeEnabled());
    await user.click(centerButton);

    const generationSurface = await waitFor(() => {
      const element = document.querySelector<HTMLElement>(".generation-drawer-page");
      expect(element).not.toBeNull();
      return element as HTMLElement;
    });

    await user.click(screen.getByRole("button", { name: "供应商渠道" }));
    const providerDialog = await screen.findByRole("dialog", { name: "供应商渠道" });
    const providerBackdrop = providerDialog.closest<HTMLElement>(".provider-channel-backdrop");

    expect(providerBackdrop).not.toBeNull();
    expect(Number(getComputedStyle(providerBackdrop as HTMLElement).zIndex))
      .toBeGreaterThan(Number(getComputedStyle(generationSurface).zIndex));
  });

  it("新建会话只创建一次，并允许第二个会话继续独立运行", async () => {
    const fetchMock = mockAssetApi();
    const user = userEvent.setup();
    renderWorkbench();

    const centerButton = await screen.findByRole("button", { name: "生成中心" });
    await waitFor(() => expect(centerButton).toBeEnabled());
    await user.click(centerButton);
    await user.click(screen.getAllByRole("button", { name: "新建会话" })[0]);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input, init]) => String(input) === "/api/generation-conversations" && init?.method === "POST")).toHaveLength(1));

    const firstSessionButton = await screen.findByRole("button", { name: "新建会话" });
    await user.click(firstSessionButton);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input, init]) => String(input) === "/api/generation-conversations" && init?.method === "POST")).toHaveLength(2));
    expect(document.querySelector(".generation-center-action b")).toHaveTextContent("2");
  });
});
