import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  recoverGenerationTurn: vi.fn(),
  fetchAssetDetails: vi.fn(),
  answerGenerationInput: vi.fn(),
  fetchWorkbench: vi.fn(),
  fetchGenerationConversation: vi.fn(),
  fetchGenerationConversations: vi.fn(),
  fetchGenerationAgentCapabilities: vi.fn(),
  fetchGenerationProviders: vi.fn(),
  fetchGenerationConversationEvents: vi.fn(),
  subscribeToGenerationConversationEvents: vi.fn((_id: string, _onEvent: unknown, onConnection?: (connected: boolean) => void) => {
    onConnection?.(true);
    return vi.fn();
  }),
  createGenerationConversation: vi.fn(),
  sendGenerationConversationMessage: vi.fn(),
  steerGenerationConversationTurn: vi.fn(),
  cancelGenerationConversationTurn: vi.fn(),
  updateGenerationConversationContext: vi.fn(),
  updateGenerationConversationDraft: vi.fn(),
  confirmGenerationConversation: vi.fn(),
  onConfirmed: vi.fn(),
  onNewSession: vi.fn(),
  onClose: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  recoverGenerationTurn: mocks.recoverGenerationTurn,
  fetchAssetDetails: mocks.fetchAssetDetails,
  answerGenerationInput: mocks.answerGenerationInput,
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) { super(message); this.status = status; }
  },
  fetchWorkbench: mocks.fetchWorkbench,
  fetchGenerationConversation: mocks.fetchGenerationConversation,
  fetchGenerationConversations: mocks.fetchGenerationConversations,
  fetchGenerationAgentCapabilities: mocks.fetchGenerationAgentCapabilities,
  fetchGenerationProviders: mocks.fetchGenerationProviders,
  fetchGenerationConversationEvents: mocks.fetchGenerationConversationEvents,
  subscribeToGenerationConversationEvents: mocks.subscribeToGenerationConversationEvents,
  createGenerationConversation: mocks.createGenerationConversation,
  sendGenerationConversationMessage: mocks.sendGenerationConversationMessage,
  steerGenerationConversationTurn: mocks.steerGenerationConversationTurn,
  cancelGenerationConversationTurn: mocks.cancelGenerationConversationTurn,
  updateGenerationConversationContext: mocks.updateGenerationConversationContext,
  updateGenerationConversationDraft: mocks.updateGenerationConversationDraft,
  confirmGenerationConversation: mocks.confirmGenerationConversation,
}));

import { GenerationChatPage } from "../components/GenerationChatPage";
import { resetGenerationStores } from "../lib/generationStore";
import { ApiError } from "../lib/api";

const asset = {
  id: "asset-reference",
  key: "media.chapter-01-rain-reference",
  name: "夜雨参考图",
  kind: "media",
  subtype: "background",
  reviewStatus: "approved",
  schemaRef: null,
  tags: ["chapter-01"],
};

const providers = [
  {
    id: "provider-ready",
    name: "已解锁渠道",
    kind: "openai-compatible",
    base_url: "https://provider.example/v1",
    text_model: "text-pro",
    image_model: "image-pro",
    quality: "high",
    concurrency: 2,
    max_retries: 2,
    allow_private_network: false,
    is_active: true,
    is_unlocked: true,
    models: [
      { id: "text-pro", modalities: ["text"], classification: "provider", available: true, enabled: true },
      { id: "text-fast", modalities: ["text"], classification: "provider", available: true, enabled: true },
      { id: "image-pro", modalities: ["image"], classification: "provider", available: true, enabled: true },
    ],
    models_refreshed_at: null,
    model_catalog_api_version: 2,
  },
  {
    id: "provider-locked",
    name: "已锁定渠道",
    kind: "openai-compatible",
    base_url: "https://locked.example/v1",
    text_model: "locked-text",
    image_model: "locked-image",
    quality: "high",
    concurrency: 1,
    max_retries: 1,
    allow_private_network: false,
    is_active: true,
    is_unlocked: false,
    models: [
      { id: "locked-text", modalities: ["text"], classification: "provider", available: true, enabled: true },
      { id: "locked-image", modalities: ["image"], classification: "provider", available: true, enabled: true },
    ],
    models_refreshed_at: null,
    model_catalog_api_version: 2,
  },
];

function draft(overrides: Record<string, unknown> = {}) {
  return {
    version: 1,
    title: "序章夜雨背景生成",
    summary: "为序章建立一张可审查的夜雨背景。",
    tasks: [{
      id: "make-rain",
      asset: {
        mode: "new",
        asset_id: null,
        key: "media.chapter-01-rain",
        kind: "media",
        subtype: "background",
        title: "序章夜雨背景",
        schema_ref: null,
        tags: [],
        metadata: {},
      },
      kind: "image",
      prompt: "16:9 夜雨中的序章街道。",
      provider_profile_id: "provider-fake",
      model: "fake-image",
      schema: null,
      depends_on: [],
      width: 1600,
      height: 900,
      max_bytes: null,
      transparent: false,
      reference_task_id: null,
      references: [],
      target_path: "approved/assets/backgrounds/chapter-01-rain.png",
      candidate_path: "workspace/candidates/pending/make-rain",
      locked_fields: [],
      metadata: {},
    }],
    questions: [],
    assumptions: ["沿用图片默认路由"],
    warnings: [],
    settings: {
      extra_call_budget: 2,
      max_paid_remediation_rounds: 2,
      max_transport_retries: 2,
      max_concurrency: 3,
    },
    context_hash: "context-hash-1",
    ...overrides,
  };
}

function conversation(overrides: Record<string, unknown> = {}) {
  const currentDraft = draft();
  return {
    id: "session-1",
    project_id: "project-1",
    plan_id: null,
    thread_id: null,
    purpose: "generation_planning",
    title: "序章夜雨背景生成",
    status: "awaiting_user",
    context_hash: "context-hash-1",
    context: { seed_asset_ids: [asset.id] },
    draft: currentDraft,
    draft_hash: "draft-hash-1",
    draft_version: 2,
    budget_limit: 8,
    budget_used: 0,
    turn_count: 1,
    diagnostic_reason: null,
    stop_reason: null,
    created_at: "2026-08-06T00:00:00Z",
    updated_at: "2026-08-06T00:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

function setup(overrides: { events?: unknown[]; conversation?: Record<string, unknown>; providers?: unknown[]; sessionId?: string | null; createOnMount?: boolean } = {}) {
  const currentConversation = conversation(overrides.conversation);
  mocks.fetchWorkbench.mockResolvedValue({
    project: { id: "project-1", name: "示例项目" },
    assets: [asset],
    job: {},
    relations: [],
  });
  mocks.fetchGenerationConversation.mockResolvedValue(currentConversation);
  mocks.fetchGenerationConversations.mockResolvedValue([currentConversation]);
  mocks.fetchGenerationAgentCapabilities.mockResolvedValue({
    available: true,
    adapter: "test",
    version: "test",
    models: [],
    diagnostic: {},
  });
  mocks.fetchGenerationProviders.mockResolvedValue(overrides.providers ?? []);
  mocks.fetchGenerationConversationEvents.mockResolvedValue(overrides.events ?? []);
  mocks.updateGenerationConversationDraft.mockImplementation(async (_id: string, _hash: string, nextDraft: unknown) => ({
    ...currentConversation,
    draft: nextDraft,
    draft_hash: "draft-hash-edited",
    draft_version: 3,
  }));
  mocks.updateGenerationConversationContext.mockResolvedValue(currentConversation);
  mocks.confirmGenerationConversation.mockResolvedValue({
    conversation: conversation({ plan_id: "plan-1", status: "completed" }),
    plan: { id: "plan-1" },
    jobs: [{ id: "job-1" }],
    created_assets: [],
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <GenerationChatPage
        sessionId={overrides.sessionId === undefined ? "session-1" : overrides.sessionId}
        createOnMount={overrides.createOnMount}
        onConfirmed={mocks.onConfirmed}
        onNewSession={mocks.onNewSession}
        onClose={mocks.onClose}
      />
    </QueryClientProvider>,
  );
}

async function chooseMenuOption(
  user: ReturnType<typeof userEvent.setup>,
  trigger: HTMLElement,
  optionName: string | RegExp,
) {
  await user.click(trigger);
  await user.click(screen.getByRole("option", { name: optionName }));
}

describe("GenerationChatPage", () => {
  beforeEach(() => {
    resetGenerationStores();
    vi.clearAllMocks();
    sessionStorage.clear();
    Object.defineProperty(window,"innerWidth",{value:1440,writable:true});
    mocks.recoverGenerationTurn.mockResolvedValue({attempt_id:"attempt-2"});
    localStorage.removeItem("gams.openRunInspector");
  });

  it("Escape 只关闭资产预览并返回触发器，不退出会话", async () => {
    setup();mocks.fetchAssetDetails.mockResolvedValue(asset);
    const user=userEvent.setup();
    const trigger=await screen.findByRole("button",{name:"预览 夜雨参考图"});
    await user.click(trigger);
    await screen.findByRole("dialog",{name:"预览 夜雨参考图"});
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog",{name:"预览 夜雨参考图"})).not.toBeInTheDocument();
    expect(mocks.onClose).not.toHaveBeenCalled();
    expect(trigger).toHaveFocus();
  });

  it("中文输入法确认候选不会发送，并保存输入草稿", async () => {
    setup();
    const input=await screen.findByPlaceholderText("描述你想生成的资源、用途、风格或需要参考的资产…");
    fireEvent.change(input,{target:{value:"文官立绘"}});
    fireEvent.keyDown(input,{key:"Enter",isComposing:true,keyCode:229});
    expect(mocks.sendGenerationConversationMessage).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("gams.composer.session-1")).toBe("文官立绘");
  });
  it("千级资产选择器限制渲染数量并支持定向搜索", async () => {
    mocks.fetchWorkbench.mockResolvedValueOnce({project:{id:"project-1",name:"示例项目"},assets:Array.from({length:1000},(_,i)=>({...asset,id:`asset-${i}`,name:`测试参考 ${i}`,key:`media.test-${i}`})),job:{},relations:[]});
    const view=setup();const user=userEvent.setup();
    await user.click(await screen.findByRole("button",{name:"添加参考资产"}));
    const dialog=await screen.findByRole("dialog",{name:"选择 Agent 要读取的参考"});
    expect(within(dialog).getByText(/找到 1000 项/)).toBeInTheDocument();
    expect(view.container.querySelectorAll(".generation-reference-choice")).toHaveLength(120);
    await user.type(within(dialog).getByPlaceholderText(/搜索/),"测试参考 999");
    expect(view.container.querySelectorAll(".generation-reference-choice")).toHaveLength(1);
  });

  it("一万条历史消息保持时间线渲染有界", async () => {
    const events=Array.from({length:10000},(_,i)=>({id:`history-${i}`,sequence:i+1,session_id:"session-1",turn_id:`turn-${i}`,thread_id:null,event_type:"user.message",data:{content:`历史需求 ${i}`},created_at:"2026-09-22"}));
    const view=setup({events});
    await screen.findByText("历史需求 9999");
    expect(view.container.querySelectorAll(".generation-message").length).toBeLessThanOrEqual(60);
    await userEvent.setup().click(screen.getByRole("button",{name:"加载更早的对话"}));
    await screen.findByText("历史需求 9939");
    expect(view.container.querySelectorAll(".generation-message").length).toBeLessThanOrEqual(60);
  });

  it("渲染可见 Agent 消息与可展开工具事件", async () => {
    setup({
      events: [
        {
          id: "event-user",
          sequence: 1,
          session_id: "session-1",
          event_type: "user.message",
          thread_id: null,
          turn_id: "turn-1",
          data: { content: "生成序章夜雨背景" },
          created_at: "2026-08-06T00:00:00Z",
        },
        {
          id: "event-tool",
          sequence: 2,
          session_id: "session-1",
          event_type: "tool.started",
          thread_id: null,
          turn_id: "turn-1",
          data: { tool: "read_project_specs" },
          created_at: "2026-08-06T00:00:00Z",
        },
        {
          id: "event-assistant",
          sequence: 3,
          session_id: "session-1",
          event_type: "assistant.message",
          thread_id: null,
          turn_id: "turn-1",
          data: { content: "我已读取风格圣经，并把它作为主参考依据。" },
          created_at: "2026-08-06T00:00:00Z",
        },
      ],
    });

    expect(await screen.findByText("我已读取风格圣经，并把它作为主参考依据。"))
      .toBeInTheDocument();
    const tool = screen.getByRole("button", { name: /read_project_specs/ });
    await userEvent.setup().click(tool);
    expect(await screen.findByText(/"tool": "read_project_specs"/)).toBeInTheDocument();
  });

  it("预览内可以接受全部警告并执行，无需回右栏勾选",async()=>{
    setup({conversation:{draft:draft({warnings:[{code:"identity",message:"请确认原创人物设定"},{code:"route",message:"请确认生成路由"}]})}});
    const user=userEvent.setup();
    await user.click(await screen.findByRole("button",{name:"检查并生成"}));
    const dialog=screen.getByRole("dialog",{name:"确认前预览"});
    await user.click(within(dialog).getByRole("checkbox",{name:/我已检查资源/}));
    expect(within(dialog).getByRole("button",{name:"确认并执行"})).toBeDisabled();
    await user.click(within(dialog).getByRole("checkbox",{name:"请确认原创人物设定"}));
    await user.click(within(dialog).getByRole("checkbox",{name:"请确认生成路由"}));
    await user.click(within(dialog).getByRole("button",{name:"确认并执行"}));
    await waitFor(()=>expect(mocks.confirmGenerationConversation).toHaveBeenCalledWith("session-1",expect.any(String),["identity","route"],"context-hash-1"));
  });

  it("编辑草案后可预览，并且只有确认动作调用 confirm 接口", async () => {
    setup();
    const user = userEvent.setup();
    const target = await screen.findByRole("textbox", { name: "批准后落地路径" });
    await user.clear(target);
    await user.type(target, "approved/assets/backgrounds/edited-rain.png");
    await waitFor(() => expect(mocks.updateGenerationConversationDraft).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "检查并生成" }));
    const dialog = await screen.findByRole("dialog", { name: "确认前预览" });
    expect(within(dialog).getByText(/edited-rain\.png/)).toBeInTheDocument();
    const checks = screen.getAllByRole("checkbox", { name: /我已检查资源/ });
    await user.click(checks[checks.length - 1]);
    await user.click(within(dialog).getByRole("button", { name: "确认并执行" }));

    await waitFor(() => expect(mocks.confirmGenerationConversation).toHaveBeenCalledWith(
      "session-1",
      "draft-hash-edited",
      [],
      "context-hash-1",
    ));
    expect(mocks.onConfirmed).toHaveBeenCalledWith("plan-1");
    expect(localStorage.getItem("gams.openRunInspector")).toBeNull();
  });

  it("没有选中会话时展示生成中心空状态，并从会话列表新建", async () => {
    setup({ sessionId: null });

    expect(await screen.findByText("选择一个会话，或开始新的规划。"))
      .toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "新建会话" })[0]);
    expect(mocks.onNewSession).toHaveBeenCalledTimes(1);
  });

  it("收起生成中心时响应 Escape，并把关闭动作交给宿主", async () => {
    setup();
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "序章夜雨背景生成" });
    await user.keyboard("{Escape}");
    expect(mocks.onClose).toHaveBeenCalledTimes(1);
  });

  it("从聊天框选择文字供应商和模型，并保存为会话默认路由", async () => {
    setup({ providers });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "生成路由" }));

    const dialog = screen.getByRole("dialog", { name: "选择资产生成供应商和模型" });
    const providerMenu = within(dialog).getByRole("combobox", { name: "文字生成供应商" });
    await user.click(providerMenu);
    expect(screen.getByRole("option", { name: /已锁定渠道/ })).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("option", { name: "已解锁渠道" }));

    await waitFor(() => expect(mocks.updateGenerationConversationDraft).toHaveBeenCalled());
    const firstSavedDraft = mocks.updateGenerationConversationDraft.mock.calls.at(-1)?.[2];
    expect(firstSavedDraft).toMatchObject({
      settings: { route_defaults: { text: { provider_profile_id: "provider-ready", model: "text-pro" } } },
    });

    const modelMenu = within(dialog).getByRole("combobox", { name: "文字生成模型" });
    await waitFor(() => expect(modelMenu).toBeEnabled());
    await user.click(modelMenu);
    await user.click(screen.getByRole("option", { name: "text-fast" }));
    await waitFor(() => expect(mocks.updateGenerationConversationDraft).toHaveBeenCalledTimes(2));
    expect(mocks.updateGenerationConversationDraft.mock.calls.at(-1)?.[2]).toMatchObject({
      settings: { route_defaults: { text: { provider_profile_id: "provider-ready", model: "text-fast" } } },
    });
    await user.click(within(dialog).getByRole("combobox", {name:"文字模型思考等级"}));
    await user.click(screen.getByRole("option", {name:/^高$/}));
    await waitFor(()=>expect(mocks.updateGenerationConversationDraft.mock.calls.at(-1)?.[2].settings.route_defaults.text.reasoning_effort).toBe("high"));
  });

  it("已保存模型不可用时明确提示，并允许切换到可用模型", async () => {
    setup({
      providers,
      conversation: {
        draft: draft({
          settings: {
            extra_call_budget: 2,
            max_paid_remediation_rounds: 2,
            max_transport_retries: 2,
            max_concurrency: 3,
            route_defaults: { image: { provider_profile_id: "provider-ready", model: "retired-image" } },
          },
        }),
      },
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "生成路由" }));

    const dialog = screen.getByRole("dialog", { name: "选择资产生成供应商和模型" });
    expect(within(dialog).getByRole("combobox", { name: "图片生成模型" })).toHaveTextContent("当前模型不可用 · retired-image");
    expect(within(dialog).getByText("当前模型不可用，请重新选择。")).toBeInTheDocument();

    await chooseMenuOption(user, within(dialog).getByRole("combobox", { name: "图片生成模型" }), "image-pro");
    await waitFor(() => expect(mocks.updateGenerationConversationDraft).toHaveBeenCalled());
    expect(mocks.updateGenerationConversationDraft.mock.calls.at(-1)?.[2]).toMatchObject({
      settings: { route_defaults: { image: { provider_profile_id: "provider-ready", model: "image-pro" } } },
    });
  });

  it("运行中的输入使用原生 steer 追加到当前回合", async () => {
    setup({ conversation: { status: "running" } });
    const user = userEvent.setup();
    const composer = await screen.findByPlaceholderText("向正在运行的回合追加指令…");
    await user.type(composer, "优先沿用已批准的夜雨参考图");
    await user.click(screen.getByRole("button", { name: "追加指令" }));

    await waitFor(() => expect(mocks.steerGenerationConversationTurn).toHaveBeenCalledWith(
      "session-1",
      "优先沿用已批准的夜雨参考图",
      expect.any(String),
    ));
    expect(mocks.sendGenerationConversationMessage).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "生成路由" })).toBeDisabled();
  });

  it("在聊天流回答原生问题并暂停主输入", async () => {
    const inputRequest = {
      id: "input-1", turn_id: "turn-1", response_mode: "resume_turn", status: "pending",
      questions: [{ id: "role", header: "角色身份", question: "角色身份是什么？", options: [
        { label: "武将", description: "军职" }, { label: "文官", description: "朝臣" },
      ], isOther: true }], answers: {}, auto_resolution_ms: null, fallback_reason: null,
    };
    setup({ conversation: { status: "awaiting_input", pending_input: inputRequest }, events: [
      { id: "event-input", sequence: 1, session_id: "session-1", event_type: "user_input.requested", thread_id: null, turn_id: "turn-1", data: inputRequest, created_at: "2026-08-06T00:00:00Z" },
    ] });
    const user = userEvent.setup();
    expect(await screen.findByText("角色身份是什么？")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("请先回答上方问题…")).toBeDisabled();
    await user.click(screen.getByLabelText(/武将/));
    await user.click(screen.getByRole("button", { name: "提交回答" }));
    await waitFor(() => expect(mocks.answerGenerationInput).toHaveBeenCalledWith(
      "session-1", "input-1", expect.any(String), { role: { answers: ["武将"] } },
    ));
    expect(mocks.sendGenerationConversationMessage).not.toHaveBeenCalled();
    expect(mocks.steerGenerationConversationTurn).not.toHaveBeenCalled();
  });

  it("恢复绑定失败回合且不重复发送用户消息", async () => {
    setup({
      conversation: { status: "running" },
      events: [
        { id: "user-1", sequence: 1, session_id: "session-1", event_type: "user.message", thread_id: null, turn_id: "turn-1", data: { content: "生成新角色" }, created_at: "2026-08-06T00:00:00Z" },
        { id: "unavailable-1", sequence: 2, session_id: "session-1", event_type: "agent.unavailable", thread_id: null, turn_id: "turn-1", data: { error_code: "runtime_unavailable", message: "Codex 暂不可用", retryable: true }, created_at: "2026-08-06T00:00:01Z" },
        { id: "failed-1", sequence: 3, session_id: "session-1", event_type: "turn.failed", thread_id: null, turn_id: "turn-1", data: { error_code: "draft_invalid", message: "方案未通过校验", retryable: true, field_path: "warnings[0]", validation_message: "必须是包含有效 code 和非空 message 的对象" }, created_at: "2026-08-06T00:00:02Z" },
      ],
    });
    const user = userEvent.setup();

    expect(await screen.findByText("方案未通过校验")).toBeInTheDocument();
    expect(screen.queryByText("Codex 暂不可用")).not.toBeInTheDocument();
    expect(screen.getByText("warnings[0]")).toBeInTheDocument();
    expect(screen.getByText(/必须是包含有效 code/, {selector:"p"})).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续本轮" }));

    await waitFor(() => expect(mocks.recoverGenerationTurn).toHaveBeenCalledWith("session-1","turn-1",expect.any(String)));
    expect(mocks.sendGenerationConversationMessage).not.toHaveBeenCalled();
    expect(mocks.steerGenerationConversationTurn).not.toHaveBeenCalled();
  });

  it("上下文失效时刷新当前参考并立即重试原指令", async () => {
    setup({
      events: [
        { id: "user-1", sequence: 1, session_id: "session-1", event_type: "user.message", thread_id: null, turn_id: "turn-1", data: { content: "生成序章夜雨背景" }, created_at: "2026-08-06T00:00:00Z" },
        { id: "failed-1", sequence: 2, session_id: "session-1", event_type: "turn.failed", thread_id: null, turn_id: "turn-1", data: { error_code: "context_invalid", message: "规划上下文无效或已变化，请刷新上下文后重试。", retryable: true }, created_at: "2026-08-06T00:00:01Z" },
      ],
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "继续本轮" }));

    await waitFor(() => expect(mocks.recoverGenerationTurn).toHaveBeenCalledWith("session-1","turn-1",expect.any(String)));
    expect(mocks.sendGenerationConversationMessage).not.toHaveBeenCalled();
  });

  it("确认时上下文变化保留编辑并要求重新核对", async () => {
    setup();
    mocks.confirmGenerationConversation
      .mockRejectedValueOnce(new ApiError("project context changed while planning", 409));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", {name:"检查并生成"}));
    await user.click(await screen.findByRole("checkbox", { name: /我已检查资源/ }));
    await user.click(screen.getByRole("button", { name: "确认并执行" }));

    await waitFor(() => expect(mocks.updateGenerationConversationContext).toHaveBeenCalledWith("session-1", [asset.id]));
    await waitFor(() => expect(within(screen.getByRole("dialog",{name:"确认前预览"})).getByText(/项目快照已刷新，编辑内容已保留/)).toBeInTheDocument());
    expect(mocks.confirmGenerationConversation).toHaveBeenCalledTimes(1);
    expect(mocks.onConfirmed).not.toHaveBeenCalled();
  });

  it("Agent 不可用时保留人工编排入口", async () => {
    mocks.fetchGenerationAgentCapabilities.mockResolvedValueOnce({ available: false, diagnostic: { hint: "请登录 Codex" } });
    setup({ conversation: { status: "unavailable" } });
    expect(await screen.findByText("Agent 暂不可用，方案编辑仍可继续。"))
      .toBeInTheDocument();
    expect(screen.getByText("人工编排")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续人工编排" })).toBeInTheDocument();
  });

  it("草案保存失败时阻止确认，并可从可见错误重试", async () => {
    setup();
    mocks.updateGenerationConversationDraft.mockRejectedValueOnce(new Error("草案版本冲突"));
    const user = userEvent.setup();
    const target = await screen.findByRole("textbox", { name: "批准后落地路径" });
    await user.clear(target);
    await user.type(target, "approved/assets/backgrounds/retry-rain.png");

    expect(await screen.findByText("草案版本冲突")).toBeInTheDocument();
    expect(screen.getByText("草案尚未保存，预览与确认已暂停。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查并生成" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "确认并执行" })).not.toBeInTheDocument();
    expect(mocks.confirmGenerationConversation).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "重试保存" }));
    await waitFor(() => expect(mocks.updateGenerationConversationDraft).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "检查并生成" })).toBeEnabled());
  });

  it("Generation 规划和参考筛选使用可访问的 SelectMenu", async () => {
    const view = setup();
    const user = userEvent.setup();

    // The migration should leave no native single-select controls behind.
    expect(view.container.querySelectorAll("select")).toHaveLength(0);

    const source = await screen.findByRole("combobox", { name: "资源来源" });
    await chooseMenuOption(user, source, "已有项目资产");
    const existingAsset = await screen.findByRole("combobox", { name: "选择已有资产" });
    await chooseMenuOption(user, existingAsset, /夜雨参考图 · media\.chapter-01-rain-reference/);
    await waitFor(() => expect(mocks.updateGenerationConversationDraft).toHaveBeenCalled());

    // Switching back to a new asset exposes the remaining task proposal menu.
    await chooseMenuOption(user, source, "确认时新建资产");
    const kind = await screen.findByRole("combobox", { name: "资产类型" });
    await chooseMenuOption(user, kind, "内容");
    const taskType = screen.getByRole("combobox", { name: "任务类型" });
    await chooseMenuOption(user, taskType, "图像编辑");
    expect(await screen.findByRole("combobox", { name: "上游参考任务" })).toBeInTheDocument();

    // Adding a task reference reveals the reference asset and role menus.
    await chooseMenuOption(user, screen.getByRole("combobox", { name: "选择任务参考" }), /夜雨参考图/);
    await user.click(screen.getByRole("button", { name: "添加参考资源" }));
    const referenceAsset = await screen.findByRole("combobox", { name: "参考资产" });
    expect(screen.getByRole("combobox", { name: "角色" })).toBeInTheDocument();
    await chooseMenuOption(user, referenceAsset, /夜雨参考图/);
    await chooseMenuOption(user, screen.getByRole("combobox", { name: "角色" }), "辅助参考");

    // The context picker has its own three filter menus; selecting each one
    // should update the visible result without invoking a browser-native popup.
    await user.click(screen.getByRole("button", { name: "添加参考资产" }));
    const dialog = await screen.findByRole("dialog", { name: "选择 Agent 要读取的参考" });
    await chooseMenuOption(user, within(dialog).getByRole("combobox", { name: "参考资产类型" }), "媒体");
    await chooseMenuOption(user, within(dialog).getByRole("combobox", { name: "参考资产 Subtype" }), "background");
    await chooseMenuOption(user, within(dialog).getByRole("combobox", { name: "参考资产状态" }), "已批准");
    expect(within(dialog).getByText("找到 1 项")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "只看已选" })).toHaveAttribute("aria-pressed", "false");
    expect(dialog.querySelectorAll("select")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog", { name: "选择 Agent 要读取的参考" })).not.toBeInTheDocument();
  });
});
