import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectDialog } from "../components/ProjectDialog";

describe("ProjectDialog", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("只在 Game-Projects 中创建标准 Project", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ projects_root: "/projects" }), { status: 200 })));
    render(<ProjectDialog open onClose={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "新建 Project" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("例如 Emperor-Simulator")).toBeVisible();
    expect(screen.getByRole("button", { name: "创建并扫描" })).toBeDisabled();
  });

  it("可以修改已导入项目的名称而不修改目录", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ id: "imported-1", name: "新项目名", root_path: "/projects/imported" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(
      <ProjectDialog
        open
        project={{ id: "imported-1", name: "旧项目名", path: "/projects/imported", assetCount: 0, thumbnail: "" }}
        onClose={vi.fn()}
        onSaved={onSaved}
      />,
    );

    expect(screen.getByRole("heading", { name: "编辑项目" })).toBeInTheDocument();
    expect(screen.queryByText("目录名称")).not.toBeInTheDocument();
    const input = screen.getByRole("textbox", { name: "项目名称" });
    await user.clear(input);
    await user.type(input, "新项目名");
    await user.click(screen.getByRole("button", { name: "保存名称" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/imported-1",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ name: "新项目名" }) }),
    );
    expect(await screen.findByText(/项目名称已保存/)).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalledOnce();
  });
});
