import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NarrativeAtlas } from "../components/NarrativeAtlas";
import type { NarrativeMap, ProjectSummary } from "../types";

const project: ProjectSummary = {
  id: "project-1",
  name: "大周帝国",
  path: "/projects/empire",
  branch: "main",
  assetCount: 4,
  thumbnail: "",
};

const atlas: NarrativeMap = {
  project_id: project.id,
  project_name: project.name,
  chapters: [{
    asset_id: "chapter-1",
    key: "content.chapter.court",
    title: "权谋暗涌",
    order: 3,
    scene_ids: ["scene-1"],
    revision_id: "chapter-revision",
    revision_status: "approved",
  }],
  scenes: [{
    asset_id: "scene-1",
    key: "content.scene.throne",
    title: "朝堂对峙",
    chapter_asset_id: "chapter-1",
    order: 7,
    revision_id: "scene-revision",
    revision_status: "approved",
    content: {
      title: "朝堂对峙",
      summary: "边关捷报传来，主战派要求趁胜追击。",
      scene_type: "剧情场景",
      time: "建昭十二年·秋",
      location: "宣德殿",
      dialogue: ["陛下，敌军已溃。"],
    },
    requirements: [{
      id: "requirement-missing",
      asset_key: "media.cg.throne",
      asset_id: null,
      title: "朝堂对峙 CG",
      kind: "media",
      subtype: "cg",
      role: "CG 画面",
      prompt: "生成朝堂场景",
      width: 1024,
      height: 1024,
      transparent: false,
      target_path: null,
      order: 0,
      status: "missing",
      rendition_id: null,
    }],
    coverage: { required: 1, ready: 0, candidate: 0, planned: 0, missing: 1, ratio: 0 },
    graph: {
      nodes: [{
        asset_id: "scene-1",
        key: "content.scene.throne",
        title: "朝堂对峙",
        kind: "content",
        subtype: "scene",
        status: "candidate",
        rendition_id: null,
        role: "scene",
      }],
      edges: [],
    },
  }],
  unassigned_scene_ids: [],
  coverage: { required: 1, ready: 0, candidate: 0, planned: 0, missing: 1, ratio: 0 },
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderAtlas(onOpenProduction = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <NarrativeAtlas project={project} onOpenProduction={onOpenProduction} />
    </QueryClientProvider>,
  );
  return onOpenProduction;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("NarrativeAtlas", () => {
  it("展示章节树、场景关系与缺失覆盖", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(atlas));
    renderAtlas();

    expect(await screen.findByRole("navigation", { name: "章节树" })).toBeInTheDocument();
    expect(screen.getByText(/第 1 章.*权谋暗涌/)).toBeInTheDocument();
    expect(screen.getAllByText("朝堂对峙").length).toBeGreaterThan(0);
    expect(screen.getByText("边关捷报传来，主战派要求趁胜追击。")).toBeInTheDocument();
    expect(screen.getByText("朝堂对峙 CG")).toBeInTheDocument();
    expect(screen.getByText("缺失（1）")).toBeInTheDocument();
  });

  it("把场景编辑保存为普通候选修订", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/revisions") && init?.method === "POST") return json({ id: "scene-revision-2" }, 201);
      return json(atlas);
    });
    const user = userEvent.setup();
    renderAtlas();

    await user.click(await screen.findByRole("button", { name: "编辑当前场景" }));
    const title = screen.getByLabelText("场景标题");
    await user.clear(title);
    await user.type(title, "朝堂决断");
    await user.click(screen.getByRole("button", { name: "保存为候选" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/revisions",
      expect.objectContaining({ method: "POST" }),
    ));
    const request = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/revisions"));
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({
      asset_id: "scene-1",
      parent_revision_id: "scene-revision",
      content: { title: "朝堂决断" },
    });
  });

  it("将缺失项物化为 Catalog 资产后交给现有计划编辑器", async () => {
    const onOpenProduction = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (String(input).endsWith("/requirements") && init?.method === "POST") {
        return json({
          project_id: project.id,
          scene_asset_id: "scene-1",
          asset_ids: ["asset-cg"],
          created_asset_ids: ["asset-cg"],
          requirement_ids: ["requirement-missing"],
        });
      }
      return json(atlas);
    });
    const user = userEvent.setup();
    renderAtlas(onOpenProduction);

    await user.click(await screen.findByRole("button", { name: /预览生成方案/ }));
    expect(screen.getByRole("dialog", { name: "缺失资产生产方案" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "进入预算确认" }));

    await waitFor(() => expect(onOpenProduction).toHaveBeenCalledWith(["asset-cg"]));
  });
});
