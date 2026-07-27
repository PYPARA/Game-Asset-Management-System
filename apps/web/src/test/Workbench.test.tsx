import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Workbench } from "../Workbench";

function json(payload: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function mockAssetApi() {
  const assets = [
    { id: "asset-shen", key: "portrait.shen-yan.neutral", title: "沈渊（文官）· 中立姿态" },
    { id: "asset-han", key: "portrait.han-lie.resolute", title: "韩烈（大将军）· 坚毅" },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
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
      if (url.startsWith("/api/jobs?")) return json([]);
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
    }),
  );
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
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(screen.getByRole("button", { name: "供应商设置" }));

    expect(screen.getByRole("dialog", { name: "连接与凭据" })).toBeInTheDocument();
    expect(screen.getByText("本地持久凭据的边界")).toBeInTheDocument();
    expect(screen.getByText(/无法抵御同源脚本注入/)).toBeInTheDocument();
  });
});
