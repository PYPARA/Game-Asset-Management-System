import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowsClockwise,
  CheckCircle,
  ClockCountdown,
  CurrencyDollar,
  GitBranch,
  Heartbeat,
  ImagesSquare,
  ListBullets,
  Path,
  Play,
  WarningCircle,
  Wrench,
  X,
} from "@phosphor-icons/react";
import {
  createRunRemediation,
  diagnoseRunWithCodex,
  fetchRunInspection,
  resumeGenerationPlan,
  runEvidenceUrl,
  subscribeToRunEvents,
  updateGenerationBudget,
} from "../lib/api";
import { useModalFocus } from "../hooks/useModalFocus";
import type {
  AgentSessionRun,
  GenerationJobRun,
  ProductionFindingRun,
  RunEvidenceItem,
  RunInspection,
} from "../types";
import { SelectMenu } from "./SelectMenu";

type InspectorTab = "pipeline" | "evidence" | "events";
type RemediationChoice = "retry" | "tool_repair" | "regenerate" | "image_edit" | "await_user";

interface RunInspectorDrawerProps {
  open: boolean;
  planId: string | null;
  focusAssetIds?: string[];
  onClose: () => void;
  onChanged: () => void;
}

const stages = ["queued", "output_received", "hard_qa", "semantic_qa", "candidate_ready"] as const;
const activePlanStatuses = new Set(["confirmed", "queued", "running", "remediating"]);
const actionableJobStatuses = new Set([
  "candidate_ready",
  "succeeded",
  "qa_failed",
  "awaiting_user",
  "failed",
]);

const stageLabels: Record<string, string> = {
  queued: "排队",
  output_received: "输出落盘",
  hard_qa: "硬 QA",
  semantic_qa: "语义复核",
  candidate_ready: "候选待批准",
};

const statusLabels: Record<string, string> = {
  draft: "草稿",
  confirmed: "已确认",
  queued: "排队",
  running: "执行中",
  output_received: "输出已落盘",
  hard_qa: "硬 QA",
  semantic_qa: "语义复核",
  candidate_ready: "生成成功 · 待批准",
  remediating: "返工中",
  awaiting_user: "等待人工",
  credentials_locked: "等待凭据",
  qa_failed: "QA 未通过",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const eventLabels: Record<string, string> = {
  "plan.confirmed": "计划已确认",
  "run.queued": "任务进入队列",
  "run.lease_acquired": "Runner 取得租约",
  "run.inputs_resolved": "上游输入已解析",
  "provider.call_started": "供应商调用开始",
  "provider.call_failed": "供应商调用失败",
  "provider.model_unavailable": "模型不可用，等待人工",
  "provider.credentials_locked": "供应商凭据已锁定",
  "artifact.created": "证据或产物已落盘",
  "run.stage_changed": "生产阶段变化",
  "finding.created": "发现阻塞问题",
  "action.accepted": "返工动作已接受",
  "budget.changed": "预算已变更",
  "run.awaiting_user": "流程等待人工",
  "run.recovered": "任务从持久状态恢复",
  "run.resumed": "任务已安全恢复",
  "worker.started": "媒体 Worker 启动",
  "agent.turn_started": "Agent 诊断开始",
  "agent.diagnosis_ready": "Agent 诊断完成",
  "agent.unavailable": "Agent 不可用，转人工",
  "agent.config_applied": "白名单配置已更新",
  "action.proposed": "Agent 提出动作",
  "action.rejected": "Agent 动作被拒绝",
  "changeset.created": "ChangeSet 待审批",
  "changeset.approved": "ChangeSet 已记录批准",
  "changeset.rejected": "ChangeSet 已驳回",
};

function formatCost(value: number | null | undefined): string {
  return value === null || value === undefined ? "未知" : value.toFixed(2);
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function actionLabel(action: RemediationChoice): string {
  if (action === "tool_repair") return "确定性工具修复";
  if (action === "regenerate") return "重新生成";
  if (action === "image_edit") return "参考图编辑";
  if (action === "retry") return "同请求重试";
  return "等待人工决定";
}

function evidenceLabel(item: RunEvidenceItem): string {
  const labels: Record<string, string> = {
    candidate: "当前候选",
    side_by_side: "参考 / 候选",
    overlay: "50% 叠加",
    difference: "像素差异",
    contact_sheet_dark: "深色联系表",
    contact_sheet_light: "浅色联系表",
    contact_sheet_checkerboard: "棋盘格联系表",
    comparison_unavailable: "无参考图",
  };
  return labels[item.kind] ?? item.label;
}

function evidenceDescription(item: RunEvidenceItem): string {
  if (item.kind === "overlay") return "参考图与候选图各占 50%，用于检查构图、轮廓和透明边缘；不是失败提示。";
  if (item.kind === "difference") return "高亮两图的像素变化，适合同源修订；不同角色之间仅供诊断留档。";
  if (item.kind === "side_by_side") return "并排核对参考与候选的风格、比例和构图。";
  if (item.kind.startsWith("contact_sheet")) return "在不同背景上检查透明通道、边缘和可读性。";
  return "";
}

function attemptTitle(purpose: string, phase: string, dispatchState?: string): string {
  const purposeLabel = purpose === "base" ? "初次生成" : purpose === "retry" ? "恢复重试" : purpose;
  const phaseLabel = phase === "succeeded" ? "已完成" : phase === "failed" ? "未完成" : phase;
  return `${purposeLabel} · ${phaseLabel}${dispatchState === "not_sent" ? "（历史）" : ""}`;
}

function networkPolicyLabel(policy?: string | null): string {
  if (policy === "fake_ip_compatible") return "Fake-IP 兼容";
  return policy ?? "";
}

function jobAssetId(job: GenerationJobRun): string {
  return typeof job.request?.asset_id === "string" ? job.request.asset_id : "";
}

function snapshotValue(job: GenerationJobRun, key: string): string {
  const value = job.provider_snapshot?.[key];
  return typeof value === "string" && value.trim() ? value : "";
}

function frozenProviderName(job: GenerationJobRun): string {
  return snapshotValue(job, "name") || job.provider_profile_id;
}

function frozenModel(job: GenerationJobRun): string {
  return snapshotValue(job, "model") || job.request.model || "模型未知";
}

function suggestedAction(
  job: GenerationJobRun,
  findings: ProductionFindingRun[],
): RemediationChoice {
  if (job.recovery_eligible) return "retry";
  const suggestion = findings.find((finding) => finding.job_id === job.id && !finding.resolved_at)
    ?.suggested_action;
  if (suggestion === "tool_repair" && job.task_kind !== "text") return "tool_repair";
  if (suggestion === "regenerate" || suggestion === "image_edit" || suggestion === "retry") {
    return suggestion;
  }
  return job.status === "awaiting_user" ? "await_user" : "regenerate";
}

function strategyDefault(action: RemediationChoice): string {
  if (action === "tool_repair") return "normalize";
  if (action === "regenerate") return "prompt-reframe";
  if (action === "image_edit") return "reference-preserving-edit";
  if (action === "retry") return "same-request";
  return "manual-handoff";
}

export function RunInspectorDrawer({
  open,
  planId,
  focusAssetIds = [],
  onClose,
  onChanged,
}: RunInspectorDrawerProps) {
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [tab, setTab] = useState<InspectorTab>("pipeline");
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [streamConnected, setStreamConnected] = useState(false);
  const [action, setAction] = useState<RemediationChoice>("regenerate");
  const [strategy, setStrategy] = useState("prompt-reframe");
  const [reason, setReason] = useState("");
  const [promptAdjustment, setPromptAdjustment] = useState("");
  const [budgetValue, setBudgetValue] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentSession, setAgentSession] = useState<AgentSessionRun | null>(null);
  const focusKey = focusAssetIds.join("|");
  const inspectionQuery = useQuery({
    queryKey: ["run-inspection", planId],
    queryFn: () => fetchRunInspection(planId as string),
    enabled: open && Boolean(planId),
    refetchInterval: open ? 2_000 : false,
  });
  const inspection = inspectionQuery.data;
  const selectedJob = inspection?.jobs.find((job) => job.id === selectedJobId) ?? null;
  const selectedFindings = useMemo(
    () => inspection?.findings.filter((finding) => finding.job_id === selectedJobId) ?? [],
    [inspection?.findings, selectedJobId],
  );
  const selectedAttempts = useMemo(
    () => inspection?.attempts.filter((attempt) => attempt.job_id === selectedJobId) ?? [],
    [inspection?.attempts, selectedJobId],
  );
  const selectedEvidence = useMemo(
    () => inspection?.evidence.filter((item) => item.job_id === null || item.job_id === selectedJobId) ?? [],
    [inspection?.evidence, selectedJobId],
  );

  useModalFocus({ open, dialogRef, initialFocusRef: closeRef, onClose });

  useEffect(() => {
    if (!open) return;
    setTab("pipeline");
    setMessage("");
  }, [open, planId]);

  useEffect(() => {
    if (!open || !inspection?.jobs.length) return;
    const focusIds = new Set(focusAssetIds);
    const preferred = inspection.jobs.find((job) => focusIds.has(jobAssetId(job)))
      ?? inspection.jobs.find((job) => job.status === "awaiting_user")
      ?? inspection.jobs[0];
    setSelectedJobId((current) => inspection.jobs.some((job) => job.id === current) ? current : preferred.id);
    setBudgetValue(inspection.plan.extra_call_budget);
  }, [focusKey, inspection?.plan.id, inspection?.jobs.length, open]);

  useEffect(() => {
    if (!selectedJobId) return;
    const latestSession = (inspection?.agent_sessions ?? [])
      .filter((item) => item.job_id === selectedJobId)
      .sort((left, right) => left.created_at.localeCompare(right.created_at))
      .at(-1);
    setAgentSession(latestSession ?? null);
  }, [inspection?.agent_sessions, selectedJobId]);

  useEffect(() => {
    if (!selectedJob || !inspection) return;
    const nextAction = suggestedAction(selectedJob, inspection.findings);
    setAction(nextAction);
    setStrategy(strategyDefault(nextAction));
    setReason(selectedJob.blocking_reason ?? selectedJob.error_message ?? "");
    setPromptAdjustment("");
  }, [selectedJob?.id]);

  useEffect(() => {
    if (!open || !planId) return;
    return subscribeToRunEvents(
      planId,
      () => void queryClient.invalidateQueries({ queryKey: ["run-inspection", planId] }),
      setStreamConnected,
    );
  }, [open, planId, queryClient]);

  const chooseAction = (nextAction: RemediationChoice) => {
    setAction(nextAction);
    setStrategy(strategyDefault(nextAction));
    setMessage("");
  };

  const refresh = async () => {
    await inspectionQuery.refetch();
    onChanged();
  };

  const diagnoseWithCodex = async () => {
    if (!selectedJob) return;
    setAgentBusy(true);
    setMessage("");
    try {
      const result = await diagnoseRunWithCodex(
        selectedJob.id,
        selectedFindings.filter((finding) => finding.blocking && !finding.resolved_at).map((finding) => finding.id),
      );
      setAgentSession(result);
      setMessage(
        result.status === "completed"
          ? "Codex 已提交 Controller 白名单动作。"
          : `Codex 已降级为人工处理：${result.stop_reason ?? result.diagnostic_reason ?? "需要人工决定"}`,
      );
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Codex 诊断不可用，已保留人工处理入口。");
    } finally {
      setAgentBusy(false);
    }
  };

  const operationKeys = useRef(new Map<string, string>());
  const submitRemediation = async () => {
    if (!selectedJob) return;
    setBusy(true);
    setMessage("");
    try {
      const findingIds = selectedFindings
        .filter((finding) => finding.blocking && !finding.resolved_at)
        .map((finding) => finding.id);
      const paid = action === "regenerate" || action === "image_edit";
      const fingerprint = JSON.stringify([selectedJob.id, selectedJob.attempt_count, action, strategy, reason.trim(), promptAdjustment.trim(), findingIds]);
      if (!operationKeys.current.has(fingerprint)) operationKeys.current.set(fingerprint, crypto.randomUUID());
      await createRunRemediation(selectedJob.id, {
        idempotency_key: operationKeys.current.get(fingerprint),
        action,
        strategy,
        reason: reason.trim(),
        parameters: promptAdjustment.trim() ? { prompt_suffix: promptAdjustment.trim() } : {},
        finding_ids: findingIds,
        expected_additional_calls: paid ? 1 : 0,
      });
      setMessage(paid ? "付费返工已预留预算并进入队列。" : "动作已写入事件流并进入确定性队列。");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "返工动作提交失败。");
    } finally {
      setBusy(false);
    }
  };

  const saveBudget = async () => {
    if (!planId) return;
    setBusy(true);
    setMessage("");
    try {
      await updateGenerationBudget(planId, budgetValue);
      setMessage("额外调用预算已更新并记录事件。");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "预算更新失败。");
    } finally {
      setBusy(false);
    }
  };

  const resumePlan = async () => {
    if (!planId) return;
    setBusy(true);
    setMessage("");
    try {
      await resumeGenerationPlan(planId);
      setMessage("仅安全可恢复的任务已重新入队；未知交付保持暂停。");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "运行恢复失败。");
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  const canAct = Boolean(
    selectedJob
    && actionableJobStatuses.has(selectedJob.status)
    && reason.trim()
    && (!(action === "regenerate" || action === "image_edit") || promptAdjustment.trim()),
  );
  const paidAction = action === "regenerate" || action === "image_edit";
  const budgetValid = Number.isInteger(budgetValue)
    && budgetValue >= (inspection?.plan.extra_calls_used ?? 0)
    && budgetValue <= 10_000;
  const freeActionDetail = action === "tool_repair"
    ? "Worker 输出会重新执行硬 QA"
    : action === "retry"
      ? "实际供应商请求仍写入调用台账"
      : "停止当前任务，等待明确决定";
  const candidateReady = selectedJob?.status === "candidate_ready";
  const remediationControls = inspection ? <div className="remediation-controls">
    <div className="action-choice-grid">
      {(selectedJob?.task_kind === "text"
        ? ["regenerate", "retry", "await_user"]
        : ["tool_repair", "regenerate", "image_edit", "retry", "await_user"]
      ).map((choice) => <button key={choice} type="button" className={action === choice ? "active" : ""} aria-pressed={action === choice} disabled={choice === "retry" && ["dispatch_started", "legacy_unknown"].includes(selectedJob?.delivery_state ?? "")} onClick={() => chooseAction(choice as RemediationChoice)}>{choice === "tool_repair" ? <Wrench size={15} /> : choice === "retry" ? <ArrowsClockwise size={15} /> : choice === "await_user" ? <WarningCircle size={15} /> : <ImagesSquare size={15} />}{actionLabel(choice as RemediationChoice)}</button>)}
    </div>
    {action === "tool_repair" ? <label className="production-field"><span>已注册 Worker</span><SelectMenu ariaLabel="已注册 Worker" value={strategy} options={[{ value: "normalize", label: "编码 / 尺寸归一化" }, { value: "color_key", label: "角点色键移除" }, { value: "smart_matte", label: "智能抠图适配" }, { value: "edge_cleanup", label: "边缘清理" }]} onChange={setStrategy} /></label> : <label className="production-field"><span>策略记录</span><input value={strategy} onChange={(event) => setStrategy(event.target.value)} /></label>}
    {(action === "regenerate" || action === "image_edit") ? <label className="production-field"><span>本轮必须改变的生成约束</span><textarea rows={3} value={promptAdjustment} onChange={(event) => setPromptAdjustment(event.target.value)} placeholder="例如：保持人物身份，改为正面半身构图，移除背景文字。" /></label> : null}
    <label className="production-field"><span>判断依据</span><textarea rows={4} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="引用证据并说明为什么选择该动作。" /></label>
    <div className={`action-impact ${paidAction ? "paid" : "free"}`}><CurrencyDollar size={17} /><div><strong>{paidAction ? "预计增加 1 次供应商调用" : "不占付费返工额度"}</strong><small>{paidAction ? `剩余额度 ${Math.max(0, inspection.plan.extra_call_budget - inspection.plan.extra_calls_used)} 次` : freeActionDetail}</small></div></div>
    <button className="button primary remediation-submit" type="button" onClick={submitRemediation} disabled={busy || !canAct}>{busy ? "正在写入…" : "记录并执行动作"}</button>
  </div> : null;

  return (
    <div className="drawer-backdrop production-drawer-backdrop" onMouseDown={onClose}>
      <aside
        ref={dialogRef}
        tabIndex={-1}
        className="production-drawer run-inspector-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="run-inspector-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="drawer-header production-drawer-header run-inspector-header">
          <div>
            <span className="section-kicker">持久事件流 · {streamConnected ? "实时连接" : "轮询兜底"}</span>
            <h2 id="run-inspector-title">{inspection?.plan.name ?? "运行检查器"}</h2>
          </div>
          {inspection ? <span className={`run-state-badge ${inspection.plan.status}`}>{statusLabels[inspection.plan.status] ?? inspection.plan.status}</span> : null}
          <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭运行检查器"><X size={19} /></button>
        </header>

        {inspectionQuery.isLoading ? (
          <div className="production-loading"><ArrowsClockwise size={28} /> 正在读取持久运行状态…</div>
        ) : inspectionQuery.isError || !inspection ? (
          <div className="production-loading error-state"><WarningCircle size={28} /><strong>无法读取运行状态</strong><p>{inspectionQuery.error instanceof Error ? inspectionQuery.error.message : "运行不存在或本地服务不可用。"}</p><button className="button secondary" type="button" onClick={() => void inspectionQuery.refetch()}>重试</button></div>
        ) : (
          <div className="run-inspector-body">
            <section className="run-ledger" aria-label="运行预算">
              <div><CurrencyDollar size={17} /><span>实际 / 基础调用</span><strong>{inspection.plan.actual_calls} / {inspection.plan.estimated_calls}</strong></div>
              <div><span>额外返工</span><strong>{inspection.plan.extra_calls_used} / {inspection.plan.extra_call_budget}</strong></div>
              <div><span>费用估算</span><strong>{formatCost((!inspection.attempts.length || inspection.attempts.some(a => a.estimated_cost == null)) ? null : inspection.plan.actual_cost)}</strong></div>
              <div><Heartbeat size={17} /><span>计划并发</span><strong>{inspection.plan.max_concurrency}</strong></div>
              <label><span>调整额外额度</span><input aria-label="额外调用预算" type="number" min={inspection.plan.extra_calls_used} value={budgetValue} onChange={(event) => setBudgetValue(Number(event.target.value))} /></label>
              <button className="button secondary" type="button" onClick={saveBudget} disabled={busy || !budgetValid || budgetValue === inspection.plan.extra_call_budget}>保存预算</button>
              <button className="button secondary" type="button" onClick={resumePlan} disabled={busy}><Play size={15} /> 安全恢复</button>
            </section>

            <div className="run-work-area">
              <nav className="run-job-rail" aria-label="计划任务">
                <div className="run-rail-heading"><GitBranch size={16} /><strong>任务 DAG</strong><span>{inspection.jobs.length}</span></div>
                <div className="run-job-list">
                  {inspection.jobs.map((job, index) => (
                    <button key={job.id} className={job.id === selectedJobId ? "active" : ""} type="button" onClick={() => setSelectedJobId(job.id)}>
                      <span className="job-causal-number">{String(index + 1).padStart(2, "0")}</span>
                      <span><strong>{job.task_id}</strong><small>{statusLabels[job.status] ?? job.status} · {stageLabels[job.stage] ?? job.stage}</small><em>{frozenProviderName(job)} / {frozenModel(job)}</em></span>
                      {job.status === "awaiting_user" ? <WarningCircle size={16} weight="fill" /> : job.status === "candidate_ready" ? <CheckCircle size={16} weight="fill" /> : <ClockCountdown size={16} />}
                    </button>
                  ))}
                </div>
                <div className="manual-fallback-note"><Wrench size={16} /><p><strong>无需 Codex 也可处理</strong>证据、停止原因、预算和所有白名单动作均可在此人工检查与选择。</p></div>
                <button className="button secondary agent-diagnose-button" type="button" onClick={diagnoseWithCodex} disabled={agentBusy || !selectedJob || !actionableJobStatuses.has(selectedJob.status)}>
                  {agentBusy ? "正在请求 Codex…" : "请求 Codex 诊断"}
                </button>
                {agentSession ? <div className={`agent-session-note ${agentSession.status}`}><strong>Agent {agentSession.status}</strong><small>{agentSession.stop_reason ?? agentSession.diagnostic_reason ?? `上下文 ${agentSession.context_hash.slice(0, 12)}`}</small></div> : null}
              </nav>

              <section className="run-detail-pane">
                <div className="run-tabs" role="tablist" aria-label="运行详情">
                  <button type="button" role="tab" aria-selected={tab === "pipeline"} className={tab === "pipeline" ? "active" : ""} onClick={() => setTab("pipeline")}><Path size={16} /> 流水线</button>
                  <button type="button" role="tab" aria-selected={tab === "evidence"} className={tab === "evidence" ? "active" : ""} onClick={() => setTab("evidence")}><ImagesSquare size={16} /> 证据 {selectedEvidence.length}</button>
                  <button type="button" role="tab" aria-selected={tab === "events"} className={tab === "events" ? "active" : ""} onClick={() => setTab("events")}><ListBullets size={16} /> 事件 {inspection.events.length}</button>
                </div>

                {tab === "pipeline" && selectedJob ? (
                  <div className="run-tab-scroll">
                    <section className="frozen-provider-route">
                      <span><strong>{frozenProviderName(selectedJob)}</strong><small>冻结供应商 · {selectedJob.provider_profile_id}</small></span>
                      <b>{frozenModel(selectedJob)}</b>
                    </section>
                    <section className="stage-sequence">
                      <div className="run-section-heading"><span>阶段状态</span><small>{candidateReady ? "生成和 QA 已完成，等待人工批准" : selectedJob.blocking_reason ?? selectedJob.error_message ?? "当前没有停止原因"}</small></div>
                      <ol>{stages.map((stage, index) => {
                        const currentIndex = stages.indexOf(selectedJob.stage as typeof stages[number]);
                        const completed = currentIndex > index || selectedJob.status === "candidate_ready";
                        const active = selectedJob.stage === stage;
                        return <li key={stage} className={`${completed ? "completed" : ""} ${active ? "active" : ""}`}><span>{completed ? <CheckCircle size={16} weight="fill" /> : index + 1}</span><strong>{stageLabels[stage]}</strong></li>;
                      })}</ol>
                    </section>
                    <section className="attempt-register">
                      <div className="run-section-heading"><span>Attempt 台账</span><small>每次实际供应商请求均独立计数</small></div>
                      {selectedAttempts.length === 0 ? <p className="run-empty-copy">尚无 Attempt。</p> : <ol>{selectedAttempts.map((attempt) => <li key={attempt.id}><span className={attempt.dispatch_state === "not_sent" ? "not-sent" : attempt.billable ? "billable" : "worker"}>{attempt.dispatch_state === "not_sent" ? "未发送 · 未计费" : attempt.dispatch_state === "response_received" ? "已收到结果" : attempt.billable ? "已发送" : "Worker"}</span><div><strong>{attemptTitle(attempt.purpose, attempt.phase, attempt.dispatch_state)}</strong><em>{frozenProviderName(selectedJob)} / {frozenModel(selectedJob)}</em><small>{attempt.dispatch_state === "dispatch_started" ? "交付状态未知，需要核对" : attempt.dispatch_state === "response_received" ? "供应商已响应，本次调用已计入台账" : attempt.dispatch_state === "not_sent" ? "本地准备失败，未调用供应商" : "历史发送状态未确认"}</small>{attempt.network_policy && <small>网络策略：{networkPolicyLabel(attempt.network_policy)}</small>}{(attempt.error_message || attempt.error_hint) && <details className="attempt-error-details"><summary>查看历史错误</summary>{attempt.error_message && <small>{attempt.error_message}</small>}{attempt.error_hint && <small>{attempt.error_hint}</small>}</details>}<small>{attempt.request_id ?? attempt.idempotency_key ?? "无供应商请求 ID"}</small></div><div><strong>{attempt.dispatch_state === "not_sent" ? "未计费" : formatCost(attempt.estimated_cost)}</strong><small>{formatTime(attempt.completed_at ?? attempt.started_at)}</small></div></li>)}</ol>}
                    </section>
                    <section className="finding-register">
                      <div className="run-section-heading"><span>Finding</span><small>稳定 code 决定策略切换与停止</small></div>
                      {selectedFindings.length === 0 ? <p className="run-empty-copy">没有记录阻塞 Finding。</p> : <ol>{selectedFindings.map((finding) => <li key={finding.id} className={finding.resolved_at ? "resolved" : "blocking"}><WarningCircle size={16} weight="fill" /><div><strong>{finding.code}</strong><small>第 {finding.occurrence} 次 · 建议 {finding.suggested_action}</small></div><span>{finding.resolved_at ? "已解决" : "阻塞"}</span></li>)}</ol>}
                    </section>
                  </div>
                ) : null}

                {tab === "evidence" ? (
                  <div className="run-tab-scroll evidence-board">
                    <div className="run-section-heading"><span>人工证据板</span><small>联系表与对比图均由确定性 Worker 生成</small></div>
                    {selectedEvidence.length === 0 ? <p className="run-empty-copy">当前任务还没有证据。</p> : <div className="evidence-grid">{selectedEvidence.map((item) => <article key={item.id} className={item.path ? "" : "evidence-unavailable"}>{item.path ? <img src={runEvidenceUrl(item.id)} alt={evidenceLabel(item)} loading="lazy" /> : <WarningCircle size={24} />}<div><strong>{evidenceLabel(item)}</strong>{evidenceDescription(item) && <p>{evidenceDescription(item)}</p>}<small>{item.sha256 ? item.sha256.slice(0, 12) : String(item.metadata.reason ?? "不适用")}</small></div></article>)}</div>}
                  </div>
                ) : null}

                {tab === "events" ? (
                  <div className="run-tab-scroll event-timeline">
                    <div className="run-section-heading"><span>持久事件</span><small>sequence 游标单调递增</small></div>
                    <ol>{[...inspection.events].reverse().map((event) => {
                      const eventJob = inspection.jobs.find((job) => job.id === event.job_id);
                      const eventModel = typeof event.data.model === "string" ? event.data.model : eventJob ? frozenModel(eventJob) : "";
                      return <li key={event.id}><span>{event.sequence}</span><div><strong>{eventLabels[event.event_type] ?? event.event_type}</strong><small>{event.stage ? `${stageLabels[event.stage] ?? event.stage} · ` : ""}{formatTime(event.created_at)}</small>{eventJob ? <em>{frozenProviderName(eventJob)}{eventModel ? ` / ${eventModel}` : ""}</em> : null}<p>{Object.keys(event.data).length ? JSON.stringify(event.data) : "无附加数据"}</p></div></li>;
                    })}</ol>
                  </div>
                ) : null}
              </section>

              <aside className="remediation-console" aria-label="返工动作">
                <div className="run-section-heading"><span>{candidateReady ? "下一步" : "处理任务"}</span><small>{selectedJob ? `${selectedJob.task_id} · ${statusLabels[selectedJob.status] ?? selectedJob.status}` : "选择任务"}</small></div>
                {candidateReady ? <>
                  <div className="candidate-ready-summary"><CheckCircle size={22} weight="fill" /><div><strong>生成与 QA 已完成</strong><p>候选尚未批准。请回到结果栏前往资产库审核；只有确实需要修改时才创建返工。</p></div></div>
                  <details className="candidate-remediation-options"><summary>需要调整候选</summary>{remediationControls}</details>
                </> : remediationControls}
              </aside>
            </div>
          </div>
        )}

        <footer className="drawer-footer production-drawer-footer run-inspector-footer">
          {message ? <div className="production-message" role="status"><CheckCircle size={15} /> {message}</div> : <div className="production-status"><Heartbeat size={15} /> lease、Attempt、Finding 和预算均已持久化。</div>}
          <div className="drawer-spacer" />
          {inspection && activePlanStatuses.has(inspection.plan.status) ? <span className="live-run-copy"><span /> Controller 正在运行</span> : null}
          <button className="button secondary" type="button" onClick={onClose}>关闭</button>
        </footer>
      </aside>
    </div>
  );
}
