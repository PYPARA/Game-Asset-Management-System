import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchWorkbench, previewEmperorImport, submitReview } from "../lib/api";

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

  it("严格区分待审候选与已批准修订且不混入 demo 修订", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/projects") {
          return json([{ id: "project-1", name: "真实项目", root_path: "/tmp/project", asset_count: 1 }]);
        }
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
    expect(result.source).toBe("api");
    expect(result.assets).toHaveLength(1);
    expect(result.assets[0]).toMatchObject({
      candidateRevisionId: "revision-candidate",
      approvedRevisionId: "revision-approved",
      reviewStatus: "pending",
      reviewReady: false,
      reviewBlockReason: "结构化文本 diff 审核尚未接入，当前禁止直接批准。",
      prompt: "真实候选",
    });
    expect(result.assets[0].revisions.map((revision) => revision.label)).toEqual([
      "r02（候选）",
      "r01（已批准）",
    ]);
    expect(result.assets[0].thumbnails).toEqual([]);
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

  it("导入预览失败时不会伪造 186 项成功报告", async () => {
    vi.stubGlobal("fetch", vi.fn(() => json({ detail: "project path does not exist" }, 404)));
    await expect(previewEmperorImport("/missing")).rejects.toThrow("project path does not exist");
  });
});
