import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Check, CaretDown } from "@phosphor-icons/react";
import "./SelectMenu.css";

export interface MultiSelectMenuOption {
  value: string;
  label: ReactNode;
  disabled?: boolean;
}

export interface MultiSelectMenuProps {
  values: readonly string[];
  options: readonly MultiSelectMenuOption[];
  onChange: (values: string[]) => void;
  ariaLabel: string;
  className?: string;
  disabled?: boolean;
  placeholder?: string;
}

/**
 * A compact, non-native multi-select for relation fields.  Unlike a browser
 * `<select multiple>`, the menu stays usable with a pointer while retaining
 * listbox semantics and a complete keyboard path.
 */
export function MultiSelectMenu({
  values,
  options,
  onChange,
  ariaLabel,
  className = "",
  disabled = false,
  placeholder = "请选择",
}: MultiSelectMenuProps) {
  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const baseId = useId().replace(/:/g, "");
  const listboxId = `${baseId}-listbox`;
  const selected = useMemo(() => new Set(values), [values]);
  const selectedLabels = options.filter((option) => selected.has(option.value));
  const triggerText = selectedLabels.length === 0
    ? placeholder
    : selectedLabels.length <= 2
      ? selectedLabels.map((option) => option.label).join("、")
      : `${selectedLabels.slice(0, 2).map((option) => option.label).join("、")} 等 ${selectedLabels.length} 项`;

  const nextEnabledIndex = (start: number, direction: 1 | -1) => {
    if (options.length === 0) return -1;
    let index = start;
    for (let count = 0; count < options.length; count += 1) {
      index = (index + direction + options.length) % options.length;
      if (!options[index]?.disabled) return index;
    }
    return -1;
  };

  const openMenu = () => {
    if (disabled) return;
    const selectedIndex = options.findIndex((option) => selected.has(option.value) && !option.disabled);
    setHighlightedIndex(selectedIndex >= 0 ? selectedIndex : options.findIndex((option) => !option.disabled));
    setOpen(true);
  };

  const closeMenu = (restoreFocus = false) => {
    setOpen(false);
    setHighlightedIndex(-1);
    if (restoreFocus) window.setTimeout(() => triggerRef.current?.focus(), 0);
  };

  const toggleOption = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    const next = new Set(values);
    if (next.has(option.value)) next.delete(option.value);
    else next.add(option.value);
    // Preserve the authored option order so persisted relation fields remain
    // stable across opening/closing the menu.
    onChange(options.filter((candidate) => next.has(candidate.value)).map((candidate) => candidate.value));
  };

  const onTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!open) openMenu();
      else toggleOption(highlightedIndex);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        openMenu();
      } else {
        setHighlightedIndex((current) => nextEnabledIndex(current >= 0 ? current : -1, event.key === "ArrowDown" ? 1 : -1));
      }
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      if (!open) return;
      event.preventDefault();
      setHighlightedIndex(nextEnabledIndex(event.key === "Home" ? -1 : options.length, event.key === "Home" ? 1 : -1));
      return;
    }
    if (event.key === "Escape" && open) {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    if (event.key === "Tab" && open) closeMenu();
  };

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeMenu();
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  useEffect(() => {
    if (!open || highlightedIndex < 0) return;
    const option = listRef.current?.querySelector<HTMLElement>(`[data-index="${highlightedIndex}"]`);
    option?.scrollIntoView?.({ block: "nearest" });
  }, [highlightedIndex, open]);

  const rootClassName = ["select-menu", "multi-select-menu", className].filter(Boolean).join(" ");
  const activeDescendant = highlightedIndex >= 0 ? `${baseId}-option-${highlightedIndex}` : undefined;

  return (
    <div ref={rootRef} className={rootClassName}>
      <button
        ref={triggerRef}
        className="select-menu-trigger multi-select-trigger"
        type="button"
        role="combobox"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-activedescendant={activeDescendant}
        disabled={disabled}
        onClick={() => (open ? closeMenu() : openMenu())}
        onKeyDown={onTriggerKeyDown}
      >
        <span className={`select-menu-value ${selectedLabels.length ? "" : "placeholder"}`}>{triggerText}</span>
        <CaretDown className="select-menu-chevron" size={13} aria-hidden="true" />
      </button>
      {open ? (
        <div
          ref={listRef}
          id={listboxId}
          className="select-menu-list multi-select-list"
          role="listbox"
          aria-label={`${ariaLabel}选项`}
          aria-multiselectable="true"
          tabIndex={-1}
        >
          {options.map((option, index) => {
            const isSelected = selected.has(option.value);
            return (
              <div
                id={`${baseId}-option-${index}`}
                key={option.value}
                className={`select-menu-option multi-select-option ${index === highlightedIndex ? "highlighted" : ""} ${option.disabled ? "disabled" : ""}`}
                role="option"
                aria-selected={isSelected}
                aria-disabled={option.disabled || undefined}
                data-index={index}
                onMouseEnter={() => { if (!option.disabled) setHighlightedIndex(index); }}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => toggleOption(index)}
              >
                <span className="multi-select-check" aria-hidden="true">{isSelected ? <Check size={13} weight="bold" /> : null}</span>
                <span>{option.label}</span>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
