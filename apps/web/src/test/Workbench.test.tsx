import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Workbench } from "../Workbench";

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

  it("在后端离线时展示 Emperor-Simulator 的真实 demo 台账", async () => {
    renderWorkbench();

    expect((await screen.findAllByText("portrait.shen-yan.neutral")).length).toBeGreaterThan(0);
    expect(screen.getByText("离线演示")).toBeInTheDocument();
    expect(screen.getByText("cg.court-confrontation")).toBeInTheDocument();
  });

  it("按名称与 Key 搜索资产", async () => {
    const user = userEvent.setup();
    renderWorkbench();

    const search = await screen.findByPlaceholderText("搜索资产 Key / 名称 / 标签");
    await user.type(search, "韩烈");

    const table = screen.getByRole("table");
    expect(within(table).getByText("portrait.han-lie.resolute")).toBeInTheDocument();
    expect(within(table).queryByText("portrait.shen-yan.neutral")).not.toBeInTheDocument();
  });

  it("支持选择并批量批准候选版本", async () => {
    const user = userEvent.setup();
    renderWorkbench();

    await user.click(await screen.findByRole("checkbox", { name: "选择 沈渊（文官）· 中立姿态" }));
    await user.click(screen.getByRole("button", { name: "批量批准版本" }));

    const row = within(screen.getByRole("table")).getByText("portrait.shen-yan.neutral").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLTableRowElement).getByText("已审查")).toBeInTheDocument();
    expect(screen.getByText("演示状态已更新 1 项；不会写入项目文件。")).toBeInTheDocument();
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
