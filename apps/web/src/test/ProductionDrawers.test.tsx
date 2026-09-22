import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GenerationPlanDrawer } from "../components/GenerationPlanDrawer";
import { RunInspectorDrawer } from "../components/RunInspectorDrawer";
import type { GameAsset, RunInspection } from "../types";

function json(payload: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function queryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

async function chooseMenuOption(
  user: ReturnType<typeof userEvent.setup>,
  trigger: HTMLElement,
  optionName: string,
) {
  if (trigger.getAttribute("aria-expanded") !== "true") {
    await user.click(trigger);
  }
  if (trigger.getAttribute("aria-expanded") !== "true") {
    await user.click(trigger);
  }
  await user.click(await screen.findByRole("option", { name: optionName }));
}

const mediaAsset: GameAsset = {
  id: "asset-portrait",
  detailsLoaded: true,
  key: "portrait.hero.neutral",
  name: "主角中立立绘",
  kind: "media",
  category: "2d-media",
  subtype: "portrait",
  subtypeLabel: "角色立绘",
  tags: [],
  reviewStatus: "pending",
  productionStage: "imported",
  timeAccuracy: "known",
  preview: { kind: "placeholder", label: "媒体资产", detail: "暂无预览" },
  qaPassed: 0,
  qaTotal: 0,
  updatedAt: "2026-07-30T10:00:00Z",
  updatedLabel: "2026-07-30T10:00:00Z",
  updatedBy: "本机索引",
  thumbnails: [],
  linkedMedia: [],
  relatedAssets: [],
  revisionFormat: "media",
  revisionContent: null,
  revisions: [],
  prompt: "",
  negativePrompt: "",
  model: "unknown",
  recipe: "unknown",
  seed: "unknown",
  qa: [],
};

const sidekickAsset: GameAsset = {
  ...mediaAsset,
  id: "asset-sidekick",
  key: "portrait.sidekick.neutral",
  name: "同伴中立立绘",
};

const contentAsset: GameAsset = {
  ...mediaAsset,
  id: "asset-lore",
  key: "item.route-lore",
  name: "路线设定",
  kind: "content",
  category: "content",
  subtype: "item",
  subtypeLabel: "物品设定",
  schemaRef: "schemas/item.schema.json",
  preview: { kind: "content", label: "结构化内容", meta: "JSON", summary: "路线设定摘要" },
  revisionFormat: "json",
};

const routingProviders = [
  {
    id: "provider-text",
    name: "文字供应商",
    kind: "fake",
    base_url: "https://text.invalid/v1",
    text_model: "text-default",
    image_model: "text-provider-image",
    quality: "high",
    concurrency: 2,
    max_retries: 2,
    allow_private_network: false,
    pricing: { text_call: 0.25, image_call: 0.6 },
    is_active: true,
    is_unlocked: true,
    models_refreshed_at: "2026-07-30T08:00:00Z",
    models: [
      { id: "text-default", modalities: ["text"], classification: "provider", available: true },
      { id: "text-provider-image", modalities: ["image"], classification: "provider", available: true },
    ],
  },
  {
    id: "provider-image",
    name: "图片供应商",
    kind: "fake",
    base_url: "https://image.invalid/v1",
    text_model: "image-provider-text",
    image_model: "image-default",
    quality: "high",
    concurrency: 3,
    max_retries: 1,
    allow_private_network: false,
    pricing: { image_call: 0.8 },
    is_active: true,
    is_unlocked: true,
    models_refreshed_at: "2026-07-30T08:00:00Z",
    models: [
      { id: "image-default", modalities: ["image"], classification: "provider", available: true },
      { id: "image-provider-text", modalities: ["text"], classification: "manual", available: true },
    ],
  },
];

function mockRoutingApi() {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/providers") return json(routingProviders);
    if (url === "/api/provider-defaults") {
      return json({
        text: { provider_profile_id: "provider-text", model: "text-default" },
        image: { provider_profile_id: "provider-image", model: "image-default" },
        updated_at: "2026-07-30T08:00:00Z",
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const inspection: RunInspection = {
  plan: {
    id: "plan-1",
    project_id: "project-1",
    provider_profile_id: "provider-1",
    name: "立绘生产运行",
    status: "awaiting_user",
    tasks: [],
    estimated_calls: 1,
    estimated_cost: 0.8,
    suggested_extra_calls: 2,
    extra_call_budget: 2,
    extra_calls_used: 0,
    actual_calls: 1,
    actual_cost: 0.8,
    max_paid_remediation_rounds: 2,
    max_transport_retries: 2,
    max_concurrency: 3,
    confirmed_at: "2026-07-30T10:00:00Z",
    created_at: "2026-07-30T09:59:00Z",
  },
  jobs: [
    {
      id: "job-1",
      plan_id: "plan-1",
      project_id: "project-1",
      provider_profile_id: "provider-1",
      task_id: "draw-hero",
      task_kind: "image",
      request: {
        id: "draw-hero",
        kind: "image",
        asset_id: "asset-portrait",
        prompt: "主角立绘",
        depends_on: [],
      },
      status: "awaiting_user",
      stage: "hard_qa",
      progress: 0.7,
      result_revision_id: "revision-1",
      error_category: "validation",
      error_message: "宽度不符合规格",
      attempt_count: 1,
      paid_remediation_rounds: 0,
      lease_owner: null,
      lease_expires_at: null,
      heartbeat_at: null,
      resolved_request: {},
      provider_snapshot: {
        id: "provider-1",
        name: "测试供应商",
        model: "gpt-image-1",
      },
      pending_action_id: null,
      created_at: "2026-07-30T10:00:00Z",
      updated_at: "2026-07-30T10:01:00Z",
    },
  ],
  attempts: [
    {
      id: "attempt-1",
      job_id: "job-1",
      number: 1,
      status: "succeeded",
      phase: "succeeded",
      purpose: "base",
      idempotency_key: "call-1",
      request_id: "request-1",
      error_category: null,
      error_message: null,
      output_hash: "abc",
      billable: true,
      estimated_cost: 0.8,
      started_at: "2026-07-30T10:00:00Z",
      completed_at: "2026-07-30T10:01:00Z",
    },
  ],
  findings: [
    {
      id: "finding-1",
      job_id: "job-1",
      revision_id: "revision-1",
      code: "media.width_mismatch",
      severity: "error",
      blocking: true,
      evidence: [],
      suggested_action: "tool_repair",
      occurrence: 1,
      resolved_at: null,
      created_at: "2026-07-30T10:01:00Z",
    },
  ],
  evidence: [
    {
      id: "evidence-1",
      job_id: "job-1",
      revision_id: "revision-1",
      kind: "candidate",
      label: "当前候选",
      path: "workspace/candidate.webp",
      media_type: "image/webp",
      sha256: "0123456789abcdef",
      byte_size: 1200,
      metadata: {},
      created_at: "2026-07-30T10:01:00Z",
    },
  ],
  actions: [],
  events: [
    {
      id: "event-1",
      sequence: 1,
      job_id: "job-1",
      attempt_id: "attempt-1",
      event_type: "finding.created",
      stage: "hard_qa",
      data: { code: "media.width_mismatch" },
      causation_id: null,
      created_at: "2026-07-30T10:01:00Z",
    },
  ],
};

describe("M2 生产抽屉", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("只有在预算复核并显式勾选后才确认计划", async () => {
    const user = userEvent.setup();
    const confirmed = vi.fn();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/providers") {
        return json([
          {
            id: "provider-1",
            name: "确定性测试供应商",
            kind: "fake",
            text_model: "fake-text",
            image_model: "fake-image",
            concurrency: 4,
            pricing: { image_call: 0.8 },
            is_unlocked: true,
            models: [
              { id: "fake-text", modalities: ["text"], classification: "heuristic", available: true, enabled: true },
              { id: "fake-image", modalities: ["image"], classification: "heuristic", available: true, enabled: true },
            ],
          },
        ]);
      }
      if (url === "/api/provider-defaults") {
        return json({
          text: { provider_profile_id: "provider-1", model: "fake-text" },
          image: { provider_profile_id: "provider-1", model: "fake-image" },
          max_concurrency: 5,
          max_transport_retries: 4,
          updated_at: null,
        });
      }
      if (url === "/api/generation-plans" && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        expect(body).toMatchObject({
          project_id: "project-1",
          extra_call_budget: 2,
          max_paid_remediation_rounds: 2,
          max_transport_retries: 4,
          max_concurrency: 5,
          tasks: [
            {
              kind: "image",
              asset_id: "asset-portrait",
              provider_profile_id: "provider-1",
              model: "fake-image",
              width: 1024,
              height: 1024,
              transparent: true,
              depends_on: [],
            },
          ],
        });
        expect(body).not.toHaveProperty("estimated_calls");
        return json({ id: "plan-created" }, 201);
      }
      if (url === "/api/generation-plans/plan-created/confirm" && init?.method === "POST") {
        return json([]);
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <QueryClientProvider client={queryClient()}>
        <GenerationPlanDrawer
          open
          project={{ id: "project-1", name: "测试 Project", path: "/tmp/project", assetCount: 1, thumbnail: "" }}
          assets={[mediaAsset]}
          initialAssetIds={[mediaAsset.id]}
          onClose={() => undefined}
          onConfirmed={confirmed}
        />
      </QueryClientProvider>,
    );

    const dialog = screen.getByRole("dialog", { name: "编排生成计划" });
    expect((await within(dialog).findAllByText("确定性测试供应商")).length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText("主角中立立绘").length).toBeGreaterThan(0);
    await user.click(within(dialog).getByRole("button", { name: "校验并查看预算" }));

    const confirmButton = screen.getByRole("button", { name: "确认并启动计划" });
    expect(confirmButton).toBeDisabled();
    expect(screen.getByText(/未知交付不会自动重试/)).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /我确认本次调用额度与运行约束/ }));
    expect(confirmButton).toBeEnabled();
    await user.click(confirmButton);

    await waitFor(() => expect(confirmed).toHaveBeenCalledWith("plan-created"));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/generation-plans/plan-created/confirm",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("重命名上游任务时同步更新因果链", async () => {
    const user = userEvent.setup();
    let submitted: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/providers") {
        return json([{
          id: "provider-1",
          name: "确定性测试供应商",
          kind: "fake",
          text_model: "fake-text",
          image_model: "fake-image",
          concurrency: 4,
          pricing: { image_call: 0.8 },
          is_unlocked: true,
          models: [
            { id: "fake-text", modalities: ["text"], classification: "heuristic", available: true, enabled: true },
            { id: "fake-image", modalities: ["image"], classification: "heuristic", available: true, enabled: true },
          ],
        }]);
      }
      if (url === "/api/provider-defaults") {
        return json({
          text: { provider_profile_id: "provider-1", model: "fake-text" },
          image: { provider_profile_id: "provider-1", model: "fake-image" },
          updated_at: null,
        });
      }
      if (url === "/api/generation-plans" && init?.method === "POST") {
        submitted = JSON.parse(String(init.body));
        return json({ id: "plan-renamed" }, 201);
      }
      if (url === "/api/generation-plans/plan-renamed/confirm" && init?.method === "POST") {
        return json([]);
      }
      throw new Error(`unexpected request: ${url}`);
    }));
    render(
      <QueryClientProvider client={queryClient()}>
        <GenerationPlanDrawer
          open
          project={{ id: "project-1", name: "测试 Project", path: "/tmp/project", assetCount: 2, thumbnail: "" }}
          assets={[mediaAsset, sidekickAsset]}
          initialAssetIds={[mediaAsset.id, sidekickAsset.id]}
          onClose={() => undefined}
          onConfirmed={() => undefined}
        />
      </QueryClientProvider>,
    );

    const dialog = screen.getByRole("dialog", { name: "编排生成计划" });
    expect((await within(dialog).findAllByText("确定性测试供应商")).length).toBeGreaterThan(0);
    const originalRootId = "produce-portrait-hero-neutral-1";
    await user.click(within(dialog).getByRole("checkbox", { name: originalRootId }));
    const taskIds = within(dialog).getAllByLabelText("任务 ID");
    await user.clear(taskIds[0]);
    await user.type(taskIds[0], "root-renamed");
    expect(within(dialog).getByRole("checkbox", { name: "root-renamed" })).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "校验并查看预算" }));
    await user.click(screen.getByRole("checkbox", { name: /我确认本次调用额度与运行约束/ }));
    await user.click(screen.getByRole("button", { name: "确认并启动计划" }));

    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toMatchObject({
      tasks: [
        { id: "root-renamed", depends_on: [] },
        { id: "produce-portrait-sidekick-neutral-2", depends_on: ["root-renamed"] },
      ],
    });
  });

  it("按任务类型继承全局路由，并在切换供应商时要求重新选择模型", async () => {
    const user = userEvent.setup();
    mockRoutingApi();
    render(
      <QueryClientProvider client={queryClient()}>
        <GenerationPlanDrawer
          open
          project={{ id: "project-1", name: "测试 Project", path: "/tmp/project", assetCount: 1, thumbnail: "" }}
          assets={[mediaAsset]}
          initialAssetIds={[mediaAsset.id]}
          onClose={() => undefined}
          onConfirmed={() => undefined}
        />
      </QueryClientProvider>,
    );

    const providerSelect = await screen.findByLabelText("任务供应商");
    await waitFor(() => expect(providerSelect).toHaveValue("provider-image"));
    expect(screen.getByLabelText("图片模型")).toHaveValue("image-default");

    await chooseMenuOption(user, providerSelect, "文字供应商");
    expect(screen.getByLabelText("图片模型")).toHaveValue("");

    await chooseMenuOption(user, screen.getByLabelText("任务类型"), "结构化文本");
    expect(screen.getByLabelText("任务供应商")).toHaveValue("provider-text");
    expect(screen.getByLabelText("文字模型")).toHaveValue("text-default");

    await chooseMenuOption(user, screen.getByLabelText("任务类型"), "参考图编辑");
    expect(screen.getByLabelText("任务供应商")).toHaveValue("provider-image");
    expect(screen.getByLabelText("图片模型")).toHaveValue("image-default");
  });

  it("默认路由变化不会改写已经建立的草稿任务", async () => {
    const client = queryClient();
    mockRoutingApi();
    render(
      <QueryClientProvider client={client}>
        <GenerationPlanDrawer
          open
          project={{ id: "project-1", name: "测试 Project", path: "/tmp/project", assetCount: 1, thumbnail: "" }}
          assets={[mediaAsset]}
          initialAssetIds={[mediaAsset.id]}
          onClose={() => undefined}
          onConfirmed={() => undefined}
        />
      </QueryClientProvider>,
    );

    const providerSelect = await screen.findByLabelText("任务供应商");
    await waitFor(() => expect(providerSelect).toHaveValue("provider-image"));
    expect(screen.getByLabelText("图片模型")).toHaveValue("image-default");

    act(() => {
      client.setQueryData(["provider-defaults"], {
        text: { provider_profile_id: "provider-text", model: "text-default" },
        image: { provider_profile_id: "provider-text", model: "text-provider-image" },
        updated_at: "2026-07-30T09:00:00Z",
      });
    });
    const snapshot = screen.getByLabelText("全局默认路由快照源");
    await waitFor(() => expect(within(snapshot).getByText("text-provider-image")).toBeInTheDocument());
    expect(providerSelect).toHaveValue("provider-image");
    expect(screen.getByLabelText("图片模型")).toHaveValue("image-default");
  });

  it("混合任务分别继承文字与图片路由，且不再展示渠道价格或成本路由", async () => {
    const user = userEvent.setup();
    mockRoutingApi();
    render(
      <QueryClientProvider client={queryClient()}>
        <GenerationPlanDrawer
          open
          project={{ id: "project-1", name: "测试 Project", path: "/tmp/project", assetCount: 2, thumbnail: "" }}
          assets={[contentAsset, mediaAsset]}
          initialAssetIds={[contentAsset.id, mediaAsset.id]}
          onClose={() => undefined}
          onConfirmed={() => undefined}
        />
      </QueryClientProvider>,
    );

    const providerSelects = await screen.findAllByLabelText("任务供应商");
    await waitFor(() => {
      expect(providerSelects[0]).toHaveValue("provider-text");
      expect(providerSelects[1]).toHaveValue("provider-image");
    });
    expect(screen.getByLabelText("文字模型")).toHaveValue("text-default");
    expect(screen.getByLabelText("图片模型")).toHaveValue("image-default");

    await user.click(screen.getByRole("button", { name: "校验并查看预算" }));
    expect(screen.queryByText("供应商成本路由")).not.toBeInTheDocument();
    expect(screen.queryByText("预计基础成本")).not.toBeInTheDocument();
    expect(screen.getByText(/文字供应商 \/ text-default/)).toBeInTheDocument();
    expect(screen.getByText(/图片供应商 \/ image-default/)).toBeInTheDocument();
  });

  it("要求模型先登记，并阻止已停用或明确分类不兼容的模型", async () => {
    const user = userEvent.setup();
    mockRoutingApi();
    render(
      <QueryClientProvider client={queryClient()}>
        <GenerationPlanDrawer
          open
          project={{ id: "project-1", name: "测试 Project", path: "/tmp/project", assetCount: 1, thumbnail: "" }}
          assets={[mediaAsset]}
          initialAssetIds={[mediaAsset.id]}
          onClose={() => undefined}
          onConfirmed={() => undefined}
        />
      </QueryClientProvider>,
    );

    const modelInput = await screen.findByLabelText("图片模型");
    await waitFor(() => expect(modelInput).toHaveValue("image-default"));
    await user.clear(modelInput);
    await user.type(modelInput, "manual-image-custom");
    expect(screen.getByText("该模型尚未登记；请先在供应商渠道的模型选择器中添加它。")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "校验并查看预算" }));
    expect(screen.getByRole("alert")).toHaveTextContent("该模型尚未登记");

    const incompatibleModel = screen.getByLabelText("图片模型");
    await user.clear(incompatibleModel);
    await user.type(incompatibleModel, "image-provider-text");
    expect(screen.getByText("image-provider-text 已分类为不支持图片任务。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "校验并查看预算" }));
    expect(screen.getByRole("alert")).toHaveTextContent("image-provider-text 已分类为不支持图片任务");
    expect(screen.getByRole("dialog", { name: "编排生成计划" })).toBeInTheDocument();
  });

  it("把键盘焦点限制在抽屉内并在关闭后还给入口", async () => {
    const user = userEvent.setup();
    const client = queryClient();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/api/providers") return json([]);
      if (String(input) === "/api/provider-defaults") {
        return json({ text: null, image: null, updated_at: null });
      }
      throw new Error(`unexpected request: ${String(input)}`);
    }));

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <QueryClientProvider client={client}>
          <button type="button" onClick={() => setOpen(true)}>打开计划</button>
          {open ? (
            <GenerationPlanDrawer
              open
              project={{ id: "project-1", name: "测试 Project", path: "/tmp/project", assetCount: 1, thumbnail: "" }}
              assets={[mediaAsset]}
              initialAssetIds={[mediaAsset.id]}
              onClose={() => setOpen(false)}
              onConfirmed={() => undefined}
            />
          ) : null}
        </QueryClientProvider>
      );
    }

    render(<Harness />);
    const opener = screen.getByRole("button", { name: "打开计划" });
    await user.click(opener);
    const close = screen.getByRole("button", { name: "关闭生成计划" });
    await waitFor(() => expect(close).toHaveFocus());
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "校验并查看预算" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "编排生成计划" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("展示持久证据并把人工 Worker 动作绑定到 Finding", async () => {
    const user = userEvent.setup();
    const changed = vi.fn();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/generation-plans/plan-1/inspect") return json(inspection);
      if (url === "/api/jobs/job-1/remediations" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({
          action: "tool_repair",
          strategy: "normalize",
          reason: "宽度不符合规格",
          parameters: {},
          finding_ids: ["finding-1"],
          expected_additional_calls: 0,
        });
        return json({ id: "action-1", status: "accepted" }, 201);
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <QueryClientProvider client={queryClient()}>
        <RunInspectorDrawer
          open
          planId="plan-1"
          focusAssetIds={["asset-portrait"]}
          onClose={() => undefined}
          onChanged={changed}
        />
      </QueryClientProvider>,
    );

    const dialog = await screen.findByRole("dialog", { name: "立绘生产运行" });
    expect(within(dialog).getByText("media.width_mismatch")).toBeInTheDocument();
    expect(within(dialog).getByText(/无需 Codex 也可处理/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("tab", { name: /证据 1/ }));
    const image = within(dialog).getByRole("img", { name: "当前候选" });
    expect(image).toHaveAttribute("src", "/api/run-evidence/evidence-1/content");
    await user.click(within(dialog).getByRole("button", { name: "记录并执行动作" }));

    expect(await within(dialog).findByText("动作已写入事件流并进入确定性队列。")).toBeInTheDocument();
    expect(changed).toHaveBeenCalled();
  });
});
