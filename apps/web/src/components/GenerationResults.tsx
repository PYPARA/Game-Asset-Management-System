import { SelectMenu } from "./SelectMenu";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle } from "@phosphor-icons/react";
import { createRunRemediation, fetchRunInspection, runEvidenceUrl } from "../lib/api";
import type { GenerationBatch, GameAsset } from "../types";
import { GenerationAssetPreview } from "./GenerationAssetPreview";
import { RunInspectorDrawer } from "./RunInspectorDrawer";
const labels: Record<string,string> = { queued:"排队中",running:"生成中",confirmed:"已确认",hard_qa:"质量检查",candidate_ready:"生成成功 · 待批准",succeeded:"已完成",completed:"已完成",failed:"失败",qa_failed:"QA 未通过",awaiting_user:"执行已暂停",credentials_locked:"等待解锁凭据",output_received:"产物已接收",cancelled:"已取消" };
export function GenerationResults({ batches, assets, onOpenAsset }: { batches: GenerationBatch[]; assets: GameAsset[]; onOpenAsset?: (id:string)=>void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const batch = batches.find(x=>x.plan_id === selected) ?? batches.at(-1);
  const query = useQuery({ queryKey:["generation-result",batch?.plan_id], queryFn:()=>fetchRunInspection(batch!.plan_id), enabled:!!batch, refetchInterval:3000 });
  const [inspect, setInspect] = useState(false);
  const [recovering, setRecovering] = useState<string | null>(null);
  const [recoveryError, setRecoveryError] = useState("");
  const recover = async (jobId: string, attemptCount: number) => {
    if (recovering) return;
    setRecovering(jobId);
    setRecoveryError("");
    try {
      await createRunRemediation(jobId, {idempotency_key:`resume:${jobId}:${attemptCount}`, action:"retry", strategy:"same-request",
        reason:"继续执行已确认任务", parameters:{}, finding_ids:[], expected_additional_calls:0});
      await query.refetch();
    } catch (error) { setRecoveryError(error instanceof Error ? error.message : "恢复失败，请重试。"); }
    finally { setRecovering(null); }
  };
  const [preview, setPreview] = useState<{asset:GameAsset; revisionId:string|null}|null>(null);
  if (!batch) return <div className="generation-stage-empty"><h3>生成结果会保留在这里</h3><p>方案确认后可以查看进度、候选产物与质量检查。同一会话的每次生成分别保留。</p></div>;
  const hasFailedJobs = query.data?.jobs.some(job => ["failed", "qa_failed", "awaiting_user", "credentials_locked"].includes(job.status)) ?? false;
  return <div className="generation-results">
    <SelectMenu ariaLabel="执行批次" value={batch.plan_id} onChange={setSelected} options={batches.map((x,i)=>({value:x.plan_id,label:`第 ${i+1} 批 · 方案 v${x.draft_version}`}))} />
    {recoveryError && <p role="alert">{recoveryError}</p>}
    {query.isError && <p role="alert">暂时无法读取执行状态，正在重新连接。</p>}
    {query.isLoading && <p role="status">正在读取执行进度…</p>}
    {query.data?.jobs.map(job=>{
      const asset=assets.find(x=>x.id===job.request.asset_id);
      const approved = asset?.reviewStatus === "approved" && !asset.candidateRevisionId;
      const candidate=query.data!.evidence.find(e=>e.job_id===job.id && e.kind==="candidate" && e.media_type?.startsWith("image/") && e.path);
      const currentLabel = approved && job.status === "candidate_ready" ? "生成成功 · 已批准" : labels[job.status] ?? job.status;
      return <article className="generation-result-card" key={job.id}><header><strong>{asset?.name ?? job.task_id}</strong><span>{job.blocking_reason && ["queued", "running"].includes(job.status) ? "正在重试" : currentLabel}</span></header><progress max={100} value={job.progress} aria-label={`${job.task_id}进度`} />
        {job.status === "candidate_ready" && <div className="generation-candidate-ready" role="status"><CheckCircle size={20} weight="fill" /><span><strong>{approved ? "生成、QA 与批准均已完成" : "生成与 QA 已完成"}</strong><small>{approved ? "资产已晋升为不可变批准版本，可加入 Release 后执行交付。" : "当前产物是待批准候选；批准后才会进入可发布资产。"}</small></span></div>}
        {candidate && <figure className="generation-candidate-figure"><a href={runEvidenceUrl(candidate.id)} target="_blank" rel="noreferrer"><img loading="lazy" src={runEvidenceUrl(candidate.id)} alt={`${asset?.name ?? job.task_id} 当前候选`} /></a><figcaption>{approved ? "本批候选 · 已批准" : "当前候选 · 待批准"}</figcaption></figure>}
        {(job.blocking_reason || job.error_message) && <p role="alert">{job.blocking_reason || job.error_message}</p>}
        {job.recovery_eligible && <button type="button" className="button primary" disabled={!!recovering} onClick={()=>void recover(job.id, job.attempt_count)}>{recovering === job.id ? "正在恢复…" : "继续执行已确认任务"}</button>}
        <div className="generation-result-actions">
          {job.result_revision_id && asset && <button type="button" className="button secondary" onClick={()=>setPreview({asset,revisionId:job.result_revision_id})}>查看候选大图与版本</button>}
          {job.result_revision_id && asset && onOpenAsset && <button className="button primary" type="button" onClick={()=>onOpenAsset(asset.id)}>{approved ? "查看已批准资产" : "前往资产库批准候选"}</button>}
        </div>
        <details><summary>生成与 QA 详情</summary><p>{String(job.provider_snapshot.model ?? job.request.model ?? "")} · 尝试 {job.attempt_count} 次</p><p>{labels[["awaiting_user", "failed", "credentials_locked"].includes(job.status) ? job.status : job.stage] ?? job.stage}</p>{query.data!.findings.filter(x=>x.job_id===job.id).map(x=><p key={x.id}>{x.code}</p>)}</details>
      </article>;
    })}
    <button className="button secondary" type="button" onClick={()=>setInspect(true)}>{hasFailedJobs ? "检查运行与处理失败任务" : "查看运行详情与 QA 证据"}</button>
    <p className="generation-muted-copy">继续在对话中描述修改，检查下一版方案后再生成。批准与发布仍在资产库完成。</p>
    <RunInspectorDrawer open={inspect} planId={batch.plan_id} onClose={()=>setInspect(false)} onChanged={()=>void query.refetch()} />
    {preview && <GenerationAssetPreview {...preview} context="candidate" onClose={()=>setPreview(null)} />}
  </div>;
}
