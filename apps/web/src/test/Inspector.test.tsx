import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AssetEditorDrawer } from "../components/AssetEditorDrawer";
import { Inspector } from "../components/Inspector";
import type { GameAsset } from "../types";

function asset(overrides: Partial<GameAsset> = {}): GameAsset {
  return {
    id: "asset-1",
    candidateRevisionId: "revision-1",
    reviewReady: true,
    detailsLoaded: true,
    key: "content.story-arc.test",
    name: "玉玺之晨",
    kind: "content",
    category: "content",
    subtype: "story_arc",
    subtypeLabel: "故事弧",
    tags: [],
    reviewStatus: "pending",
    productionStage: "imported",
    timeAccuracy: "unknown",
    preview: { kind: "content", label: "故事弧", meta: "朝堂", summary: "新帝在朝会上完成权力交接。" },
    qaPassed: 0,
    qaTotal: 0,
    updatedAt: "",
    updatedLabel: "历史时间未知",
    updatedBy: "历史导入",
    thumbnails: [],
    linkedMedia: [],
    relatedAssets: [],
    revisionFormat: "json",
    revisionContent: { id: "test", title: "玉玺之晨", synopsis: "新帝在朝会上完成权力交接。", participants: [] },
    revisions: [{ id: "revision-1", sequence: 1, label: "r01（候选）", image: "", format: "json", content: { id: "test" }, createdAt: "历史时间未知", author: "本机用户", resolution: "—", fileSize: "—", status: "candidate" }],
    prompt: "",
    negativePrompt: "",
    model: "unknown",
    recipe: "unknown",
    seed: "unknown",
    qa: [],
    ...overrides,
  };
}

const noop = () => undefined;

describe("类型化资产检查器", () => {
  it("叙事资产显示案卷内容并提供编辑入口", async () => {
    const onEdit = vi.fn();
    const content = asset();
    render(<Inspector asset={content} allAssets={[content]} onClose={noop} onEdit={onEdit} onNavigate={noop} onReview={noop} />);
    expect(screen.getByText("新帝在朝会上完成权力交接。", { selector: ".document-text-value" })).toBeInTheDocument();
    expect(screen.getAllByText("朝堂")).not.toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: /编辑内容/ }));
    expect(onEdit).toHaveBeenCalledWith(content);
  });

  it("角色右侧展示全部关联立绘并优先中立图", async () => {
    const character = asset({
      id: "character-1",
      key: "entity.character.hero",
      name: "姬宁",
      kind: "entity",
      category: "characters",
      subtype: "character",
      subtypeLabel: "角色",
      preview: { kind: "image", images: ["/neutral.webp", "/angry.webp", "/sad.webp"], label: "关联立绘" },
      revisionContent: { id: "hero", name: "姬宁" },
      linkedMedia: [
        { assetId: "p0", key: "portrait.hero.neutral", name: "姬宁·中立", subtype: "portrait", images: ["/neutral.webp"] },
        { assetId: "p1", key: "portrait.hero.angered", name: "姬宁·震怒", subtype: "portrait", images: ["/angry.webp"] },
        { assetId: "p2", key: "portrait.hero.sorrowful", name: "姬宁·悲恸", subtype: "portrait", images: ["/sad.webp"] },
        { assetId: "p3", key: "portrait.hero.ill", name: "姬宁·病重", subtype: "portrait", images: ["/ill.webp"] },
      ],
    });
    render(<Inspector asset={character} allAssets={[character]} onClose={noop} onEdit={noop} onNavigate={noop} onReview={noop} />);
    expect(screen.getByTitle("portrait.hero.neutral")).toBeInTheDocument();
    expect(screen.getByTitle("portrait.hero.angered")).toBeInTheDocument();
    expect(screen.getByTitle("portrait.hero.sorrowful")).toBeInTheDocument();
    expect(screen.getByTitle("portrait.hero.ill")).toBeInTheDocument();
    expect(screen.getByAltText("姬宁·中立")).toHaveAttribute("src", "/neutral.webp");
    await userEvent.click(screen.getByTitle("portrait.hero.ill"));
    expect(screen.getByAltText("姬宁·病重")).toHaveAttribute("src", "/ill.webp");
  });

  it("宽编辑抽屉校验只读 ID，并保存新的候选内容", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const content = asset();
    render(<AssetEditorDrawer asset={content} allAssets={[content]} onClose={noop} onSave={onSave} />);
    const title = screen.getByDisplayValue("玉玺之晨");
    await userEvent.clear(title);
    await userEvent.type(title, "新标题");
    await userEvent.click(screen.getByRole("button", { name: /保存为候选/ }));
    expect(onSave).toHaveBeenCalledWith(content, expect.objectContaining({ id: "test", title: "新标题" }));
  });
});
