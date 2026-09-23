import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowCounterClockwise,
  Archive,
  ArrowsClockwise,
  Check,
  CheckCircle,
  ChatCircleDots,
  CaretDown,
  CaretRight,
  CircleNotch,
  DotsThree,
  FileArrowUp,
  Funnel,
  FloppyDisk,
  ImageSquare,
  Lightning,
  ListChecks,
  MagicWand,
  MagnifyingGlass,
  Minus,
  PaperPlaneRight,
  Plus,
  Question,
  Robot,
  ShieldWarning,
  SlidersHorizontal,
  Sparkle,
  Stop,
  Toolbox,
  Trash,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import {
  ApiError,
  recoverGenerationTurn,
  renameGenerationConversation,
  resolveGenerationProposal,
  answerGenerationInput,
  cancelGenerationConversationTurn,
  confirmGenerationConversation,
  createGenerationConversation,
  archiveGenerationConversation,
  deleteGenerationConversation,
  fetchGenerationConversation,
  fetchGenerationAgentCapabilities,
  fetchGenerationConversationEvents,
  fetchGenerationConversations,
  fetchGenerationProviders,
  fetchWorkbench,
  sendGenerationConversationMessage,
  steerGenerationConversationTurn,
  subscribeToGenerationConversationEvents,
  unarchiveGenerationConversation,
  updateGenerationConversationContext,
  updateGenerationConversationDraft,
} from "../lib/api";
import type {
  GenerationAssetProposal,
  GenerationConversation,
  GenerationConversationSummary,
  GenerationAgentCapabilities,
  GenerationProviderProfile,
  GenerationConversationEvent,
  GenerationPlanningDraft,
  GenerationInputRequest,
  GenerationReferenceProposal,
  GenerationTaskProposal,
  GameAsset,
  WorkbenchPayload,
} from "../types";
import type { ProviderDefaultRoute, ProviderModelModality } from "../types";
import { assetKindLabel, assetSubtypeLabel } from "../lib/labels";
import { useModalFocus } from "../hooks/useModalFocus";
import { MarkdownPreview } from "./MarkdownPreview";
import { GenerationInputCard } from "./GenerationInputCard";
import { useGenerationEvents } from "../lib/generationStore";
import { GenerationAssetPreview, ReferenceTile } from "./GenerationAssetPreview";
import { GenerationResults } from "./GenerationResults";
import "./GenerationWorkspace.css";
import { SelectMenu } from "./SelectMenu";

function textValue(value: unknown, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

const EMPTY_ASSET_IDS: string[] = [];

function numberValue(value: unknown, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function queryErrorMessage(value: unknown, fallback: string): string {
  if (value instanceof ApiError) {
    return value.hint ? `${value.message}（${value.hint}）` : value.message;
  }
  if (value instanceof Error && value.message) return value.message;
  return fallback;
}

function eventText(event: GenerationConversationEvent): string {
  const data = event.data ?? {};
  if (["turn.failed", "agent.unavailable"].includes(event.event_type)) {
    if (/paginated_threads|thread\/resume.*not supported/i.test(String(data.reason))) return "原会话的恢复协议不兼容。继续本轮会保留已回答问题和当前方案，重建规划上下文。";
    if (data.error_code === "process_restarted") return "服务重启中断了这次规划；对话与回答已保存，可以继续本轮。";
  }
  return textValue(data.content ?? data.message ?? data.reason ?? data.text);
}

export function compactConversationEvents(events: GenerationConversationEvent[]): GenerationConversationEvent[] {
  const streamKey = (event: GenerationConversationEvent) => `${event.turn_id}:${event.data.item_id ?? event.data.itemId ?? "legacy"}`;
  const finalMessageItems = new Set(events.filter(event => event.event_type === "assistant.message" && (event.data.item_id || event.data.itemId)).map(streamKey));
  const finalMessageTurns = new Set(events.filter(event => event.event_type === "assistant.message" && event.data.phase !== "commentary" && !event.data.item_id && !event.data.itemId).map(event => event.turn_id));
  const toolResults = new Set(events.filter((event) => event.event_type === "tool.result").map((event) => `${event.turn_id}:${textValue(event.data.itemId ?? event.data.item_id ?? event.data.tool)}`));
  const terminalErrorByTurn = new Map<string, string>();
  const activityCounts = new Map<string, number>();
  const activityFirst = new Map<string, GenerationConversationEvent>();
  const latestToolProgress = new Map<string, string>();
  const assistantDeltas = new Map<string, string>();
  const reasoningSummaries = new Map<string, string>();
  for (const event of events) {
    const turnKey = event.turn_id ?? event.id;
    if (event.event_type === "agent.unavailable" || event.event_type === "turn.failed") {
      const existing = terminalErrorByTurn.get(turnKey);
      if (!existing || event.event_type === "turn.failed") terminalErrorByTurn.set(turnKey, event.id);
    }
    if (event.event_type === "tool.started" || event.event_type === "tool.result" || event.event_type === "reasoning.summary") {
      const toolName = textValue(event.data.tool, event.event_type === "reasoning.summary" ? "reasoning" : "commandExecution");
      if (toolName === "reasoning" || toolName === "commandExecution") {
        activityCounts.set(turnKey, (activityCounts.get(turnKey) ?? 0) + 1);
        if (!activityFirst.has(turnKey)) activityFirst.set(turnKey, event);
      }
    }
    if (event.event_type === "assistant.delta" && !finalMessageTurns.has(event.turn_id) && !finalMessageItems.has(streamKey(event))) {
      assistantDeltas.set(streamKey(event), `${assistantDeltas.get(streamKey(event)) ?? ""}${eventText(event)}`);
    }
    if (event.event_type === "reasoning.summary") {
      reasoningSummaries.set(turnKey, `${reasoningSummaries.get(turnKey) ?? ""}${eventText(event)}`);
    }
    if (event.event_type === "tool.progress") {
      const toolKey = `${turnKey}:${textValue(event.data.itemId ?? event.data.item_id ?? event.data.tool)}`;
      latestToolProgress.set(toolKey, event.id);
    }
  }
  const emittedDeltas = new Set<string>();
  const emittedReasoning = new Set<string>();
  const seenFinalMessages = new Set<string>();
  return events.flatMap((event) => {
    if (event.event_type.startsWith("tool.") && event.data.tool === "userMessage") return [];
    if (event.event_type === "draft.updated" && event.data.source === "system") return [];
    if (event.event_type === "user.message" && event.data.input_request_answer === true) return [];
    const turnKey = event.turn_id ?? event.id;
    if (
      (event.event_type === "agent.unavailable" || event.event_type === "turn.failed")
      && terminalErrorByTurn.get(turnKey) !== event.id
    ) return [];
    if (event.event_type === "assistant.delta") {
      const key = streamKey(event);
      if (finalMessageTurns.has(event.turn_id) || finalMessageItems.has(key) || emittedDeltas.has(key)) return [];
      emittedDeltas.add(key);
      const raw = assistantDeltas.get(key) ?? eventText(event);
      // Structured draft JSON is progress, not user-facing prose.
      const content = /\{\s*"(?:message|draft_json)"/.test(raw) ? `方案输出已接收 ${raw.length.toLocaleString()} 字符；完整输出通过校验后会显示方案。` : raw;
      return [{ ...event, data: { ...event.data, content } }];
    }
    const activityName = textValue(event.data.tool, event.event_type === "reasoning.summary" ? "reasoning" : "commandExecution");
    if ((event.event_type === "tool.started" || event.event_type === "tool.result" || event.event_type === "reasoning.summary")
      && (activityName === "reasoning" || activityName === "commandExecution")) {
      const first = activityFirst.get(turnKey);
      if (!first || first.id !== event.id) return [];
      return [{ ...first, event_type: "tool.result", data: { ...first.data, tool: "agent.activity", summary: `已完成 ${activityCounts.get(turnKey) ?? 1} 项只读检索与思考` } }];
    }
    if (event.event_type === "assistant.message") {
      const key = `${turnKey}:${eventText(event)}`;
      if (seenFinalMessages.has(key)) return [];
      seenFinalMessages.add(key);
    }
    if (event.event_type === "reasoning.summary") {
      if (emittedReasoning.has(turnKey)) return [];
      emittedReasoning.add(turnKey);
      return [{ ...event, data: { ...event.data, content: reasoningSummaries.get(turnKey) ?? eventText(event) } }];
    }
    if (event.event_type === "tool.progress" || event.event_type === "tool.started") {
      const toolKey = `${turnKey}:${textValue(event.data.itemId ?? event.data.item_id ?? event.data.tool)}`;
      if (toolResults.has(toolKey)) return [];
      if (event.event_type === "tool.progress" && latestToolProgress.get(toolKey) !== event.id) return [];
    }
    return [event];
  });
}

function cloneDraft(draft: GenerationPlanningDraft): GenerationPlanningDraft {
  return JSON.parse(JSON.stringify(draft)) as GenerationPlanningDraft;
}

function defaultAssetProposal(asset: GameAsset): GenerationAssetProposal {
  return {
    mode: "existing",
    asset_id: asset.id,
    key: asset.key,
    kind: asset.kind,
    subtype: asset.subtype,
    title: asset.name,
    schema_ref: asset.schemaRef ?? null,
    tags: asset.tags,
    metadata: {},
  };
}

function makeTaskId(index: number) {
  return `produce-asset-${index + 1}`;
}

function draftTaskForAsset(asset: GameAsset, index: number): GenerationTaskProposal {
  const image = asset.kind === "media" || asset.kind === "production";
  return {
    id: makeTaskId(index),
    asset: defaultAssetProposal(asset),
    kind: image ? "image" : "text",
    prompt: image ? `生成 ${asset.name}，遵循项目风格圣经。` : `生成 ${asset.name} 的结构化内容。`,
    provider_profile_id: null,
    model: null,
    schema: null,
    depends_on: [],
    width: image ? 1024 : null,
    height: image ? 1024 : null,
    max_bytes: null,
    transparent: image,
    reference_task_id: null,
    references: [],
    target_path: null,
    candidate_path: null,
    locked_fields: [],
    metadata: {},
  };
}

function draftTaskForNewAsset(index: number): GenerationTaskProposal {
  return {
    id: makeTaskId(index),
    asset: {
      mode: "new",
      asset_id: null,
      key: `image.new-${index + 1}`,
      kind: "media",
      subtype: "illustration",
      title: "待创建资源",
      schema_ref: null,
      tags: [],
      metadata: {},
    },
    kind: "image",
    prompt: "描述要生成的资源，并说明用途与风格。",
    provider_profile_id: null,
    model: null,
    schema: null,
    depends_on: [],
    width: 1024,
    height: 1024,
    max_bytes: null,
    transparent: false,
    reference_task_id: null,
    references: [],
    target_path: null,
    candidate_path: null,
    locked_fields: [],
    metadata: {},
  };
}

interface RouteOption {
  providerId: string;
  providerName: string;
  model: string;
  locked: boolean;
}

function providerModels(provider: GenerationProviderProfile, modality: ProviderModelModality): string[] {
  const catalog = provider.models.filter((model) => (
    model.available !== false && model.enabled !== false && model.modalities.includes(modality)
  ));
  const fallback = modality === "text" ? provider.text_model : provider.image_model;
  return [...new Set(catalog.length > 0 ? catalog.map((model) => model.id) : (fallback ? [fallback] : []))];
}

function routeOptions(providers: GenerationProviderProfile[], modality: ProviderModelModality): RouteOption[] {
  const values: RouteOption[] = [];
  for (const provider of providers) {
    if (!provider.is_active) continue;
    for (const model of providerModels(provider, modality)) {
      values.push({
        providerId: provider.id,
        providerName: provider.name,
        model,
        locked: !provider.is_unlocked,
      });
    }
  }
  return values;
}

function routeLabel(
  route: ProviderDefaultRoute | null | undefined,
  providers: GenerationProviderProfile[],
  fallback = "沿用系统默认",
) {
  if (!route?.provider_profile_id || !route.model) return fallback;
  const provider = providers.find((item) => item.id === route.provider_profile_id);
  return provider ? `${provider.name} · ${route.model}` : `${route.provider_profile_id} · ${route.model}`;
}

export interface GenerationChatPageProps {
  /** The center owns the selected session; null renders the session launcher. */
  sessionId?: string | null;
  seedAssetIds?: string[];
  createOnMount?: boolean;
  open?: boolean;
  onClose?: () => void;
  onNewSession?: () => void;
  onSelectSession?: (sessionId: string) => void;
  onSessionCreated?: (sessionId: string) => void;
  onConfirmed?: (planId: string) => void;
  onOpenAsset?: (assetId: string) => void;
  onSessionRemoved?: (sessionId: string) => void;
  workbenchData?: WorkbenchPayload;
}

export function GenerationChatPage({
  sessionId: suppliedSessionId = null,
  seedAssetIds: suppliedSeedIds,
  createOnMount = false,
  open = true,
  onClose,
  onNewSession,
  onSelectSession,
  onSessionCreated,
  onConfirmed,
  onOpenAsset,
  onSessionRemoved,
  workbenchData,
}: GenerationChatPageProps = {}) {
  const activeSessionId = suppliedSessionId;
  const seedAssetIds = suppliedSeedIds ?? EMPTY_ASSET_IDS;
  const workbenchQuery = useQuery<WorkbenchPayload>({
    queryKey: ["workbench"],
    queryFn: fetchWorkbench,
    enabled: !workbenchData,
    initialData: workbenchData,
  });
  const [conversation, setConversation] = useState<GenerationConversation | null>(null);
  const [draft, setDraft] = useState<GenerationPlanningDraft | null>(null);
  const stream = useGenerationEvents(activeSessionId);
  const events = stream.events;
  const allVisibleEvents = useMemo(()=>compactConversationEvents(events),[events]);

  const connected = stream.connected;
  const [inputRequests, setInputRequests] = useState<Record<string, GenerationInputRequest>>({});
  const [input, setInput] = useState("");
  const inputOwner = useRef<string | null | undefined>(undefined);
  const sendBusy = useRef(false);
  const messageKey = useRef<{content:string;id:string}|null>(null);
  const recoverKeys = useRef(new Map<string,string>());
  const [sessionSearch, setSessionSearch] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [workTab, setWorkTab] = useState<"plan"|"references"|"results">("plan");
  const [previewAsset, setPreviewAsset] = useState<GameAsset|null>(null);
  const [taskPickerOpen, setTaskPickerOpen] = useState(false);
  const [taskSearch, setTaskSearch] = useState("");
  const taskDialogRef=useRef<HTMLElement>(null);
  const renameDialogRef=useRef<HTMLElement>(null);
  useModalFocus({open:taskPickerOpen,dialogRef:taskDialogRef,initialFocusRef:taskDialogRef,onClose:()=>setTaskPickerOpen(false)});
  useModalFocus({open:!!renaming,dialogRef:renameDialogRef,initialFocusRef:renameDialogRef,onClose:()=>setRenaming(null)});
  const [historyLimit, setHistoryLimit] = useState(60);
  const [newMessages, setNewMessages] = useState(false);
  const chatScroll = useRef<HTMLDivElement>(null);
  const followBottom = useRef(true);
  const restoreScroll = useRef<number|null>(null);
  const lifecycleCursor = useRef(0);
  useEffect(()=>{
    inputOwner.current = activeSessionId;
    setInput(activeSessionId ? sessionStorage.getItem(`gams.composer.${activeSessionId}`) ?? "" : "");
    try { messageKey.current = JSON.parse(sessionStorage.getItem(`gams.operation.${activeSessionId}`) ?? "null"); } catch { messageKey.current = null; }
    lifecycleCursor.current = 0;
    setHistoryLimit(60);
    const savedScroll=activeSessionId ? sessionStorage.getItem(`gams.scroll.${activeSessionId}`) : null;
    restoreScroll.current=savedScroll===null?null:Number(savedScroll);
    followBottom.current=savedScroll===null;
  },[activeSessionId]);
  const changeInput = (value:string) => {
    setInput(value);
    if (activeSessionId) sessionStorage.setItem(`gams.composer.${activeSessionId}`,value);
  };

  const [sending, setSending] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draftSaveFailed, setDraftSaveFailed] = useState(false);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmChecked, setConfirmChecked] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [acceptedWarningCodes, setAcceptedWarningCodes] = useState<string[]>([]);
  const [manualMode, setManualMode] = useState(false);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [planCollapsed, setPlanCollapsed] = useState(()=>window.innerWidth < 1200);
  useEffect(()=>{
    const onResize=()=>{ if(window.innerWidth<1200)setPlanCollapsed(true); };
    window.addEventListener("resize",onResize);
    return ()=>window.removeEventListener("resize",onResize);
  },[]);
  const [panelWidth,setPanelWidth] = useState(400);
  const [activePanel, setActivePanel] = useState<"context" | "chat" | "plan">("chat");
  const [selectedContextIds, setSelectedContextIds] = useState<string[]>(seedAssetIds);
  const [toast, setToast] = useState("");
  const [error, setError] = useState("");
  const [expandedTools, setExpandedTools] = useState<Record<string, boolean>>({});
  const createdRef = useRef(false);
  const eventCursor = useRef(0);
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  const conversationRef = useRef<GenerationConversation | null>(null);
  const pendingDraftRef = useRef<GenerationPlanningDraft | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const saveInFlightRef = useRef<Promise<void> | null>(null);
  const flushRunningRef = useRef<Promise<void> | null>(null);
  const workbench = workbenchQuery.data;
  const assets = workbench?.assets ?? [];

  useEffect(() => {
    conversationRef.current = conversation;
  }, [conversation]);

  useEffect(() => {

    setInputRequests({});
    eventCursor.current = 0;

    setSending(false);
    setError("");
    setConversation(null);
    setDraft(null);
    setSelectedContextIds(seedAssetIds);
    createdRef.current = false;
  }, [activeSessionId, createOnMount]);

  const existingConversationQuery = useQuery<GenerationConversation>({
    queryKey: ["generation-conversation", activeSessionId],
    queryFn: () => fetchGenerationConversation(activeSessionId as string),
    enabled: Boolean(activeSessionId),
    staleTime: 0,
    refetchInterval: 3000,
  });

  const recentQuery = useQuery<GenerationConversationSummary[]>({
    queryKey: ["generation-conversations", workbench?.project.id],
    queryFn: () => fetchGenerationConversations(workbench?.project.id),
    enabled: Boolean(workbench?.project.id),
    staleTime: 10_000,
    refetchInterval: 5000,
  });
  const capabilitiesQuery = useQuery<GenerationAgentCapabilities>({
    queryKey: ["generation-agent-capabilities"],
    queryFn: fetchGenerationAgentCapabilities,
    staleTime: 30_000,
    retry: false,
  });
  const providersQuery = useQuery<GenerationProviderProfile[]>({
    queryKey: ["generation-providers", workbench?.project.id],
    queryFn: fetchGenerationProviders,
    enabled: Boolean(workbench?.project.id),
    staleTime: 30_000,
    retry: false,
  });
  const [referencePickerOpen, setReferencePickerOpen] = useState(false);
  const [referenceSaving, setReferenceSaving] = useState(false);
  const [referenceSearch, setReferenceSearch] = useState("");
  const [recentMenuId, setRecentMenuId] = useState<string | null>(null);
  const [archivedOpen, setArchivedOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<GenerationConversationSummary | null>(null);
  const [lifecycleBusy, setLifecycleBusy] = useState<string | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const archivedQuery = useQuery<GenerationConversationSummary[]>({
    queryKey: ["generation-conversations-archived", workbench?.project.id],
    queryFn: () => fetchGenerationConversations(workbench?.project.id, true),
    enabled: Boolean(workbench?.project.id && archivedOpen),
    staleTime: 5_000,
  });

  useEffect(() => {
    if (!createOnMount || activeSessionId || !workbench?.project.id || createdRef.current) return;
    createdRef.current = true;
    void createGenerationConversation(workbench.project.id, seedAssetIds)
      .then((value) => {
        setConversation(value);
        setDraft(value.draft);
        setDraftSaveFailed(false);
        setSelectedContextIds(seedAssetIds);
        void recentQuery.refetch();
        onSessionCreated?.(value.id);
      })
      .catch((reason: unknown) => {
        createdRef.current = false;
        setError(reason instanceof Error ? reason.message : "无法创建生成会话。");
      });
  }, [activeSessionId, createOnMount, onSessionCreated, recentQuery, seedAssetIds, workbench?.project.id]);

  useEffect(() => {
    const value = existingConversationQuery.data;
    if (!value) return;
    if (value.last_sequence !== undefined && value.last_sequence < lifecycleCursor.current && conversationRef.current) return;
    lifecycleCursor.current = value.last_sequence ?? lifecycleCursor.current;
    setConversation(value);
    if (value.pending_input) setInputRequests((current) => ({ ...current, [value.pending_input!.id]: value.pending_input! }));
    if (!pendingDraftRef.current && !saveInFlightRef.current) setDraft(value.draft);
    setSending(value.status === "running");
    setDraftSaveFailed(false);
    setManualMode(value.status === "unavailable" || value.status === "failed");
    setSelectedContextIds(value.seed_asset_ids ?? (value.context.seed_asset_ids as string[] | undefined) ?? seedAssetIds);
  }, [existingConversationQuery.data, seedAssetIds]);

  useEffect(() => {
    const value = capabilitiesQuery.data;
    if (value && !value.available) setManualMode(true);
  }, [capabilitiesQuery.data]);

  useEffect(() => {
    const fresh = events.filter(event=>event.sequence > lifecycleCursor.current);
    for (const event of fresh) {
      lifecycleCursor.current = event.sequence;
      if (event.event_type === "user_input.requested" && typeof event.data.id === "string") {
        const item = event.data as unknown as GenerationInputRequest;
        setInputRequests(current=>({...current,[item.id]:item}));
        setConversation(current=>current ? {...current,status:"awaiting_input",pending_input:item} : current);
        setSending(false);
      } else if (["user_input.resolved","user_input.cancelled","user_input.fallback"].includes(event.event_type) && typeof event.data.id === "string") {
        const item=event.data as unknown as GenerationInputRequest;
        setInputRequests(current=>({...current,[item.id]:item}));
      } else if (["turn.started","turn.recovering"].includes(event.event_type)) {
        setSending(true); setManualMode(false);
        setConversation(current=>current ? {...current,status:"running"} : current);
      } else if (["turn.failed","turn.completed","agent.unavailable"].includes(event.event_type)) {
        setSending(false);
        setConversation(current=>current ? {...current,status:"awaiting_user"} : current);
      }
      if (event.event_type === "draft.updated" && event.data.draft && !pendingDraftRef.current && !saveInFlightRef.current) {
        setDraft(event.data.draft as GenerationPlanningDraft);
        setConversation(current=>current ? {...current,draft_hash:textValue(event.data.draft_hash,current.draft_hash ?? ""),draft_version:numberValue(event.data.draft_version,current.draft_version)} : current);
      }
    }
    // Hydrate answered cards even when the server snapshot is newer than history.
    const requests:Record<string,GenerationInputRequest>={};
    for (const event of events) if (event.event_type.startsWith("user_input.") && typeof event.data.id === "string") requests[event.data.id]=event.data as unknown as GenerationInputRequest;
    setInputRequests(current=>({...current,...requests}));
  },[events,conversation?.id]);
  useEffect(()=>{
    if (restoreScroll.current!==null && chatScroll.current && events.length) {
      chatScroll.current.scrollTop=restoreScroll.current;restoreScroll.current=null;
    } else if (followBottom.current) chatEndRef.current?.scrollIntoView?.({block:"end"});
    else setNewMessages(true);
  },[events]);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(""), 3_200);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  useEffect(() => {
    if (!previewOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.defaultPrevented && event.key === "Escape") setPreviewOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [previewOpen]);

  useEffect(() => {
    if (!open || workbenchQuery.isLoading) return;
    closeButtonRef.current?.focus();
  }, [open, workbenchQuery.isLoading]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== "Escape") return;
      if (document.querySelector('[data-nested-modal="true"]')) return;
      if (previewOpen) {
        setPreviewOpen(false);
        return;
      }
      if (referencePickerOpen) {
        setReferencePickerOpen(false);
        return;
      }
      if (archivedOpen) {
        setArchivedOpen(false);
        return;
      }
      if (deleteTarget) {
        setDeleteTarget(null);
        return;
      }
      onClose?.();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [archivedOpen, deleteTarget, onClose, open, previewOpen, referencePickerOpen]);

  useEffect(() => () => {
    if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current);
  }, []);

  const currentDraft = draft ?? conversation?.draft ?? null;
  const pendingInput = Object.values(inputRequests).find((item) => item.status === "pending") ?? conversation?.pending_input ?? null;
  const hasPendingInput = Boolean(pendingInput?.status === "pending");
  const currentHash = conversation?.draft_hash ?? null;
  const taskErrors = useMemo(() => {
    if (!currentDraft) return [];
    const errors: string[] = [];
    const ids = new Set<string>();
    const targets = new Map<string, string>();
    for (const task of currentDraft.tasks) {
      if (!task.id.trim()) errors.push("存在没有任务 ID 的任务。");
      if (ids.has(task.id)) errors.push(`任务 ID 重复：${task.id}`);
      ids.add(task.id);
      if (!task.prompt.trim()) errors.push(`任务 ${task.id || "未命名"} 的 Prompt 不能为空。`);
      if (task.asset.mode === "new" && (!task.asset.key || !task.asset.kind || !task.asset.subtype || !task.asset.title)) {
        errors.push(`任务 ${task.id || "未命名"} 的新资产信息不完整。`);
      }
      const selectedAsset = assets.find((asset) => asset.id === task.asset.asset_id);
      if (task.kind === "text" && !task.schema && !task.asset.schema_ref && !selectedAsset?.schemaRef) {
        errors.push(`任务 ${task.id || "未命名"} 缺少项目 JSON Schema。`);
      }
      if (task.kind !== "text" && (!task.width || !task.height)) errors.push(`任务 ${task.id || "未命名"} 需要尺寸。`);
      if (task.kind === "image_edit" && !task.reference_task_id && task.references.filter((reference) => reference.role === "primary").length !== 1) {
        errors.push(`任务 ${task.id || "未命名"} 需要一项主参考图或上游参考任务。`);
      }
      if (!task.target_path) errors.push(`任务 ${task.id || "未命名"} 缺少批准后落地路径。`);
      if (task.target_path?.startsWith("/") || task.target_path?.split("/").includes("..")) errors.push(`任务 ${task.id || "未命名"} 的落地路径不安全。`);
      if (task.target_path) {
        const normalized = task.target_path.replaceAll("\\", "/").toLowerCase();
        const owner = targets.get(normalized);
        if (owner) errors.push(`任务 ${owner} 与 ${task.id || "未命名"} 使用了相同落地路径。`);
        else targets.set(normalized, task.id || "未命名");
      }
      for (const dependency of task.depends_on) {
        if (!currentDraft.tasks.some((candidate) => candidate.id === dependency)) errors.push(`任务 ${task.id || "未命名"} 的依赖 ${dependency} 不存在。`);
      }
    }
    return errors;
  }, [assets, currentDraft]);
  const unresolvedQuestions = currentDraft?.questions ?? [];
  const warnings = currentDraft?.warnings ?? [];
  const warningCodes = warnings.map((item) => textValue(item.code)).filter(Boolean);
  useEffect(() => {
    setAcceptedWarningCodes((current) => current.filter((code) => warningCodes.includes(code)));
  }, [warningCodes.join("|")]);
  const canConfirm = Boolean(
    conversation && currentDraft && currentDraft.tasks.length > 0 && currentHash && confirmChecked &&
    unresolvedQuestions.length === 0 && taskErrors.length === 0 && !confirmBusy && !saving &&
    !draftSaveFailed &&
    warningCodes.every((code) => acceptedWarningCodes.includes(code)) &&
    conversation.status !== "completed" && !hasPendingInput && conversation.status !== "running",
  );
  const canPreview = Boolean(
    conversation && currentDraft && currentDraft.tasks.length > 0 && !confirmBusy && !saving &&
    !draftSaveFailed &&
    conversation.status !== "completed" && !hasPendingInput && conversation.status !== "running",
  );

  const flushDraftSave = useCallback(() => {
    if (flushRunningRef.current) return flushRunningRef.current;
    const run = (async () => {
      while (true) {
        if (saveTimerRef.current !== null) {
          window.clearTimeout(saveTimerRef.current);
          saveTimerRef.current = null;
        }
        const next = pendingDraftRef.current;
        pendingDraftRef.current = null;
        const currentConversation = conversationRef.current;
        if (!next || !currentConversation?.draft_hash || currentConversation.status === "completed") break;
        setError("");
        let failed = false;
        const requestPromise = (async () => {
          try {
            const saved = await updateGenerationConversationDraft(currentConversation.id, currentConversation.draft_hash as string, next);
            conversationRef.current = saved;
            setConversation(saved);
            // A newer local edit may have arrived while this request was in
            // flight. Keep that edit visible until its own save completes.
            if (!pendingDraftRef.current) setDraft(saved.draft);
            setDraftSaveFailed(false);
            setToast("方案修改已保存。");
          } catch (reason: unknown) {
            failed = true;
            // Preserve the exact draft the user can still see. Confirmation is
            // blocked until this pending value is saved successfully.
            if (!pendingDraftRef.current) pendingDraftRef.current = next;
            setDraftSaveFailed(true);
            setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : "方案保存失败。");
          }
        })();
        saveInFlightRef.current = requestPromise;
        await requestPromise;
        saveInFlightRef.current = null;
        if (failed || !pendingDraftRef.current) break;
      }
      setSaving(false);
    })();
    flushRunningRef.current = run;
    void run.finally(() => {
      if (flushRunningRef.current === run) flushRunningRef.current = null;
    }).catch(() => undefined);
    return run;
  }, []);

  const scheduleDraftSave = useCallback((next: GenerationPlanningDraft) => {
    pendingDraftRef.current = next;
    setDraftSaveFailed(false);
    setSaving(true);
    if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => {
      saveTimerRef.current = null;
      void flushDraftSave();
    }, 280);
  }, [flushDraftSave]);

  const refreshAndKeepDraft = async () => {
    const pending=pendingDraftRef.current;
    const current=conversationRef.current;
    if (!pending || !current) return;
    try {
      const refreshed=await updateGenerationConversationContext(current.id,selectedContextIds);
      conversationRef.current=refreshed;
      setConversation(refreshed);
      pendingDraftRef.current={...pending,context_hash:refreshed.context_hash};
      setSaving(true);
      await flushDraftSave();
    } catch(reason) { setError(queryErrorMessage(reason,"刷新失败，编辑仍保留。")); }
  };
  const retryDraftSave = useCallback(() => {
    if (!pendingDraftRef.current || saving) return;
    setDraftSaveFailed(false);
    setSaving(true);
    // Let the previous flush promise clear its bookkeeping before starting a
    // deliberate retry from the error state.
    window.setTimeout(() => void flushDraftSave(), 0);
  }, [flushDraftSave, saving]);

  const editAndSave = useCallback((updater: (value: GenerationPlanningDraft) => void) => {
    if (!currentDraft) return;
    const next = cloneDraft(currentDraft);
    updater(next);
    setDraft(next);
    scheduleDraftSave(next);
  }, [currentDraft, scheduleDraftSave]);

  const sendMessage = async () => {
    const content=input.trim();
    if (!content || !conversation || hasPendingInput || sendBusy.current) return;
    sendBusy.current=true;
    const id=conversation.id;
    messageKey.current = messageKey.current?.content===content ? messageKey.current : {content,id:crypto.randomUUID()};
    sessionStorage.setItem(`gams.operation.${id}`,JSON.stringify(messageKey.current));
    setError("");
    try {
      if (sending || conversation.status === "running") {
        await steerGenerationConversationTurn(id, content, messageKey.current.id);
      } else {
        setSending(true);
        await sendGenerationConversationMessage(id, content, selectedContextIds, messageKey.current.id);
      }
      if (inputOwner.current === id) {
        changeInput(""); messageKey.current=null; sessionStorage.removeItem(`gams.operation.${id}`);
        const latest=await fetchGenerationConversation(id);
        if (inputOwner.current === id) { setConversation(latest); setDraft(latest.draft); }
        setToast("指令已接受");
      }
    } catch (reason) {
      if (inputOwner.current===id) { setSending(false);setError(reason instanceof Error ? reason.message : "发送失败，原文已保留。"); }
    } finally { sendBusy.current=false; }
  };
  const recover = async (turnId:string) => {
    if (!conversation || sendBusy.current) return;
    sendBusy.current=true;
    const key=recoverKeys.current.get(turnId) ?? crypto.randomUUID();
    recoverKeys.current.set(turnId,key);
    try {
      await recoverGenerationTurn(conversation.id,turnId,key);
      setSending(true);setManualMode(false);
      void existingConversationQuery.refetch();
    } catch(reason) { setError(reason instanceof Error ? reason.message : "恢复失败，现场已保留。"); }
    finally {sendBusy.current=false;}
  };

  const answerInput = async (item: GenerationInputRequest, answers: Record<string, { answers: string[] }>, clientResponseId: string) => {
    if (!conversation) return;
    await answerGenerationInput(conversation.id, item.id, clientResponseId, answers);
    setInputRequests((current) => ({ ...current, [item.id]: { ...item, status: "resolved", answers } }));
    setConversation((current) => current ? { ...current, pending_input: null, status: "running" } : current);
    setSending(true);
  };

  const stopTurn = async () => {
    if (!conversation || (!sending && conversation.status !== "awaiting_input")) return;
    try {
      const stopped = await cancelGenerationConversationTurn(conversation.id);
      setConversation(stopped);
      setSending(false);
      setToast("已停止当前回合，可以继续编辑方案。");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "停止回合失败。");
    }
  };

  const confirm = async () => {
    if (!conversation || !currentHash || !canConfirm) return;
    setConfirmBusy(true);
    setError("");
    try {
      await flushDraftSave();
      const latest = conversationRef.current ?? conversation;
      const latestHash = latest.draft_hash;
      if (!latestHash) throw new Error("方案尚未保存完成。");
      let result;
      try {
        result = await confirmGenerationConversation(latest.id, latestHash, acceptedWarningCodes, latest.context_hash);
      } catch (reason: unknown) {
        const contextStale = reason instanceof ApiError && reason.status === 409 && /context|上下文/i.test(reason.message);
        if (!contextStale) throw reason;
        const refreshed = await updateGenerationConversationContext(latest.id, selectedContextIds);
        conversationRef.current = refreshed;
        setConversation(refreshed);
        setDraft(refreshed.draft);
        if (!refreshed.draft_hash) throw new Error("上下文已刷新，但方案尚未保存完成。");
        setConfirmChecked(false);
        throw new Error("项目快照已刷新，编辑内容已保留。请重新核对当前预览并勾选确认后执行。");
      }
      setConversation(result.conversation);
      setToast("已确认，执行进度与结果会保留在当前会话。");
      setPreviewOpen(false);setConfirmChecked(false);setWorkTab("results");
      onConfirmed?.(result.plan.id);
    } catch (reason: unknown) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : "确认执行失败。");
    } finally {
      setConfirmBusy(false);
    }
  };

  const updateReferenceContext = async (nextIds: string[]) => {
    if (!conversation || referenceSaving || conversation.plan_id) return;
    const normalized = [...new Set(nextIds)];
    const previous = selectedContextIds;
    setSelectedContextIds(normalized);
    setReferenceSaving(true);
    setError("");
    try {
      const saved = await updateGenerationConversationContext(conversation.id, normalized);
      conversationRef.current = saved;
      setConversation(saved);
      setDraft(saved.draft);
      setDraftSaveFailed(false);
      setReferencePickerOpen(false);
      setReferenceSearch("");
      setToast("参考资产上下文已更新。它只会影响 Agent 读取范围。");
    } catch (reason: unknown) {
      setSelectedContextIds(previous);
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : "参考资产保存失败。");
    } finally {
      setReferenceSaving(false);
    }
  };

  const refreshConversationLists = () => {
    void recentQuery.refetch();
    if (archivedOpen) void archivedQuery.refetch();
  };

  const archiveSession = async (item: GenerationConversationSummary) => {
    if (lifecycleBusy || item.status === "running" || item.status === "awaiting_input") return;
    setLifecycleBusy(item.id);
    setRecentMenuId(null);
    try {
      await archiveGenerationConversation(item.id);
      refreshConversationLists();
      setToast("会话已归档，可从“已归档会话”恢复。");
      if (item.id === conversation?.id) onSessionRemoved?.(item.id);
    } catch (reason: unknown) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : "会话归档失败。");
    } finally {
      setLifecycleBusy(null);
    }
  };

  const restoreSession = async (item: GenerationConversationSummary) => {
    if (lifecycleBusy) return;
    setLifecycleBusy(item.id);
    try {
      await unarchiveGenerationConversation(item.id);
      refreshConversationLists();
      setToast("会话已恢复到最近会话。");
    } catch (reason: unknown) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : "会话恢复失败。");
    } finally {
      setLifecycleBusy(null);
    }
  };

  const permanentlyDeleteSession = async () => {
    const item = deleteTarget;
    if (!item || lifecycleBusy) return;
    setLifecycleBusy(item.id);
    try {
      await deleteGenerationConversation(item.id);
      setDeleteTarget(null);
      refreshConversationLists();
      setToast("会话聊天记录已永久删除；已确认的计划、任务和资产未受影响。");
      if (item.id === conversation?.id) onSessionRemoved?.(item.id);
    } catch (reason: unknown) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : "会话删除失败。");
    } finally {
      setLifecycleBusy(null);
    }
  };

  const addTaskFromAsset = (asset: GameAsset) => {
    editAndSave((value) => {
      value.tasks.push(draftTaskForAsset(asset, value.tasks.length));
      value.questions = value.questions.filter((question) => !question.includes("至少"));
    });
  };

  const updateSessionRoute = (modality: ProviderModelModality, route: ProviderDefaultRoute | null) => {
    editAndSave((value) => {
      value.settings.route_defaults = {
        ...(value.settings.route_defaults ?? {}),
        [modality]: route,
      };
    });
  };

  const blockingQueryError = workbenchQuery.error ?? (activeSessionId ? existingConversationQuery.error : null);
  if (blockingQueryError || (!conversation && error)) {
    const message = blockingQueryError
      ? queryErrorMessage(blockingQueryError, "后台服务没有返回可用的项目状态。")
      : error;
    return (
      <div className="generation-chat-error-screen" role="alert">
        <div className="generation-chat-error-mark"><WarningCircle size={25} weight="duotone" /></div>
        <span className="generation-kicker">AGENT WORKBENCH / CONNECTION</span>
        <h1>生成工作台暂时无法打开</h1>
        <p>{message}</p>
        <div className="generation-chat-error-actions">
          <button
            className="button primary"
            type="button"
            onClick={() => {
              setError("");
              void workbenchQuery.refetch();
              if (activeSessionId) void existingConversationQuery.refetch();
            }}
          >
            <ArrowsClockwise size={16} /> 重试
          </button>
          <button className="button secondary" type="button" onClick={onClose}>
            <ArrowLeft size={16} /> 收起生成中心
          </button>
        </div>
        <small>项目资产不会因这次连接失败而改变。恢复服务后可继续打开已有会话。</small>
      </div>
    );
  }

  if (workbenchQuery.isLoading || (!conversation && (activeSessionId ? existingConversationQuery.isLoading : createOnMount))) {
    return <div className="generation-chat-loading" role="status"><CircleNotch size={22} className="spin" /> 正在打开生成工作台…</div>;
  }

  const projectName = workbench?.project.name ?? "当前项目";
  const selectedAssets = assets.filter((asset) => selectedContextIds.includes(asset.id));
  const recent = recentQuery.data ?? [];
  const archived = (archivedQuery.data ?? []).filter((item) => Boolean(item.archived_at));
  const providers = providersQuery.data ?? [];
  const routeDefaults = currentDraft?.settings.route_defaults ?? {};
  const historyEnd = Math.max(0, allVisibleEvents.length - historyLimit + 60);
  const visibleEvents = allVisibleEvents.slice(Math.max(0, historyEnd - 60), historyEnd);
  const routeEditingDisabled = !conversation || sending || hasPendingInput || conversation.status === "running" || saving || conversation.status === "completed";

  return (
    <div
      style={{"--generation-panel-width": `${panelWidth}px`} as React.CSSProperties}
      className={`generation-chat-page generation-drawer-page ${open ? "open" : "closed"} ${leftCollapsed ? "left-collapsed" : ""} ${planCollapsed ? "plan-collapsed" : ""} panel-${activePanel}`}
    >
      <header className="generation-chat-topbar">
        <button ref={closeButtonRef} className="generation-close-button" type="button" onClick={onClose} aria-label="收起生成中心"><X size={18} /> <span>生成中心</span></button>
        <div className="generation-title-lockup"><span className="generation-kicker">AGENT WORKBENCH / 生成中心</span><h1>{conversation?.title ?? currentDraft?.title ?? "生成中心"}</h1></div>
        <div className="generation-topbar-meta"><button type="button" className="button secondary" onClick={()=>setPlanCollapsed(x=>!x)}>工作面板</button><span className={`generation-connection ${connected ? "online" : ""}`}><i />{connected ? "事件流已连接" : "正在连接事件流"}</span><span className="generation-planning-route" title="规划与文字产物使用下方文字路由；运行记录和凭据与个人 Codex 隔离">规划 · {routeLabel(routeDefaults.text, providers)}</span><span className="generation-project-name">{projectName}</span></div>
      </header>

      <nav className="generation-mobile-tabs" aria-label="生成中心面板">
        <button type="button" className={activePanel === "context" ? "active" : ""} onClick={() => setActivePanel("context")}><ChatCircleDots size={14} /> 会话</button>
        <button type="button" className={activePanel === "chat" ? "active" : ""} onClick={() => {setActivePanel("chat");if(window.innerWidth<900)setPlanCollapsed(true);}}><Sparkle size={14} /> 对话</button>
        <button type="button" className={activePanel === "plan" ? "active" : ""} onClick={() => {setActivePanel("plan");setPlanCollapsed(false);}}><ListChecks size={14} /> 方案</button>
      </nav>

      <div className="generation-chat-body">
        <aside className="generation-context-rail" aria-label="会话列表">
          <div className="generation-sessions-head"><h2>会话</h2><button className="button secondary" type="button" onClick={onNewSession} aria-label="新建会话"><Plus size={16}/> 新建</button></div>
          <label className="generation-session-search"><MagnifyingGlass size={16}/><input aria-label="搜索会话" placeholder="搜索会话" value={sessionSearch} onChange={e=>setSessionSearch(e.target.value)}/></label>
          <div className="generation-sessions-list">{recent.filter(item=>(item.title??"").includes(sessionSearch)).map(item=><div key={item.id} className={`generation-recent-row ${item.id===conversation?.id ? "active":""}`}>
            <button className="generation-recent-item" type="button" onClick={()=>onSelectSession?.(item.id)}><span>{item.title??"新会话"}</span><small>{item.status==="running" ? "规划中" : item.status==="awaiting_input" ? "待回答" : item.status==="unavailable" ? "需要恢复" : item.plan_id ? "查看结果" : "可继续"} · {new Date(item.updated_at).toLocaleDateString("zh-CN")}</small></button>
            <button className="icon-button" type="button" aria-label={`管理会话 ${item.title??"新会话"}`} onClick={()=>setRecentMenuId(recentMenuId===item.id?null:item.id)}><DotsThree size={18}/></button>
            {recentMenuId===item.id && <div className="generation-recent-menu"><button type="button" onClick={()=>{setRenaming(item.id);setRenameText(item.title??"");setRecentMenuId(null);}}>重命名</button><button type="button" disabled={item.status==="running"||item.status==="awaiting_input"} onClick={()=>void archiveSession(item)}>归档</button></div>}
          </div>)}</div>
          <button type="button" className="generation-archive-link" onClick={()=>setArchivedOpen(true)}><Archive size={16}/> 已归档会话</button>
        </aside>

        <main className="generation-conversation-column">
          {conversation ? <>
          <div className="generation-conversation-scroll" ref={chatScroll} onScroll={()=>{const el=chatScroll.current;if(el) {followBottom.current=el.scrollHeight-el.scrollTop-el.clientHeight<100;if(activeSessionId)sessionStorage.setItem(`gams.scroll.${activeSessionId}`,String(el.scrollTop));}}}>
            {allVisibleEvents.length>historyLimit && <button className="button secondary" type="button" onClick={()=>{followBottom.current=false;setHistoryLimit(n=>n+60);}}>加载更早的对话</button>}
            {historyLimit > 60 && <button className="button secondary" type="button" onClick={()=>setHistoryLimit(n=>Math.max(60,n-60))}>较新的对话</button>}
            {events.length < 3 && <section className="generation-intro-card"><div className="generation-intro-mark"><MagicWand size={22} weight="duotone" /></div><div><span className="generation-kicker">规划回合 {conversation?.turn_count ?? 0}</span><h2>把目标说清楚，方案会自己长出来。</h2><p>Agent 只读取项目事实和固定参考，规划会使用所选文字供应商；资产生成须另行确认。</p></div><span className={`generation-mode-badge ${manualMode ? "manual" : "agent"}`}>{manualMode ? <><SlidersHorizontal size={14} /> 人工编排</> : <><Robot size={14} /> Agent 规划</>}</span></section>}
            {manualMode && !capabilitiesQuery.data?.available && <div className="generation-manual-banner" role="status"><ShieldWarning size={18} weight="fill" /><div><strong>Agent 暂不可用，方案编辑仍可继续。</strong><span>{capabilitiesQuery.data?.diagnostic?.hint || "检查本机 Codex 是否安装并登录；也可以在右侧手动补齐资源、参考图、渠道和落地路径。"}</span></div><button type="button" onClick={() => setManualMode(false)}>继续人工编排</button></div>}
            {visibleEvents.map((event) => {
              const isUser = event.event_type === "user.message";
              const isTool = event.event_type === "tool.started" || event.event_type === "tool.progress" || event.event_type === "tool.result";
              const isErrorEvent = event.event_type === "turn.failed" || event.event_type === "agent.unavailable";
              if (event.event_type === "user_input.requested") {
                const requestId = textValue(event.data.id);
                const item = inputRequests[requestId] ?? (conversation.pending_input?.id === requestId ? conversation.pending_input : null);
                if (pendingInput?.id === requestId) return null;
                return item ? <GenerationInputCard key={requestId} request={item} onAnswer={answerInput} /> : null;
              }
              if (["user_input.resolved", "user_input.fallback", "user_input.cancelled"].includes(event.event_type)) return null;
              if (event.event_type === "draft.updated" && event.data.source === "context.refresh") return null;
              if (event.event_type === "draft.updated") {
                return <article className="generation-draft-event" key={event.id}><CheckCircle size={15} /><span><strong>方案已更新</strong><small>{event.data.source === "context.refresh" ? "项目上下文发生变化，已刷新草案哈希" : `草案版本 ${event.data.draft_version == null ? "—" : String(event.data.draft_version)}`}</small></span></article>;
              }
              if (isTool) {
                const expanded = expandedTools[event.id] === true;
                const toolName = textValue(event.data.tool, "项目只读检索");
                const activitySummary = textValue(event.data.summary);
                return <article className={`generation-event-card tool-event ${event.event_type === "tool.result" ? "result" : ""}`} key={event.id}><button type="button" className="generation-tool-head" onClick={() => setExpandedTools((current) => ({ ...current, [event.id]: !expanded }))}><span className="generation-tool-icon"><Toolbox size={15} /></span><span><strong>{toolName === "agent.activity" ? "Agent 活动摘要" : toolName}</strong><small>{activitySummary || (event.event_type === "tool.started" ? "开始" : event.event_type === "tool.progress" ? "进行中" : "已完成")}</small></span><CaretDown size={14} className={expanded ? "rotated" : ""} /></button>{expanded && <pre>{JSON.stringify(event.data, null, 2)}</pre>}</article>;
              }
              const message = event.event_type === "conversation.confirmed" ? "本批方案已确认，执行进度和产物在右侧结果中查看。" : eventText(event) || (event.event_type === "turn.completed" ? "方案已更新，可以在右侧继续编辑。" : "");
              const retryTurn = event.turn_id ?? conversation.current_turn_id;
              const latestError = [...allVisibleEvents].reverse().find(item=>item.event_type === "turn.failed" || item.event_type === "agent.unavailable");
              const repairedDraft = isErrorEvent && event.data.error_code === "draft_invalid" && events.some(item=>item.sequence>event.sequence && item.event_type==="draft.updated" && Array.isArray((item.data.draft as GenerationPlanningDraft | undefined)?.tasks) && ((item.data.draft as GenerationPlanningDraft).tasks.length > 0));
              if (repairedDraft) return <details key={event.id} className="generation-history-error"><summary>历史方案校验错误 · 后续方案已保存</summary><pre>{JSON.stringify(event.data,null,2)}</pre></details>;
              if (isErrorEvent && (event.id !== latestError?.id || sending || events.some(item=>item.sequence>event.sequence && ["turn.completed","conversation.confirmed"].includes(item.event_type)))) return null;
              if (["usage.updated","turn.started","conversation.settings.updated","context.updated"].includes(event.event_type)) return null;

              const fieldPath = textValue(event.data.field_path);
              const validationMessage = textValue(event.data.validation_message);
              return <article className={`generation-message ${isUser ? "user" : "assistant"} ${isErrorEvent ? "error" : ""}`} key={event.id}><div className="generation-message-avatar">{isUser ? <span>你</span> : isErrorEvent ? <WarningCircle size={17} weight="fill" /> : <Sparkle size={16} weight="fill" />}</div><div className="generation-message-content"><div className="generation-message-meta"><strong>{isUser ? "你" : isErrorEvent ? "规划需要恢复" : event.event_type === "reasoning.summary" ? "思考摘要" : "Agent"}</strong><small>{event.event_type === "assistant.delta" ? "流式片段" : event.event_type === "assistant.message" ? "" : ""}</small></div>{message && (isUser ? <p>{message}</p> : <MarkdownPreview content={message} className="generation-message-markdown" />)}{isErrorEvent && fieldPath && validationMessage && <p className="generation-validation-detail"><code>{fieldPath}</code> {validationMessage}</p>}{isErrorEvent && <details><summary>诊断详情</summary><pre>{JSON.stringify(event.data,null,2)}</pre></details>}{isErrorEvent && <div className="generation-error-actions"><button type="button" className="text-button" onClick={() => setManualMode(true)}>人工编辑 <CaretRight size={14} /></button>{event.data.retryable !== false && retryTurn && <button type="button" className="text-button" onClick={() => void recover(retryTurn!)}><ArrowsClockwise size={13} /> 继续本轮</button>}</div>}</div></article>;
            })}
            {conversation.pending_proposal && <article className="generation-inline-error"><div><strong>你编辑了方案，Agent 新提案等待处理</strong><details><summary>比较当前方案与新提案</summary><h4>我的当前方案</h4><pre>{JSON.stringify(currentDraft,null,2)}</pre><h4>Agent 新提案</h4><pre>{JSON.stringify(conversation.pending_proposal,null,2)}</pre></details>{(["keep","apply"] as const).map(action=><button key={action} type="button" onClick={()=>void resolveGenerationProposal(conversation.id,action,conversation.draft_hash??"").then(value=>{setConversation(value);setDraft(value.draft);}).catch(e=>setError(String(e)))}>{action==="keep"?"保留我的编辑":"应用 Agent 提案"}</button>)}</div></article>}
            {error && <article className="generation-inline-error" role="alert"><WarningCircle size={17} weight="fill" /><div><strong>当前操作未完成</strong><span>{error}</span>{draftSaveFailed && <button className="generation-inline-retry" type="button" onClick={retryDraftSave}><ArrowsClockwise size={14} /> 重试保存</button>}{draftSaveFailed && /context|上下文/.test(error) && <button type="button" onClick={()=>void refreshAndKeepDraft()}>刷新引用快照并保留编辑</button>}</div><button type="button" onClick={() => setError("")} aria-label="关闭错误"><X size={14} /></button></article>}
            {pendingInput && <GenerationInputCard key={pendingInput.id} request={pendingInput} onAnswer={answerInput} />}
            {sending && !hasPendingInput && <div className="generation-thinking"><span className="thinking-dots"><i /><i /><i /></span><span>规划进行中 · 流式输出和阶段进度显示在上方</span></div>}
            {!visibleEvents.length && <div className="generation-empty-chat"><Question size={28} /><strong>从一句目标开始</strong><span>例如：为序章场景生成一张 16:9 的夜雨背景，参考当前风格圣经。</span></div>}
            <div ref={chatEndRef} />
          </div>
          <div className="generation-composer-wrap">
            <div className="generation-composer-hint"><span><Lightning size={14} weight="fill" /> 只读沙箱</span><span>{hasPendingInput ? "请先回答上方问题" : selectedContextIds.length ? `已固定 ${selectedContextIds.length} 项参考资产` : "可随时加入参考资产"}</span>{(sending || conversation.status === "awaiting_input") && <button type="button" onClick={() => void stopTurn()}><Stop size={13} weight="fill" /> 停止当前回合</button>}</div>
            <div className="generation-composer">
              <textarea value={input} onChange={(event) => changeInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void sendMessage(); } }} placeholder={hasPendingInput ? "请先回答上方问题…" : sending ? "向正在运行的回合追加指令…" : "描述你想生成的资源、用途、风格或需要参考的资产…"} rows={3} disabled={!conversation || hasPendingInput} />
              <div className="generation-reference-chips">
                <GenerationRouteControl values={routeDefaults} providers={providers} disabled={routeEditingDisabled} onChange={updateSessionRoute} />
                {selectedAssets.map((asset) => <span key={asset.id} className="generation-reference-chip"><button type="button" onClick={()=>setPreviewAsset(asset)} aria-label={`预览 ${asset.name}`}>{asset.name}</button><button type="button" onClick={() => void updateReferenceContext(selectedContextIds.filter((id) => id !== asset.id))} aria-label={`移除参考 ${asset.name}`} disabled={referenceSaving || sending}><X size={11} /></button></span>)}
                <button type="button" className="generation-context-picker" onClick={() => setReferencePickerOpen(true)} disabled={!conversation || conversation.status === "completed" || referenceSaving || sending || hasPendingInput}><Plus size={15} /> 添加参考资产{referenceSaving && <CircleNotch size={12} className="spin" />}</button>
                <span className="generation-shortcut">Enter 发送 · Shift+Enter 换行</span>
                <button type="button" className="button primary send-button" onClick={() => void sendMessage()} disabled={!input.trim() || referenceSaving || hasPendingInput}><PaperPlaneRight size={17} weight="fill" /> {sending ? "追加指令" : "发送"}</button>
              </div>
            </div>
          </div>
          </> : <section className="generation-center-empty">
            <div className="generation-center-empty-mark"><Sparkle size={24} weight="fill" /></div>
            <span className="generation-kicker">GENERATION CENTER / 会话</span>
            <h2>选择一个会话，或开始新的规划。</h2>
            <p>会话会在制作台后台持续运行。你可以先处理资产、叙事或交付，再回来继续。</p>
            <button className="button primary" type="button" onClick={onNewSession}><Plus size={16} /> 新建会话</button>
          </section>}
        </main>

        <aside className="generation-plan-rail" aria-label="生成方案检查器">
          <div className="generation-plan-header"><label className="generation-panel-resize">面板宽度<input aria-label="工作面板宽度" type="range" min="320" max="560" value={panelWidth} onChange={e=>setPanelWidth(Number(e.target.value))}/></label><div><span className="generation-kicker">PLAN INSPECTOR</span><h2>生成方案</h2></div><button className="icon-button" type="button" onClick={() => setPlanCollapsed(true)} aria-label="折叠方案检查器"><Minus size={16} /></button></div>
          <div className="generation-work-tabs" role="tablist" aria-label="工作面板">{([["plan","方案"],["references","参考"],["results","结果"]] as const).map(([id,label])=><button role="tab" aria-selected={workTab===id} type="button" key={id} onClick={()=>setWorkTab(id)}>{label}</button>)}</div>
          {workTab === "references" && <div className="generation-plan-scroll"><h3>项目规则</h3><p className="generation-muted-copy">Agent 自动读取项目规范。固定参考用于补充本次目标，无需重复固定全部规则。</p>{assets.filter(a=>a.kind==="production" && /style_bible|prompt_recipe/.test(a.subtype)).map(asset=><ReferenceTile key={asset.id} asset={asset} onPreview={setPreviewAsset}/>)}<h3>用户固定参考</h3>{selectedAssets.map(asset=><ReferenceTile key={asset.id} asset={asset} onPreview={setPreviewAsset}/>)}<button className="button secondary" type="button" onClick={()=>setReferencePickerOpen(true)}>添加参考资产</button><h3>任务引用</h3>{[...new Set(currentDraft?.tasks.flatMap(task=>task.references.map(ref=>ref.asset_id))??[])].map(id=>{const asset=assets.find(x=>x.id===id);return asset?<ReferenceTile key={id} asset={asset} onPreview={setPreviewAsset}/>:<p key={id}>参考资产已缺失</p>;})}</div>}
          {workTab === "results" && <div className="generation-plan-scroll"><GenerationResults batches={conversation?.batches??[]} assets={assets} onOpenAsset={onOpenAsset}/></div>}
          {workTab === "plan" && (currentDraft ? <div className="generation-plan-scroll">
            <div className="generation-plan-summary"><div className="plan-summary-mark"><ListChecks size={20} /></div><div><strong>{currentDraft.tasks.length ? `${currentDraft.tasks.length} 项产物` : "当前目标"}</strong><span>{currentDraft.summary || events.find(e=>e.event_type==="user.message")?.data.content as string || "描述你的目标，我们一起确定产物和参考。"}</span></div></div>
            <details className="generation-advanced"><summary>模型与生成路由</summary><section className="generation-plan-section"><div className="generation-section-title"><span>会话模型路由</span><Funnel size={15} /></div><p className="generation-field-help generation-route-help">规划使用文字路由。在聊天输入框的“生成路由”中修改；任务内明确指定的渠道仍然优先。</p><div className="generation-route-summary"><div><span>文字</span><strong>{routeLabel(routeDefaults.text, providers)}</strong></div><div><span>图片</span><strong>{routeLabel(routeDefaults.image, providers)}</strong></div></div></section></details>
            <section className="generation-plan-section"><div className="generation-section-title"><span>资源清单</span><span className="generation-count">{currentDraft.tasks.length}</span></div>{currentDraft.tasks.map((task, index) => <TaskProposalEditor key={`${task.id || "task"}-${index}`} task={task} index={index} assets={assets} allTasks={currentDraft.tasks} providers={providers} sessionRoute={routeDefaults[task.kind === "text" ? "text" : "image"] ?? null} onChange={(updater) => editAndSave((value) => { const target = value.tasks[index]; if (target) updater(target); })} onRemove={() => editAndSave((value) => { value.tasks.splice(index, 1); })} />)}<div className="generation-add-task-row"><button className="generation-add-task" type="button" onClick={() => setTaskPickerOpen(true)} disabled={!assets.some((asset) => !currentDraft.tasks.some((task) => task.asset.asset_id === asset.id))}><Plus size={15} /> 从项目资产添加任务</button><button className="generation-add-task" type="button" onClick={() => editAndSave((value) => { value.tasks.push(draftTaskForNewAsset(value.tasks.length)); })}><Plus size={15} /> 手动新建资源</button></div></section>
            {currentDraft.tasks.length > 0 && <details className="generation-advanced"><summary>高级执行设置</summary><section className="generation-plan-section"><div className="generation-section-title"><span>预算与执行</span><SlidersHorizontal size={15} /></div><div className="generation-settings-grid"><NumberField label="额外调用上限" value={numberValue(currentDraft.settings.extra_call_budget, 2)} onChange={(value) => editAndSave((draftValue) => { draftValue.settings.extra_call_budget = value; })} /><NumberField label="并发" value={numberValue(currentDraft.settings.max_concurrency, 3)} onChange={(value) => editAndSave((draftValue) => { draftValue.settings.max_concurrency = value; })} /><NumberField label="重试" value={numberValue(currentDraft.settings.max_transport_retries, 2)} onChange={(value) => editAndSave((draftValue) => { draftValue.settings.max_transport_retries = value; })} /><NumberField label="付费返工" value={numberValue(currentDraft.settings.max_paid_remediation_rounds, 2)} onChange={(value) => editAndSave((draftValue) => { draftValue.settings.max_paid_remediation_rounds = value; })} /></div><div className="generation-path-legend"><span><i className="candidate-dot" />候选暂存</span><code>workspace/candidates/&lt;job&gt;</code><span><i className="target-dot" />批准后落地</span></div></section></details>}
            {(unresolvedQuestions.length > 0 || warnings.length > 0 || currentDraft.assumptions.length > 0 || hasPendingInput) && <section className="generation-plan-section"><div className="generation-section-title"><span>需要你的判断</span><Question size={15} /></div>{hasPendingInput && <button type="button" className="generation-answer-link" onClick={() => { document.getElementById(`generation-input-${pendingInput!.id}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); document.getElementById(`generation-input-${pendingInput!.id}`)?.focus(); }}>在对话中回答</button>}{!hasPendingInput && unresolvedQuestions.map((question) => <div className="generation-question" key={question}><Question size={14} /><span>{question.replace(/^schema:/, "请补充 JSON Schema：")}</span></div>)}{currentDraft.assumptions.map((assumption) => <div className="generation-assumption" key={assumption}><CheckCircle size={14} /><span>{assumption}</span></div>)}{warnings.map((warning, index) => { const code = textValue(warning.code); return <label className="generation-warning generation-warning-check" key={`${code}-${index}`}><input type="checkbox" checked={Boolean(code && acceptedWarningCodes.includes(code))} onChange={(event) => { if (!code) return; setAcceptedWarningCodes((current) => event.target.checked ? [...new Set([...current, code])] : current.filter((item) => item !== code)); }} /><ShieldWarning size={14} /><span>{textValue(warning.message, "方案包含需要确认的警告")}</span></label>; })}</section>}
            {taskErrors.length > 0 && <section className="generation-plan-section generation-errors" role="alert"><div className="generation-section-title"><span>字段校验</span><WarningCircle size={15} /></div>{taskErrors.map((item) => <div className="generation-error-row" key={item}><WarningCircle size={13} />{item}</div>)}</section>}
          </div> : <div className="generation-plan-empty">{conversation ? <><CircleNotch size={22} className="spin" /> 正在准备方案…</> : <><ListChecks size={22} /> 选择会话后查看生成方案</>}</div>)}
          {conversation && workTab === "plan" && <div className="generation-confirm-area">
            <button className="button primary generation-confirm-button" type="button" onClick={()=>setPreviewOpen(true)} disabled={!canPreview}>检查并生成</button>
            <small>确认后才会创建执行批次。价格未知时以调用上限控制。</small>
            {saving && <small className="generation-save-state"><FloppyDisk size={13} /> 正在保存草案…</small>}
            {draftSaveFailed && <small className="generation-save-state error"><WarningCircle size={13} /> 草案尚未保存，预览与确认已暂停。</small>}
            <small className="generation-confirm-note">确认前不会执行资产生成，也不会创建生成批次。</small>
          </div>}
        </aside>
      </div>
      {leftCollapsed && <button className="generation-restore-left" type="button" onClick={() => setLeftCollapsed(false)} aria-label="展开上下文栏"><CaretRight size={18} /></button>}
      {planCollapsed && <button className="generation-restore-plan" type="button" onClick={() => setPlanCollapsed(false)} aria-label="展开方案检查器"><CaretRight size={18} /></button>}
      {newMessages && <button className="generation-new-messages" type="button" onClick={()=>{setHistoryLimit(60);followBottom.current=true;setNewMessages(false);chatEndRef.current?.scrollIntoView?.({block:"end"});}}>有新消息 ↓</button>}
      {previewAsset && <GenerationAssetPreview asset={previewAsset} onClose={()=>setPreviewAsset(null)}/>}
      {renaming && <div className="generation-modal-backdrop"><section ref={renameDialogRef} tabIndex={-1} data-nested-modal="true" className="generation-modal" role="dialog" aria-modal="true" aria-label="重命名会话"><h2>重命名会话</h2><input aria-label="会话名称" value={renameText} onChange={e=>setRenameText(e.target.value)}/><button type="button" onClick={()=>setRenaming(null)}>取消</button><button type="button" disabled={!renameText.trim()} onClick={()=>void renameGenerationConversation(renaming,renameText).then(()=>{setRenaming(null);void recentQuery.refetch();void existingConversationQuery.refetch();}).catch(e=>setError(String(e)))}>保存</button></section></div>}
      {taskPickerOpen && <div className="generation-modal-backdrop"><section ref={taskDialogRef} tabIndex={-1} data-nested-modal="true" className="generation-modal" role="dialog" aria-modal="true" aria-label="选择任务资产"><h2>选择要生成的资产</h2><input aria-label="搜索任务资产" value={taskSearch} onChange={e=>setTaskSearch(e.target.value)} placeholder="搜索资产名称或 Key"/><div className="generation-task-picker">{assets.filter(a=>(a.name+a.key).includes(taskSearch)).slice(0,80).map(a=><div key={a.id}><ReferenceTile asset={a} onPreview={setPreviewAsset}/><button type="button" onClick={()=>{addTaskFromAsset(a);setTaskPickerOpen(false);}}>添加任务</button></div>)}</div><button type="button" onClick={()=>setTaskPickerOpen(false)}>关闭</button></section></div>}
      {previewOpen && currentDraft && <GenerationPlanPreview
        draft={currentDraft}
        assets={assets}
        confirmChecked={confirmChecked}
        canConfirm={canConfirm}
        confirmBusy={confirmBusy}
        acceptedWarningCodes={acceptedWarningCodes}
        onWarningsChange={setAcceptedWarningCodes}
        blockerMessage={error || (draftSaveFailed ? "方案保存失败，请返回编辑重试保存。" : saving ? "正在保存方案…" : taskErrors.join("；") || (hasPendingInput ? "请先回答对话中的问题。" : unresolvedQuestions.length ? "请先处理方案中的待补充信息。" : warningCodes.some(code=>!acceptedWarningCodes.includes(code)) ? "请勾选上方每项方案提示，再确认执行。" : ""))}
        onConfirmChecked={checked=>{setConfirmChecked(checked);setError("");}}
        onConfirm={() => void confirm()}
        onClose={() => setPreviewOpen(false)}
      />}
      {referencePickerOpen && <AssetReferencePicker
        assets={assets}
        selectedIds={selectedContextIds}
        initialSearch={referenceSearch}
        onApply={(ids) => void updateReferenceContext(ids)}
        onClose={() => { setReferencePickerOpen(false); setReferenceSearch(""); }}
      />}
      {archivedOpen && <ArchivedSessionsDialog
        sessions={archived}
        busyId={lifecycleBusy}
        onRestore={(item) => void restoreSession(item)}
        onDelete={setDeleteTarget}
        onClose={() => setArchivedOpen(false)}
      />}
      {deleteTarget && <DeleteConversationDialog
        conversation={deleteTarget}
        busy={lifecycleBusy === deleteTarget.id}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => void permanentlyDeleteSession()}
      />}
      <div className={`toast ${toast ? "visible" : ""}`} role="status" aria-live="polite">{toast}</div>
    </div>
  );
}

function GenerationRouteControl({
  values,
  providers,
  disabled,
  onChange,
}: {
  values: Partial<Record<ProviderModelModality, ProviderDefaultRoute | null>>;
  providers: GenerationProviderProfile[];
  disabled: boolean;
  onChange: (modality: ProviderModelModality, route: ProviderDefaultRoute | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  return (
    <div
      className="generation-composer-route"
      ref={rootRef}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || !open) return;
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
      }}
    >
      <button type="button" className={`generation-composer-route-trigger ${open ? "open" : ""}`} aria-label="生成路由" aria-expanded={open} disabled={disabled} onClick={() => setOpen((current) => !current)}>
        <SlidersHorizontal size={15} />
        <span><strong>生成路由</strong><small>文字 {routeLabel(values.text, providers)} · 图片 {routeLabel(values.image, providers)}</small></span>
        <CaretDown size={13} className={open ? "rotated" : ""} />
      </button>
      {open && <div className="generation-composer-route-popover" role="dialog" aria-label="选择资产生成供应商和模型">
        <header><div><span className="generation-kicker">ASSET GENERATION ROUTING</span><strong>选择生成供应商与模型</strong></div><button type="button" onClick={() => setOpen(false)} aria-label="关闭生成路由"><X size={14} /></button></header>
        <GenerationRouteSection modality="text" value={values.text ?? null} providers={providers} disabled={disabled} onChange={(route) => onChange("text", route)} />
        <label className="generation-reasoning-field"><span>文字模型思考等级</span><SelectMenu ariaLabel="文字模型思考等级" value={values.text?.reasoning_effort ?? ""} options={[{value:"",label:"模型默认"},{value:"none",label:"无"},{value:"minimal",label:"最低"},{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"最高"}]} disabled={disabled || !values.text} onChange={effort=>{if(values.text) onChange("text",{...values.text,reasoning_effort:(effort || undefined) as ProviderDefaultRoute["reasoning_effort"]});}} /><small>应用于后续规划及文字生成；模型不支持时会返回明确错误，不会静默降级。请先选择文字路由。</small></label>
        <GenerationRouteSection modality="image" value={values.image ?? null} providers={providers} disabled={disabled} onChange={(route) => onChange("image", route)} />
        <footer>这是当前会话的默认路由；任务内明确选择的渠道仍然优先。</footer>
      </div>}
    </div>
  );
}

function GenerationRouteSection({
  modality,
  value,
  providers,
  disabled,
  onChange,
}: {
  modality: "text" | "image";
  value: ProviderDefaultRoute | null;
  providers: GenerationProviderProfile[];
  disabled: boolean;
  onChange: (route: ProviderDefaultRoute | null) => void;
}) {
  const candidates = providers.filter((provider) => provider.is_active && providerModels(provider, modality).length > 0);
  const currentProvider = candidates.find((provider) => provider.id === value?.provider_profile_id);
  const models = currentProvider ? providerModels(currentProvider, modality) : [];
  const currentModelAvailable = Boolean(value?.model && models.includes(value.model));
  const modalityLabel = modality === "text" ? "文字" : "图片";
  const providerOptions = [
    { value: "", label: "沿用系统默认" },
    ...candidates.map((provider) => ({
      value: provider.id,
      label: `${provider.name}${provider.is_unlocked ? "" : " · 渠道已锁定"}`,
      disabled: !provider.is_unlocked,
    })),
  ];
  const modelOptions = models.map((model) => ({ value: model, label: model }));

  const selectProvider = (providerId: string) => {
    if (!providerId) {
      onChange(null);
      return;
    }
    const provider = candidates.find((item) => item.id === providerId);
    if (!provider || !provider.is_unlocked) return;
    const availableModels = providerModels(provider, modality);
    const configuredDefault = modality === "text" ? provider.text_model : provider.image_model;
    const previousModel = value?.provider_profile_id === providerId ? value.model : null;
    const model = (previousModel && availableModels.includes(previousModel) ? previousModel : null)
      ?? (configuredDefault && availableModels.includes(configuredDefault) ? configuredDefault : null)
      ?? availableModels[0];
    if (model) onChange({ provider_profile_id: provider.id, model });
  };

  return (
    <section className="generation-composer-route-section">
      <div className="generation-composer-route-heading"><span className={modality}>{modalityLabel.slice(0, 1)}</span><div><strong>{modalityLabel}生成</strong><small>{value ? routeLabel(value, providers) : "使用项目系统默认"}</small></div></div>
      <div className="generation-composer-route-fields">
        <label><span>供应商</span><SelectMenu ariaLabel={`${modalityLabel}生成供应商`} value={value?.provider_profile_id ?? ""} options={providerOptions} onChange={selectProvider} disabled={disabled} /></label>
        <label><span>模型</span><SelectMenu ariaLabel={`${modalityLabel}生成模型`} value={currentModelAvailable ? value?.model ?? "" : ""} options={modelOptions} onChange={(model) => { if (currentProvider && model) onChange({ provider_profile_id: currentProvider.id, model, reasoning_effort: value?.reasoning_effort }); }} disabled={disabled || !currentProvider || !currentProvider.is_unlocked || models.length === 0} placeholder={value?.model && !currentModelAvailable ? `当前模型不可用 · ${value.model}` : value ? "没有可用模型" : "由系统默认决定"} /></label>
      </div>
      {currentProvider && !currentProvider.is_unlocked && <small className="generation-composer-route-warning"><ShieldWarning size={12} /> 渠道已锁定，请先到供应商渠道中解锁。</small>}
      {currentProvider?.is_unlocked && value?.model && !currentModelAvailable && models.length > 0 && <small className="generation-composer-route-warning"><WarningCircle size={12} /> 当前模型不可用，请重新选择。</small>}
      {!candidates.length && <small className="generation-composer-route-warning"><WarningCircle size={12} /> 没有支持{modalityLabel}生成的可用渠道。</small>}
    </section>
  );
}

function RoutePicker({
  modality,
  value,
  providers,
  onChange,
  fallbackLabel,
}: {
  modality: ProviderModelModality;
  value: ProviderDefaultRoute | null;
  providers: GenerationProviderProfile[];
  onChange: (route: ProviderDefaultRoute | null) => void;
  fallbackLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const options = routeOptions(providers, modality);
  const filtered = options.filter((option) => `${option.providerName} ${option.model}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const label = routeLabel(value, providers, fallbackLabel);
  return (
    <div className="generation-route-picker">
      <button type="button" className={`generation-route-trigger ${open ? "open" : ""}`} onClick={() => setOpen((current) => !current)} aria-expanded={open}>
        <span><small>{modality === "text" ? "文字" : "图片"}</small><strong>{label}</strong></span><CaretDown size={13} className={open ? "rotated" : ""} />
      </button>
      {open && <div className="generation-route-popover" role="listbox" aria-label={`${modality === "text" ? "文字" : "图片"}渠道和模型`}>
        <div className="generation-route-search"><MagnifyingGlass size={13} /><input autoFocus value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索渠道或模型" /></div>
        <button type="button" className="generation-route-option inherited" onClick={() => { onChange(null); setOpen(false); setSearch(""); }}><span><strong>{fallbackLabel}</strong><small>由系统默认或任务优先级决定</small></span><CheckCircle size={13} /></button>
        {filtered.map((option) => <button key={`${option.providerId}-${option.model}`} type="button" className={`generation-route-option ${option.locked ? "locked" : ""}`} disabled={option.locked} onClick={() => { onChange({ provider_profile_id: option.providerId, model: option.model }); setOpen(false); setSearch(""); }}><span><strong>{option.providerName}</strong><small>{option.model}{option.locked ? " · 渠道已锁定" : ""}</small></span>{option.locked ? <ShieldWarning size={13} /> : <Check size={13} />}</button>)}
        {!filtered.length && <p className="generation-route-empty">没有匹配的可用模型，请到系统设置检查渠道目录。</p>}
      </div>}
    </div>
  );
}

function TaskProposalEditor({
  task,
  index,
  assets,
  allTasks,
  providers,
  sessionRoute,
  onChange,
  onRemove,
}: {
  task: GenerationTaskProposal;
  index: number;
  assets: GameAsset[];
  allTasks: GenerationTaskProposal[];
  providers: GenerationProviderProfile[];
  sessionRoute: ProviderDefaultRoute | null;
  onChange: (updater: (task: GenerationTaskProposal) => void) => void;
  onRemove: () => void;
}) {
  const [open, setOpen] = useState(index === 0);
  const [referencePreview,setReferencePreview]=useState<GameAsset|null>(null);
  const [referenceRevision,setReferenceRevision]=useState<string|null>(null);
  const [referenceChoice,setReferenceChoice]=useState("");
  const assetLabel = task.asset.mode === "new"
    ? task.asset.title || task.asset.key || "待创建资源"
    : assets.find((asset) => asset.id === task.asset.asset_id)?.name ?? task.asset.key ?? "未选择资源";
  const taskKindLabel = task.kind === "image_edit" ? "图像编辑" : task.kind === "image" ? "图像生成" : "结构化文字";
  const referenceCandidates = assets.filter((asset) => !task.references.some((reference) => reference.asset_id === asset.id));
  return (
    <div className={`generation-task-editor ${open ? "open" : ""}`}>
      <div className="generation-task-summary">
        <button type="button" onClick={() => setOpen((current) => !current)} aria-expanded={open}>
          <span className="task-index">{String(index + 1).padStart(2, "0")}</span>
          <span><strong>{assetLabel}</strong><small>{taskKindLabel} · {task.id || "未命名任务"}</small></span>
          <CaretDown size={14} className={open ? "rotated" : ""} />
        </button>
        <button className="icon-button danger-quiet" type="button" onClick={onRemove} aria-label={`删除 ${assetLabel}`}><Trash size={14} /></button>
      </div>
      {open && <div className="generation-task-fields">
        <div className="generation-field-row">
          <div className="generation-select-field"><span>资源来源</span>
            <SelectMenu
              ariaLabel="资源来源"
              value={task.asset.mode}
              options={[{ value: "existing", label: "已有项目资产" }, { value: "new", label: "确认时新建资产" }]}
              onChange={(nextMode) => onChange((value) => {
              value.asset.mode = nextMode as GenerationAssetProposal["mode"];
              if (value.asset.mode === "new") {
                value.asset.asset_id = null;
                value.asset.kind = value.asset.kind ?? "media";
                value.asset.title = value.asset.title ?? "待创建资源";
              }
            })}
            />
          </div>
          <label>任务 ID<input value={task.id} onChange={(event) => onChange((value) => { value.id = event.target.value; })} /></label>
        </div>
        {task.asset.mode === "existing" ? <div className="generation-select-field"><span>选择已有资产</span>
          <SelectMenu
            ariaLabel="选择已有资产"
            value={task.asset.asset_id ?? ""}
            options={[{ value: "", label: "请选择项目资产" }, ...assets.map((asset) => ({ value: asset.id, label: `${asset.name} · ${asset.key}` }))]}
            onChange={(assetId) => onChange((value) => {
            const selected = assets.find((asset) => asset.id === assetId);
            value.asset.asset_id = assetId || null;
            if (selected) {
              value.asset.key = selected.key;
              value.asset.kind = selected.kind;
              value.asset.subtype = selected.subtype;
              value.asset.title = selected.name;
              value.asset.schema_ref = selected.schemaRef ?? null;
            }
          })}
          />
        </div> : <>
          <div className="generation-field-row">
            <label>稳定 Key<input value={task.asset.key ?? ""} onChange={(event) => onChange((value) => { value.asset.key = event.target.value || null; })} placeholder="例如 media.chapter-01-bg" /></label>
            <label>资产标题<input value={task.asset.title ?? ""} onChange={(event) => onChange((value) => { value.asset.title = event.target.value || null; })} /></label>
          </div>
          <div className="generation-field-row">
            <div className="generation-select-field"><span>资产类型</span><SelectMenu ariaLabel="资产类型" value={task.asset.kind ?? "media"} options={[{ value: "content", label: "内容" }, { value: "design", label: "设计" }, { value: "entity", label: "实体" }, { value: "media", label: "媒体" }, { value: "production", label: "制作" }]} onChange={(kind) => onChange((value) => { value.asset.kind = kind as GenerationAssetProposal["kind"]; })} /></div>
            <label>Subtype<input value={task.asset.subtype ?? ""} onChange={(event) => onChange((value) => { value.asset.subtype = event.target.value || null; })} placeholder="例如 background" /></label>
          </div>
          {task.kind === "text" && <label>项目 JSON Schema 路径<input value={task.asset.schema_ref ?? ""} onChange={(event) => onChange((value) => { value.asset.schema_ref = event.target.value || null; })} placeholder="例如 schemas/dialogue.schema.json" /></label>}
        </>}
        <div className="generation-field-row">
          <div className="generation-select-field"><span>任务类型</span><SelectMenu ariaLabel="任务类型" value={task.kind} options={[{ value: "text", label: "结构化文字" }, { value: "image", label: "图像生成" }, { value: "image_edit", label: "图像编辑" }]} onChange={(kind) => onChange((value) => { value.kind = kind as GenerationTaskProposal["kind"]; })} /></div>
          <div className="generation-route-field"><span>任务渠道 / 模型</span><RoutePicker modality={task.kind === "text" ? "text" : "image"} value={task.provider_profile_id && task.model ? { provider_profile_id: task.provider_profile_id, model: task.model } : null} providers={providers} fallbackLabel={sessionRoute ? routeLabel(sessionRoute, providers, "沿用会话默认") : "沿用会话默认"} onChange={(route) => onChange((value) => { value.provider_profile_id = route?.provider_profile_id ?? null; value.model = route?.model ?? null; })} /></div>
        </div>
        <details className="generation-advanced"><summary>Prompt</summary><label>Prompt<textarea value={task.prompt} rows={3} onChange={(event) => onChange((value) => { value.prompt = event.target.value; })} /></label></details>
        {task.kind !== "text" && <>
          <div className="generation-field-row"><label>宽<input type="number" min={1} max={8192} value={task.width ?? ""} onChange={(event) => onChange((value) => { value.width = Number(event.target.value) || null; })} /></label><label>高<input type="number" min={1} max={8192} value={task.height ?? ""} onChange={(event) => onChange((value) => { value.height = Number(event.target.value) || null; })} /></label></div>
          <label className="generation-checkbox-line"><input type="checkbox" checked={task.transparent} onChange={(event) => onChange((value) => { value.transparent = event.target.checked; })} /> 保留透明通道</label>
        </>}
        <label>批准后落地路径<input value={task.target_path ?? ""} onChange={(event) => onChange((value) => { value.target_path = event.target.value || null; })} placeholder="例如 approved/assets/portraits/hero.png" /></label>

        <div className="generation-editor-subsection">
          <div className="generation-subsection-title"><span>依赖与上游产物</span><small>按顺序注入任务结果</small></div>
          {allTasks.filter((candidate) => candidate.id !== task.id).length > 0 ? <div className="generation-dependency-list">{allTasks.filter((candidate) => candidate.id !== task.id).map((candidate) => <label key={candidate.id} className="generation-checkbox-line"><input type="checkbox" checked={task.depends_on.includes(candidate.id)} onChange={(event) => onChange((value) => { value.depends_on = event.target.checked ? [...new Set([...value.depends_on, candidate.id])] : value.depends_on.filter((id) => id !== candidate.id); if (!event.target.checked && value.reference_task_id === candidate.id) value.reference_task_id = null; })} /> {candidate.id || "未命名任务"}</label>)}</div> : <small className="generation-field-help">添加第二项任务后可配置依赖。</small>}
          {task.kind === "image_edit" && <div className="generation-select-field"><span>上游参考任务</span><SelectMenu ariaLabel="上游参考任务" value={task.reference_task_id ?? ""} options={[{ value: "", label: "不使用上游产物" }, ...allTasks.filter((candidate) => candidate.id !== task.id).map((candidate) => ({ value: candidate.id, label: candidate.id }))]} onChange={(referenceTaskId) => onChange((value) => { value.reference_task_id = referenceTaskId || null; if (referenceTaskId && !value.depends_on.includes(referenceTaskId)) value.depends_on = [...value.depends_on, referenceTaskId]; })} /></div>}
        </div>

        <div className="generation-editor-subsection">
          <div className="generation-subsection-title"><span>参考资源</span><small>一项 primary，其余 supporting</small></div>
          {task.references.map((reference, referenceIndex) => <div className="generation-reference-editor" key={`${reference.asset_id}-${referenceIndex}`}>
            {assets.find(a=>a.id===reference.asset_id) && <ReferenceTile asset={assets.find(a=>a.id===reference.asset_id)!} onPreview={asset=>{setReferenceRevision(reference.revision_id??null);setReferencePreview(asset);}}/>}
            <div className="generation-field-row"><div className="generation-select-field"><span>参考资产</span><SelectMenu ariaLabel="参考资产" value={reference.asset_id} options={assets.map((asset) => ({ value: asset.id, label: `${asset.name} · ${asset.key}` }))} onChange={(assetId) => onChange((value) => { const item = value.references[referenceIndex]; if (item) item.asset_id = assetId; })} /></div><div className="generation-select-field"><span>角色</span><SelectMenu ariaLabel="角色" value={reference.role} options={[{ value: "primary", label: "主参考图" }, { value: "supporting", label: "辅助参考" }]} onChange={(role) => onChange((value) => { const item = value.references[referenceIndex]; if (item) item.role = role as GenerationReferenceProposal["role"]; })} /></div></div>
            <label>选择理由<input value={reference.reason} onChange={(event) => onChange((value) => { const item = value.references[referenceIndex]; if (item) item.reason = event.target.value; })} placeholder="说明它如何影响方案" /></label>
            <details><summary>引用版本技术详情</summary><div className="generation-field-row"><label>版本标识<input value={reference.revision_id ?? ""} onChange={(event) => onChange((value) => { const item = value.references[referenceIndex]; if (item) item.revision_id = event.target.value || null; })} placeholder="确认时自动固定" /></label><label>SHA-256<input value={reference.sha256 ?? ""} onChange={(event) => onChange((value) => { const item = value.references[referenceIndex]; if (item) item.sha256 = event.target.value || null; })} placeholder="确认时自动固定" /></label></div></details>
            <button type="button" className="text-button generation-remove-reference" onClick={() => onChange((value) => { value.references.splice(referenceIndex, 1); })}><Trash size={13} /> 移除参考</button>
          </div>)}
          <SelectMenu ariaLabel="选择任务参考" value={referenceChoice} options={[{value:"",label:"选择参考资产"},...referenceCandidates.map(a=>({value:a.id,label:a.name}))]} onChange={setReferenceChoice}/><button type="button" className="generation-inline-add" onClick={() => { const candidate = referenceCandidates.find(a=>a.id===referenceChoice); if (candidate) onChange((value) => { value.references.push({ asset_id: candidate.id, role: value.references.some((item) => item.role === "primary") ? "supporting" : "primary", reason: "" }); }); }} disabled={!referenceChoice}><Plus size={13} /> 添加参考资源</button>
        </div>
        <small className="generation-field-help">候选暂存由系统管理：workspace/candidates/&lt;job&gt;。确认时会固定参考 revision / rendition / hash，Provider 只接收 primary。</small>
      </div>}
    {referencePreview && <GenerationAssetPreview asset={referencePreview} revisionId={referenceRevision} onClose={()=>setReferencePreview(null)}/>}
    </div>
  );
}

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return <label className="generation-number-field"><span>{label}</span><input type="number" min={0} max={10_000} value={value} onChange={(event) => onChange(Math.max(0, Number(event.target.value) || 0))} /></label>;
}

function AssetReferencePicker({
  assets,
  selectedIds,
  initialSearch,
  onApply,
  onClose,
}: {
  assets: GameAsset[];
  selectedIds: string[];
  initialSearch: string;
  onApply: (ids: string[]) => void;
  onClose: () => void;
}) {
  const [search, setSearch] = useState(initialSearch);
  const [kind, setKind] = useState("all");
  const [subtype, setSubtype] = useState("all");
  const [status, setStatus] = useState("all");
  const [draftIds, setDraftIds] = useState<string[]>(selectedIds);
  const [selectedOnly, setSelectedOnly] = useState(false);
  const [assetPreview,setAssetPreview] = useState<GameAsset|null>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  useModalFocus({ open: true, dialogRef, initialFocusRef: searchRef, onClose });
  const selectedSet = new Set(draftIds);
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  const subtypeOptions = [...new Set(assets.map((asset) => asset.subtype).filter(Boolean))].sort();
  const selectedAssets = draftIds.map((id) => assetsById.get(id)).filter((asset): asset is GameAsset => Boolean(asset));
  const filtered = assets.filter((asset) => {
    const needle = search.trim().toLocaleLowerCase();
    const haystack = `${asset.name} ${asset.key} ${asset.tags.join(" ")}`.toLocaleLowerCase();
    return (!selectedOnly || selectedSet.has(asset.id)) && (!needle || haystack.includes(needle)) && (kind === "all" || asset.kind === kind) && (subtype === "all" || asset.subtype === subtype) && (status === "all" || asset.reviewStatus === status);
  });
  const visibleAssets = filtered.slice(0, 120);
  return (
    <div className="generation-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      {assetPreview && <GenerationAssetPreview asset={assetPreview} onClose={()=>setAssetPreview(null)}/>}
      <section ref={dialogRef} className="generation-modal generation-reference-dialog" role="dialog" aria-modal="true" aria-labelledby="reference-picker-title" tabIndex={-1} data-nested-modal="true">
        <header className="generation-modal-header"><div><span className="generation-kicker">READ-ONLY CONTEXT / 参考范围</span><h2 id="reference-picker-title">选择 Agent 要读取的参考</h2><p>固定风格、角色或场景依据。这里只扩展只读上下文，不会创建资产或生成任务。</p></div><div className="generation-reference-header-actions"><span><strong>{draftIds.length}</strong> 项已选</span><button className="icon-button" type="button" onClick={onClose} aria-label="关闭参考资产选择器"><X size={17} /></button></div></header>
        <div className="generation-reference-filters">
          <label className="generation-modal-search"><MagnifyingGlass size={14} /><input ref={searchRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索名称、Key 或标签" /></label>
          <div className="generation-filter-field"><span>类型</span><SelectMenu ariaLabel="参考资产类型" value={kind} options={[{ value: "all", label: "全部类型" }, ...[...new Set(assets.map((asset) => asset.kind))].map((item) => ({ value: item, label: assetKindLabel(item) }))]} onChange={setKind} /></div>
          <div className="generation-filter-field"><span>Subtype</span><SelectMenu ariaLabel="参考资产 Subtype" value={subtype} options={[{ value: "all", label: "全部 Subtype" }, ...subtypeOptions.map((item) => ({ value: item, label: item }))]} onChange={setSubtype} /></div>
          <div className="generation-filter-field"><span>状态</span><SelectMenu ariaLabel="参考资产状态" value={status} options={[{ value: "all", label: "全部状态" }, { value: "pending", label: "待审查" }, { value: "approved", label: "已批准" }, { value: "rejected", label: "已拒绝" }, { value: "generating", label: "生成中" }]} onChange={setStatus} /></div>
        </div>
        <div className="generation-reference-workspace">
          <div className="generation-reference-browser">
            <div className="generation-reference-result-meta"><span>{selectedOnly ? "正在查看已选参考" : `找到 ${filtered.length} 项`}{filtered.length > visibleAssets.length && <small>显示前 {visibleAssets.length} 项，请搜索缩小范围</small>}</span><button type="button" className={selectedOnly ? "active" : ""} aria-pressed={selectedOnly} onClick={() => setSelectedOnly((current) => !current)}><Check size={12} /> 只看已选</button></div>
            <div className="generation-reference-results">{visibleAssets.length ? visibleAssets.map((asset) => { const selected = draftIds.includes(asset.id); const thumbnail = asset.thumbnails?.[0]; return <div key={asset.id} className="generation-reference-choice"><button type="button" className={`generation-reference-result ${selected ? "selected" : ""}`} aria-pressed={selected} onClick={() => setDraftIds((current) => selected ? current.filter((id) => id !== asset.id) : [...current, asset.id])}><span className="generation-reference-preview">{thumbnail ? <img src={thumbnail} alt="" /> : asset.kind === "media" ? <ImageSquare size={18} /> : <FileArrowUp size={18} />}</span><span className="generation-reference-result-copy"><strong>{asset.name}</strong><small>{asset.key}</small><em>{assetKindLabel(asset.kind)} · {assetSubtypeLabel(asset.kind, asset.subtype)}</em>{asset.tags.length > 0 && <i>{asset.tags.slice(0, 3).map((tag) => `#${tag}`).join(" ")}</i>}</span><span className="generation-reference-check">{selected ? <Check size={14} weight="bold" /> : <Plus size={13} />}</span></button><button type="button" className="generation-choice-preview" onClick={()=>setAssetPreview(asset)}>预览 {asset.name}</button></div>; }) : <div className="generation-route-empty">{selectedOnly ? "还没有选择参考资产。" : "没有匹配的项目资产，试试减少筛选条件。"}</div>}</div>
          </div>
          <aside className="generation-reference-selection" aria-label="已选参考资产">
            <header><div><span>已选参考</span><strong>{draftIds.length}</strong></div>{draftIds.length > 0 && <button type="button" onClick={() => setDraftIds([])}>清空</button>}</header>
            <div>{selectedAssets.length ? selectedAssets.map((asset) => <article key={asset.id}><span>{asset.kind === "media" ? <ImageSquare size={14} /> : <FileArrowUp size={14} />}</span><div><strong>{asset.name}</strong><small>{asset.key}</small></div><button type="button" onClick={() => setDraftIds((current) => current.filter((id) => id !== asset.id))} aria-label={`移除已选参考 ${asset.name}`}><X size={13} /></button></article>) : <p>从左侧选择关键风格、角色或场景。建议只固定真正影响结果的资产。</p>}</div>
          </aside>
        </div>
        <footer className="generation-modal-footer"><span>任务级参考仍可在右侧方案中单独设置。</span><div><button className="button secondary" type="button" onClick={onClose}>取消</button><button className="button primary" type="button" onClick={() => onApply(draftIds)}><Check size={15} /> 保存参考范围（{draftIds.length}）</button></div></footer>
      </section>
    </div>
  );
}

function ArchivedSessionsDialog({
  sessions,
  busyId,
  onRestore,
  onDelete,
  onClose,
}: {
  sessions: GenerationConversationSummary[];
  busyId: string | null;
  onRestore: (item: GenerationConversationSummary) => void;
  onDelete: (item: GenerationConversationSummary) => void;
  onClose: () => void;
}) {
  return <div className="generation-modal-backdrop" role="presentation"><section className="generation-modal generation-session-dialog" role="dialog" aria-modal="true" aria-labelledby="archived-sessions-title"><header className="generation-modal-header"><div><span className="generation-kicker">SESSION ARCHIVE / 会话管理</span><h2 id="archived-sessions-title">已归档会话</h2><p>归档只会移出最近列表；恢复后可以继续打开，永久删除前会再次确认。</p></div><button className="icon-button" type="button" onClick={onClose} aria-label="关闭已归档会话"><X size={17} /></button></header><div className="generation-session-list">{sessions.length ? sessions.map((item) => <div className="generation-session-row" key={item.id}><div><strong>{item.title ?? "未命名任务"}</strong><small>{item.turn_count} 回合 · {item.updated_at.slice(0, 10)}</small></div><div><button className="button secondary compact" type="button" onClick={() => onRestore(item)} disabled={busyId === item.id}><ArrowCounterClockwise size={14} /> 恢复</button><button className="button danger compact" type="button" onClick={() => onDelete(item)} disabled={busyId === item.id}><Trash size={14} /> 删除</button></div></div>) : <div className="generation-empty-dialog"><Archive size={22} /> 暂无已归档会话</div>}</div><footer className="generation-modal-footer"><span>已确认的 Plan、Job、资产和候选文件不属于聊天删除范围。</span><button className="button secondary" type="button" onClick={onClose}>完成</button></footer></section></div>;
}

function DeleteConversationDialog({
  conversation,
  busy,
  onCancel,
  onConfirm,
}: {
  conversation: GenerationConversationSummary;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return <div className="generation-modal-backdrop" role="presentation"><section className="generation-modal generation-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-conversation-title"><header className="generation-modal-header"><div><span className="generation-kicker">PERMANENT DELETE / 二次确认</span><h2 id="delete-conversation-title">永久删除这个会话？</h2><p>将删除聊天审计、上下文快照和对应 Codex 线程。这个操作不可恢复。</p></div><button className="icon-button" type="button" onClick={onCancel} aria-label="取消删除"><X size={17} /></button></header><div className="generation-delete-copy"><strong>{conversation.title ?? "未命名任务"}</strong><span>已确认的生成计划、任务、资产和候选文件不会被删除。</span></div><footer className="generation-modal-footer"><button className="button secondary" type="button" onClick={onCancel}>取消</button><button className="button danger" type="button" onClick={onConfirm} disabled={busy}>{busy ? <><CircleNotch size={15} className="spin" /> 删除中…</> : <><Trash size={15} /> 永久删除会话</>}</button></footer></section></div>;
}

function GenerationPlanPreview({
  acceptedWarningCodes, onWarningsChange, blockerMessage,
  draft,
  assets,
  confirmChecked,
  canConfirm,
  confirmBusy,
  onConfirmChecked,
  onConfirm,
  onClose,
}: {
  acceptedWarningCodes: string[];
  onWarningsChange: (codes:string[])=>void;
  blockerMessage: string;
  draft: GenerationPlanningDraft;
  assets: GameAsset[];
  confirmChecked: boolean;
  canConfirm: boolean;
  confirmBusy: boolean;
  onConfirmChecked: (checked: boolean) => void;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const [referencePreview,setReferencePreview]=useState<GameAsset|null>(null);
  const [referenceRevision,setReferenceRevision]=useState<string|null>(null);
  const previewDialogRef=useRef<HTMLElement>(null);
  useModalFocus({open:true,dialogRef:previewDialogRef,initialFocusRef:previewDialogRef,onClose});
  return (
    <div className="generation-preview-backdrop" role="presentation">
      {referencePreview && <GenerationAssetPreview asset={referencePreview} revisionId={referenceRevision} onClose={()=>setReferencePreview(null)}/>}
      <section ref={previewDialogRef} tabIndex={-1} className="generation-preview-dialog" role="dialog" aria-modal="true" aria-labelledby="generation-preview-title">
        <header className="generation-preview-header">
          <div>
            <span className="generation-kicker">FINAL REVIEW / 草案快照</span>
            <h2 id="generation-preview-title">确认前预览</h2>
            <p>{draft.summary || "请逐项核对资源、参考图、模型和落地路径。"}</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭预览"><X size={17} /></button>
        </header>
        <div className="generation-preview-scroll">
          <div className="generation-preview-ledger">
            <span>任务</span><strong>{draft.tasks.length}</strong>
            <span>基础调用</span><strong>{draft.tasks.length}</strong><span>额外调用上限</span><strong>{numberValue(draft.settings.extra_call_budget, 2)}</strong><span>预估费用</span><strong>未知</strong>
            <span>并发</span><strong>{numberValue(draft.settings.max_concurrency, 3)}</strong>
          </div>
          <ol className="generation-preview-tasks">
            {draft.tasks.map((task, index) => {
              const assetName = task.asset.mode === "new"
                ? task.asset.title || task.asset.key || "待创建资源"
                : assets.find((asset) => asset.id === task.asset.asset_id)?.name || task.asset.key || "已有项目资产";
              const kind = task.kind === "image_edit" ? "图像编辑" : task.kind === "image" ? "图像生成" : "结构化文字";
              return (
                <li key={`${task.id}-${index}`}>
                  <div className="generation-preview-task-heading">
                    <span className="task-index">{String(index + 1).padStart(2, "0")}</span>
                    <div><strong>{assetName}</strong><small>{kind} · {task.id} · {task.model || "沿用默认模型"}</small></div>
                  </div>
                  <code>{task.target_path || "尚未设置批准后路径"}</code>
                  <MarkdownPreview content={task.prompt || "未填写 Prompt"} className="generation-preview-prompt" />
                  {task.references.length > 0 && <div className="generation-preview-references"><span>参考资源</span>{task.references.map((reference, referenceIndex) => <small key={`${reference.asset_id}-${referenceIndex}`}><button type="button" onClick={()=>{const asset=assets.find(a=>a.id===reference.asset_id);if(asset){setReferenceRevision(reference.revision_id??null);setReferencePreview(asset);}}}>预览</button><b>{reference.role === "primary" ? "主" : "辅"}</b> {assets.find((asset) => asset.id === reference.asset_id)?.name || reference.asset_id}{reference.reason ? ` · ${reference.reason}` : ""}</small>)}</div>}
                </li>
              );
            })}
          </ol>
          {(draft.questions.length > 0 || draft.warnings.length > 0) && <div className="generation-preview-blockers">
            {draft.questions.map((question) => <p key={question}><Question size={14} />{question.replace(/^schema:/, "请补充 JSON Schema：")}</p>)}
            {draft.warnings.map((warning,index)=>{const code=textValue(warning.code);return <label key={`${code}-${index}`}><input type="checkbox" checked={acceptedWarningCodes.includes(code)} onChange={e=>onWarningsChange(e.target.checked ? [...new Set([...acceptedWarningCodes,code])] : acceptedWarningCodes.filter(x=>x!==code))}/><span>{textValue(warning.message,"方案包含需要确认的警告")}</span></label>;})}
          </div>}
        </div>
        <footer className="generation-preview-footer">
          {blockerMessage && <p className="generation-preview-error" role="status">{blockerMessage}</p>}
          <label className="generation-confirm-check"><input type="checkbox" checked={confirmChecked} onChange={(event) => onConfirmChecked(event.target.checked)} /><span>我已检查资源、参考图、路径、模型和预算，允许创建资产并进入执行队列。</span></label>
          <div className="generation-preview-actions"><button className="button secondary" type="button" onClick={onClose}>返回编辑</button><button className="button primary" type="button" onClick={onConfirm} disabled={!canConfirm}>{confirmBusy ? <><CircleNotch size={16} className="spin" /> 正在确认…</> : <><Check size={16} weight="bold" /> 确认并执行</>}</button></div>
        </footer>
      </section>
    </div>
  );
}
