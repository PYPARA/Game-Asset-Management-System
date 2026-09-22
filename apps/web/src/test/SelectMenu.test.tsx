import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { SelectMenu, type SelectMenuOption } from "../components/SelectMenu";
import { MultiSelectMenu } from "../components/MultiSelectMenu";

const options: SelectMenuOption[] = [
  { value: "all", label: "全部状态" },
  { value: "pending", label: "待审查" },
  { value: "approved", label: "已审查" },
];

function renderSelect(value = "all", onChange = vi.fn()) {
  return render(
    <SelectMenu
      ariaLabel="按审查状态筛选"
      value={value}
      options={options}
      onChange={onChange}
    />,
  );
}

describe("SelectMenu", () => {
  it("opens an ARIA listbox and commits a clicked option", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect("all", onChange);

    const trigger = screen.getByRole("combobox", { name: "按审查状态筛选" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await user.click(trigger);
    expect(screen.getByRole("listbox", { name: "按审查状态筛选选项" })).toBeInTheDocument();

    await user.click(screen.getByRole("option", { name: "待审查" }));
    expect(onChange).toHaveBeenCalledWith("pending");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("supports ArrowDown and Enter without relying on a native select", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect("all", onChange);
    const trigger = screen.getByRole("combobox", { name: "按审查状态筛选" });

    await user.click(trigger);
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onChange).toHaveBeenCalledWith("pending");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("closes when focus leaves the control", async () => {
    const user = userEvent.setup();
    render(
      <>
        <SelectMenu ariaLabel="筛选" value="all" options={options} onChange={() => undefined} />
        <button type="button">外部按钮</button>
      </>,
    );
    await user.click(screen.getByRole("combobox", { name: "筛选" }));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "外部按钮" }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("toggles multiple values in an accessible listbox", async () => {
    const user = userEvent.setup();
    function Example() {
      const [values, setValues] = useState<string[]>(["scene"]);
      return (
        <MultiSelectMenu
          ariaLabel="关联场景"
          values={values}
          options={[{ value: "scene", label: "场景" }, { value: "character", label: "角色" }]}
          onChange={setValues}
        />
      );
    }
    render(<Example />);
    const trigger = screen.getByRole("combobox", { name: "关联场景" });
    expect(trigger).toHaveTextContent("场景");
    await user.click(trigger);
    expect(screen.getByRole("listbox")).toHaveAttribute("aria-multiselectable", "true");
    await user.click(screen.getByRole("option", { name: "角色" }));
    expect(trigger).toHaveTextContent("场景、角色");
    await user.click(screen.getByRole("option", { name: "场景" }));
    expect(trigger).toHaveTextContent("角色");
  });
});
