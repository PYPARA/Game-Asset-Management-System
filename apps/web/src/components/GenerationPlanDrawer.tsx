import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Calculator,
  CaretRight,
  CheckCircle,
  GitBranch,
  Plus,
  ShieldCheck,
  Trash,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import {
  createAndConfirmGenerationPlan,
  fetchGenerationProviders,
} from "../lib/api";
import { useModalFocus } from "../hooks/useModalFocus";
import type {
  GameAsset,
  GenerationPlanInput,
  GenerationProviderProfile,
  GenerationTaskInput,
  GenerationTaskKind,
  ProjectSummary,
} from "../types";

interface TaskDraft {
  localId: string;
  taskId: string;
  assetId: string;
  kind: GenerationTaskKind;
  prompt: string;
  width: number;
  height: number;
  maxBytes: string;
  transparent: boolean;
  dependsOn: string[];
  referenceTaskId: string;
  targetPath: string;
  schemaText: string;
}

interface GenerationPlanDrawerProps {
  open: boolean;
  project: ProjectSummary;
  assets: GameAsset[];
  initialAssetIds: string[];
  onClose: () => void;
  onConfirmed: (planId: string) => void;
}

function safeTaskId(asset: GameAsset, index: number): string {
  const stem = asset.key
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "asset";
  return `produce-${stem}-${index + 1}`;
}

function defaultTask(asset: GameAsset, index: number): TaskDraft {
  const image = asset.kind === "media" || asset.kind === "production";
  return {
    localId: `${asset.id}-${index}-${Date.now()}`,
    taskId: safeTaskId(asset, index),
    assetId: asset.id,
    kind: image ? "image" : "text",
    prompt: image ? `生成 ${asset.name}，遵循当前 Project 的制作规范。` : `生成 ${asset.name} 的结构化内容。`,
    width: 1024,
    height: 1024,
    maxBytes: "",
    transparent: image,
    dependsOn: [],
    referenceTaskId: "",
    targetPath: "",
    schemaText: "",
  };
}

function parseSchema(task: TaskDraft, asset: GameAsset | undefined): Record<string, unknown> | undefined {
  const value = task.schemaText.trim();
  if (!value) {
    if (!asset?.schemaRef) {
      throw new Error(`任务 ${task.taskId} 的文本输出需要内联 JSON Schema 或资产 schema_ref。`);
    }
    return undefined;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`任务 ${task.taskId} 的 JSON Schema 不是有效 JSON。`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`任务 ${task.taskId} 的 JSON Schema 必须是对象。`);
  }
  return parsed as Record<string, unknown>;
}

function assertAcyclic(tasks: TaskDraft[]): void {
  const byId = new Map(tasks.map((task) => [task.taskId, task]));
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (taskId: string) => {
    if (visiting.has(taskId)) throw new Error("任务依赖形成了循环，请移除红色因果链中的闭环。");
    if (visited.has(taskId)) return;
    visiting.add(taskId);
    for (const dependency of byId.get(taskId)?.dependsOn ?? []) visit(dependency);
    visiting.delete(taskId);
    visited.add(taskId);
  };
  for (const task of tasks) visit(task.taskId);
}

function toTaskInput(task: TaskDraft, assets: Map<string, GameAsset>): GenerationTaskInput {
  if (!task.taskId.trim()) throw new Error("每个任务都需要稳定任务 ID。");
  if (!task.prompt.trim()) throw new Error(`任务 ${task.taskId} 的 Prompt 不能为空。`);
  const input: GenerationTaskInput = {
    id: task.taskId.trim(),
    kind: task.kind,
    asset_id: task.assetId,
    prompt: task.prompt.trim(),
    depends_on: task.dependsOn,
  };
  if (task.kind === "text") {
    const schema = parseSchema(task, assets.get(task.assetId));
    if (schema) input.schema = schema;
  } else {
    if (!Number.isInteger(task.width) || task.width < 1 || task.width > 8192) {
      throw new Error(`任务 ${task.taskId} 的宽度必须是 1–8192 的整数。`);
    }
    if (!Number.isInteger(task.height) || task.height < 1 || task.height > 8192) {
      throw new Error(`任务 ${task.taskId} 的高度必须是 1–8192 的整数。`);
    }
    input.width = task.width;
    input.height = task.height;
    input.transparent = task.transparent;
    if (task.maxBytes.trim()) {
      const maxBytes = Number(task.maxBytes);
      if (!Number.isInteger(maxBytes) || maxBytes < 1 || maxBytes > 2_000_000_000) {
        throw new Error(`任务 ${task.taskId} 的最大字节数必须是有效整数。`);
      }
      input.max_bytes = maxBytes;
    }
    if (task.targetPath.trim()) input.target_path = task.targetPath.trim();
    if (task.kind === "image_edit") {
      if (!task.referenceTaskId || !task.dependsOn.includes(task.referenceTaskId)) {
        throw new Error(`图像编辑任务 ${task.taskId} 必须从已勾选的上游任务选择参考图。`);
      }
      input.reference_task_id = task.referenceTaskId;
    }
  }
  return input;
}

function estimateCost(
  tasks: TaskDraft[],
  provider: GenerationProviderProfile | undefined,
): number | null {
  if (!provider?.pricing) return null;
  let total = 0;
  for (const task of tasks) {
    const key = task.kind === "text"
      ? "text_call"
      : task.kind === "image_edit" && provider.pricing.image_edit_call !== undefined
        ? "image_edit_call"
        : "image_call";
    const price = provider.pricing[key];
    if (price === undefined) return null;
    total += price;
  }
  return total;
}

function taskKindLabel(kind: GenerationTaskKind): string {
  if (kind === "text") return "结构化文本";
  if (kind === "image_edit") return "参考图编辑";
  return "图像生成";
}

export function GenerationPlanDrawer({
  open,
  project,
  assets,
  initialAssetIds,
  onClose,
  onConfirmed,
}: GenerationPlanDrawerProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [step, setStep] = useState<"edit" | "review">("edit");
  const [name, setName] = useState("新资产生产计划");
  const [providerId, setProviderId] = useState("");
  const [tasks, setTasks] = useState<TaskDraft[]>([]);
  const [assetToAdd, setAssetToAdd] = useState("");
  const [extraBudget, setExtraBudget] = useState(2);
  const [maxConcurrency, setMaxConcurrency] = useState(3);
  const [maxPaidRounds, setMaxPaidRounds] = useState(2);
  const [transportRetries, setTransportRetries] = useState(2);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const assetsById = useMemo(() => new Map(assets.map((asset) => [asset.id, asset])), [assets]);
  const providersQuery = useQuery({
    queryKey: ["generation-providers"],
    queryFn: fetchGenerationProviders,
    enabled: open,
  });
  const providers = providersQuery.data ?? [];
  const provider = providers.find((item) => item.id === providerId);
  const suggestedExtraCalls = Math.max(2, Math.ceil(tasks.length * 0.2));
  const estimatedCost = estimateCost(tasks, provider);

  useModalFocus({ open, dialogRef, initialFocusRef: closeRef, onClose });

  useEffect(() => {
    if (!open) return;
    const selected = initialAssetIds
      .map((assetId) => assetsById.get(assetId))
      .filter((asset): asset is GameAsset => Boolean(asset));
    setTasks(selected.map(defaultTask));
    setAssetToAdd(assets[0]?.id ?? "");
    setStep("edit");
    setName("新资产生产计划");
    setExtraBudget(Math.max(2, Math.ceil(selected.length * 0.2)));
    setMaxConcurrency(3);
    setMaxPaidRounds(2);
    setTransportRetries(2);
    setConfirmed(false);
    setMessage("");
    // Asset selection is intentionally snapshotted only when the drawer opens.
  }, [open]);

  useEffect(() => {
    if (!open || providers.length === 0) return;
    if (providers.some((item) => item.id === providerId)) return;
    setProviderId(providers[0].id);
    setMaxConcurrency(Math.min(3, providers[0].concurrency));
  }, [open, providerId, providers]);

  const updateTask = (localId: string, patch: Partial<TaskDraft>) => {
    setTasks((current) => current.map((task) => task.localId === localId ? { ...task, ...patch } : task));
    setMessage("");
  };

  const renameTask = (localId: string, nextTaskId: string) => {
    setTasks((current) => {
      const previousTaskId = current.find((task) => task.localId === localId)?.taskId;
      if (previousTaskId === undefined) return current;
      return current.map((task) => task.localId === localId
        ? { ...task, taskId: nextTaskId }
        : {
            ...task,
            dependsOn: task.dependsOn.map((dependency) => dependency === previousTaskId ? nextTaskId : dependency),
            referenceTaskId: task.referenceTaskId === previousTaskId ? nextTaskId : task.referenceTaskId,
          });
    });
    setMessage("");
  };

  const addTask = () => {
    const asset = assetsById.get(assetToAdd);
    if (!asset) return;
    setTasks((current) => [...current, defaultTask(asset, current.length)]);
    setExtraBudget((current) => Math.max(current, Math.max(2, Math.ceil((tasks.length + 1) * 0.2))));
  };

  const removeTask = (localId: string, taskId: string) => {
    setTasks((current) => current
      .filter((task) => task.localId !== localId)
      .map((task) => ({
        ...task,
        dependsOn: task.dependsOn.filter((dependency) => dependency !== taskId),
        referenceTaskId: task.referenceTaskId === taskId ? "" : task.referenceTaskId,
      })));
  };

  const buildPayload = (): GenerationPlanInput => {
    if (!project.id) throw new Error("请先创建或载入 Project。");
    if (!providerId) throw new Error("请先在供应商设置中建立生成配置。");
    if (tasks.length === 0) throw new Error("计划至少需要一个任务。");
    if (!Number.isInteger(extraBudget) || extraBudget < 0 || extraBudget > 10_000) {
      throw new Error("额外调用预算必须是 0–10000 的整数。");
    }
    if (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 32) {
      throw new Error("计划并发必须是 1–32 的整数。");
    }
    if (!Number.isInteger(maxPaidRounds) || maxPaidRounds < 0 || maxPaidRounds > 20) {
      throw new Error("单资产付费轮次必须是 0–20 的整数。");
    }
    if (!Number.isInteger(transportRetries) || transportRetries < 0 || transportRetries > 8) {
      throw new Error("同请求网络重试必须是 0–8 的整数。");
    }
    const taskIds = tasks.map((task) => task.taskId.trim());
    if (new Set(taskIds).size !== taskIds.length) throw new Error("任务 ID 必须唯一。");
    const known = new Set(taskIds);
    for (const task of tasks) {
      if (task.dependsOn.some((dependency) => !known.has(dependency))) {
        throw new Error(`任务 ${task.taskId} 引用了已删除的上游任务。`);
      }
    }
    assertAcyclic(tasks);
    return {
      project_id: project.id,
      provider_profile_id: providerId,
      name: name.trim() || "未命名生成计划",
      tasks: tasks.map((task) => toTaskInput(task, assetsById)),
      extra_call_budget: extraBudget,
      max_paid_remediation_rounds: maxPaidRounds,
      max_transport_retries: transportRetries,
      max_concurrency: Math.min(maxConcurrency, provider?.concurrency ?? maxConcurrency),
    };
  };

  const reviewPlan = () => {
    try {
      buildPayload();
      setConfirmed(false);
      setMessage("");
      setStep("review");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "计划校验失败。");
    }
  };

  const confirmPlan = async () => {
    setBusy(true);
    setMessage("");
    try {
      const result = await createAndConfirmGenerationPlan(buildPayload());
      onConfirmed(result.plan.id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "计划确认失败。");
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  return (
    <div className="drawer-backdrop production-drawer-backdrop" onMouseDown={onClose}>
      <aside
        ref={dialogRef}
        tabIndex={-1}
        className="production-drawer plan-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="generation-plan-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="drawer-header production-drawer-header">
          <div>
            <span className="section-kicker">确定性生产控制器 · M2</span>
            <h2 id="generation-plan-title">{step === "edit" ? "编排生成计划" : "确认调用与预算"}</h2>
          </div>
          <div className="plan-step-indicator" aria-label="计划步骤">
            <span className={step === "edit" ? "active" : "done"}>1 编排</span>
            <CaretRight size={13} />
            <span className={step === "review" ? "active" : ""}>2 确认</span>
          </div>
          <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭生成计划">
            <X size={19} />
          </button>
        </header>

        {step === "edit" ? (
          <div className="production-drawer-body plan-editor-grid">
            <section className="plan-config-rail" aria-label="计划约束">
              <label className="production-field">
                <span>计划名称</span>
                <input value={name} onChange={(event) => setName(event.target.value)} />
              </label>
              <label className="production-field">
                <span>供应商配置</span>
                <select value={providerId} onChange={(event) => setProviderId(event.target.value)}>
                  {providers.length === 0 ? <option value="">尚无供应商配置</option> : null}
                  {providers.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}{item.kind !== "fake" && !item.is_unlocked ? " · 凭据未解锁" : ""}
                    </option>
                  ))}
                </select>
              </label>
              {providersQuery.isError ? (
                <p className="production-inline-error">供应商列表读取失败，请检查本地服务。</p>
              ) : null}
              <div className="constraint-ledger">
                <h3><Calculator size={16} /> 调用账本</h3>
                <label><span>额外调用预算</span><input type="number" min={0} value={extraBudget} onChange={(event) => setExtraBudget(Number(event.target.value))} /></label>
                <small>系统建议 {suggestedExtraCalls} 次；付费返工入队前预留。</small>
                <label><span>计划并发</span><input type="number" min={1} max={provider?.concurrency ?? 32} value={maxConcurrency} onChange={(event) => setMaxConcurrency(Number(event.target.value))} /></label>
                <small>供应商上限 {provider?.concurrency ?? "—"}；实际取两者较小值。</small>
                <label><span>单资产付费轮次</span><input type="number" min={0} max={20} value={maxPaidRounds} onChange={(event) => setMaxPaidRounds(Number(event.target.value))} /></label>
                <label><span>同请求网络重试</span><input type="number" min={0} max={8} value={transportRetries} onChange={(event) => setTransportRetries(Number(event.target.value))} /></label>
              </div>
              <div className="plan-cost-readout">
                <span>基础调用</span><strong>{tasks.length}</strong>
                <span>预计基础成本</span><strong>{estimatedCost === null ? "未提供价格" : estimatedCost.toFixed(2)}</strong>
              </div>
            </section>

            <section className="task-composer" aria-label="任务 DAG">
              <div className="task-composer-heading">
                <div>
                  <span className="section-kicker">红线表示上游产物注入</span>
                  <h3>任务因果链</h3>
                </div>
                <div className="add-task-control">
                  <select aria-label="选择要添加的资产" value={assetToAdd} onChange={(event) => setAssetToAdd(event.target.value)}>
                    {assets.map((asset) => <option key={asset.id} value={asset.id}>{asset.name} · {asset.key}</option>)}
                  </select>
                  <button className="button secondary" type="button" onClick={addTask} disabled={!assetToAdd}><Plus size={15} /> 添加任务</button>
                </div>
              </div>
              {tasks.length === 0 ? (
                <div className="empty-task-plan">
                  <GitBranch size={30} />
                  <strong>计划还没有任务</strong>
                  <p>从上方选择资产。每项任务会冻结 Prompt、依赖、尺寸与预算。</p>
                </div>
              ) : (
                <ol className="task-dag-list">
                  {tasks.map((task, index) => {
                    const asset = assetsById.get(task.assetId);
                    const otherTasks = tasks.filter((candidate) => candidate.localId !== task.localId);
                    return (
                      <li key={task.localId} className="task-dag-card">
                        <div className="task-causal-index"><span>{String(index + 1).padStart(2, "0")}</span></div>
                        <div className="task-card-fields">
                          <div className="task-card-title">
                            <div><strong>{asset?.name ?? "未知资产"}</strong><small>{asset?.key}</small></div>
                            <button className="icon-button" type="button" onClick={() => removeTask(task.localId, task.taskId)} aria-label={`移除任务 ${task.taskId}`}><Trash size={16} /></button>
                          </div>
                          <div className="production-form-grid three">
                            <label className="production-field"><span>任务 ID</span><input value={task.taskId} onChange={(event) => renameTask(task.localId, event.target.value)} /></label>
                            <label className="production-field"><span>目标资产</span><select value={task.assetId} onChange={(event) => updateTask(task.localId, { assetId: event.target.value })}>{assets.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
                            <label className="production-field"><span>任务类型</span><select value={task.kind} onChange={(event) => updateTask(task.localId, { kind: event.target.value as GenerationTaskKind, referenceTaskId: "" })}><option value="text">结构化文本</option><option value="image">图像生成</option><option value="image_edit">参考图编辑</option></select></label>
                          </div>
                          <label className="production-field"><span>Prompt</span><textarea rows={3} value={task.prompt} onChange={(event) => updateTask(task.localId, { prompt: event.target.value })} /></label>
                          {task.kind === "text" ? (
                            <label className="production-field"><span>内联 JSON Schema {asset?.schemaRef ? `· 可留空使用 ${asset.schemaRef}` : "· 必填"}</span><textarea className="schema-input" rows={4} value={task.schemaText} onChange={(event) => updateTask(task.localId, { schemaText: event.target.value })} placeholder={'{"type":"object","required":["name"],"properties":{"name":{"type":"string"}}}'} /></label>
                          ) : (
                            <div className="production-form-grid four">
                              <label className="production-field"><span>宽</span><input type="number" min={1} value={task.width} onChange={(event) => updateTask(task.localId, { width: Number(event.target.value) })} /></label>
                              <label className="production-field"><span>高</span><input type="number" min={1} value={task.height} onChange={(event) => updateTask(task.localId, { height: Number(event.target.value) })} /></label>
                              <label className="production-field"><span>最大字节</span><input type="number" min={1} value={task.maxBytes} onChange={(event) => updateTask(task.localId, { maxBytes: event.target.value })} placeholder="不限制" /></label>
                              <label className="production-check"><input type="checkbox" checked={task.transparent} onChange={(event) => updateTask(task.localId, { transparent: event.target.checked })} /><span>需要透明通道</span></label>
                            </div>
                          )}
                          <fieldset className="dependency-picker">
                            <legend>上游产物注入</legend>
                            {otherTasks.length === 0 ? <small>这是根任务。</small> : otherTasks.map((candidate) => (
                              <label key={candidate.localId}>
                                <input type="checkbox" checked={task.dependsOn.includes(candidate.taskId)} onChange={(event) => updateTask(task.localId, {
                                  dependsOn: event.target.checked ? [...task.dependsOn, candidate.taskId] : task.dependsOn.filter((value) => value !== candidate.taskId),
                                  referenceTaskId: !event.target.checked && task.referenceTaskId === candidate.taskId ? "" : task.referenceTaskId,
                                })} />
                                <span>{candidate.taskId}</span>
                              </label>
                            ))}
                          </fieldset>
                          {task.kind === "image_edit" ? (
                            <label className="production-field"><span>参考图任务</span><select value={task.referenceTaskId} onChange={(event) => updateTask(task.localId, { referenceTaskId: event.target.value })}><option value="">请选择已勾选上游</option>{task.dependsOn.map((dependency) => <option key={dependency} value={dependency}>{dependency}</option>)}</select></label>
                          ) : null}
                        </div>
                      </li>
                    );
                  })}
                </ol>
              )}
            </section>
          </div>
        ) : (
          <div className="production-drawer-body plan-review-layout">
            <section className="plan-review-hero">
              <ShieldCheck size={34} weight="duotone" />
              <div><span className="section-kicker">显式确认</span><h3>{name || "未命名生成计划"}</h3><p>确认后任务进入持久队列。基础调用和付费返工会分别计数，未知交付不会自动重试。</p></div>
            </section>
            <section className="frozen-ledger">
              <div><span>基础调用</span><strong>{tasks.length}</strong><small>计划冻结</small></div>
              <div><span>预计基础成本</span><strong>{estimatedCost === null ? "未知" : estimatedCost.toFixed(2)}</strong><small>{provider?.name ?? "无供应商"}</small></div>
              <div><span>额外调用额度</span><strong>{extraBudget}</strong><small>建议 {suggestedExtraCalls}</small></div>
              <div><span>并发上限</span><strong>{Math.min(maxConcurrency, provider?.concurrency ?? maxConcurrency)}</strong><small>计划 / 供应商双重限制</small></div>
              <div><span>单资产付费轮次</span><strong>{maxPaidRounds}</strong><small>达到即等待人工</small></div>
              <div><span>同请求网络重试</span><strong>{transportRetries}</strong><small>每次 Attempt 留痕</small></div>
            </section>
            <section className="review-dag">
              <h3>冻结任务与真实依赖</h3>
              <ol>{tasks.map((task, index) => <li key={task.localId}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{task.taskId}</strong><small>{taskKindLabel(task.kind)} · {assetsById.get(task.assetId)?.name}</small></div><p>{task.dependsOn.length ? `注入：${task.dependsOn.join("、")}` : "根任务"}</p></li>)}</ol>
            </section>
            <label className="explicit-confirmation"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span><strong>我确认本次基础调用与额外预算</strong><small>供应商返回只进入输出阶段；硬 QA、证据和人工审核仍会独立执行。</small></span></label>
          </div>
        )}

        <footer className="drawer-footer production-drawer-footer">
          {message ? <div className="production-message" role="alert"><WarningCircle size={15} /> {message}</div> : <div className="production-status"><CheckCircle size={15} /> 所有执行动作由本地 Controller 校验并写入事件流。</div>}
          <div className="drawer-spacer" />
          {step === "review" ? <button className="button secondary" type="button" onClick={() => setStep("edit")} disabled={busy}>返回修改</button> : null}
          <button className="button primary" type="button" onClick={step === "edit" ? reviewPlan : confirmPlan} disabled={busy || (step === "review" && !confirmed)}>
            {step === "edit" ? "校验并查看预算" : busy ? "正在确认…" : "确认并启动计划"}
          </button>
        </footer>
      </aside>
    </div>
  );
}
