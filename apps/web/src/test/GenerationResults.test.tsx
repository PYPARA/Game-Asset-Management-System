import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { GenerationResults } from "../components/GenerationResults";
import type { GenerationBatch, GameAsset } from "../types";

vi.mock("../components/RunInspectorDrawer", () => ({RunInspectorDrawer: () => null}));
const batch = {plan_id:"plan", draft_version:6} as GenerationBatch;
const asset = {id:"asset", name:"林月"} as GameAsset;
afterEach(()=>vi.unstubAllGlobals());

it("暂停原因可见、没有产物不显示审核、超时重试沿用操作键", async()=>{
  const posts: unknown[] = [];
  const fetcher = vi.fn(async (_input: unknown, init?: RequestInit)=>{
    if (init?.method === "POST") {
      posts.push(JSON.parse(String(init.body)));
      throw new Error("连接超时");
    }
    return new Response(JSON.stringify({jobs:[{id:"job", request:{asset_id:"asset"}, task_id:"portrait",
      status:"awaiting_user", stage:"queued", progress:0, attempt_count:1, provider_snapshot:{},
      blocking_reason:"请求未发出：供应商地址检查未通过", recovery_eligible:true}], evidence:[], findings:[]}), {status:200});
  });
  vi.stubGlobal("fetch", fetcher);
  const client = new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
  render(<QueryClientProvider client={client}><GenerationResults batches={[batch]} assets={[asset]} onOpenAsset={vi.fn()} /></QueryClientProvider>);
  expect(await screen.findByText("请求未发出：供应商地址检查未通过")).toBeInTheDocument();
  expect(screen.queryByRole("button", {name:"在资产库审核"})).not.toBeInTheDocument();
  expect(screen.queryByText("排队中")).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", {name:"继续执行已确认任务"}));
  await waitFor(()=>expect(posts).toHaveLength(1));
  await user.click(await screen.findByRole("button", {name:"继续执行已确认任务"}));
  await waitFor(()=>expect(posts).toHaveLength(2));
  expect(posts[0]).toEqual(posts[1]);
  expect(posts[0]).toMatchObject({idempotency_key:"resume:job:1",action:"retry",expected_additional_calls:0});
});

it("候选就绪明确显示为生成成功，并且结果栏只展示候选而不混入诊断图", async()=>{
  const fetcher = vi.fn(async ()=>new Response(JSON.stringify({
    jobs:[{id:"job", request:{asset_id:"asset"}, task_id:"portrait", status:"candidate_ready",
      stage:"candidate_ready", progress:100, attempt_count:2, result_revision_id:"revision-candidate",
      provider_snapshot:{model:"gpt-image-2.5"}, blocking_reason:null, recovery_eligible:false}],
    evidence:[
      {id:"candidate", job_id:"job", kind:"candidate", label:"当前候选", path:"candidate.webp", media_type:"image/webp"},
      {id:"overlay", job_id:"job", kind:"overlay", label:"50% 叠加", path:"overlay.webp", media_type:"image/webp"},
      {id:"difference", job_id:"job", kind:"difference", label:"像素差异", path:"difference.webp", media_type:"image/webp"},
    ],
    findings:[],
  }), {status:200}));
  vi.stubGlobal("fetch", fetcher);
  const client = new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
  render(<QueryClientProvider client={client}><GenerationResults batches={[batch]} assets={[asset]} onOpenAsset={vi.fn()} /></QueryClientProvider>);

  expect((await screen.findAllByText("生成成功 · 待批准")).length).toBeGreaterThan(0);
  expect(screen.getByText("生成与 QA 已完成")).toBeInTheDocument();
  expect(screen.getByRole("img", {name:"林月 当前候选"})).toBeInTheDocument();
  expect(screen.queryByRole("img", {name:"50% 叠加"})).not.toBeInTheDocument();
  expect(screen.queryByRole("img", {name:"像素差异"})).not.toBeInTheDocument();
  expect(screen.getByRole("button", {name:"查看候选大图与版本"})).toBeEnabled();
  expect(screen.getByRole("button", {name:"前往资产库批准候选"})).toBeEnabled();
  expect(screen.getByRole("button", {name:"查看运行详情与 QA 证据"})).toBeEnabled();
});
