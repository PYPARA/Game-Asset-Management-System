import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { CaretDown } from "@phosphor-icons/react";
import "./SelectMenu.css";

export interface SelectMenuOption {
  value: string;
  label: ReactNode;
  disabled?: boolean;
}

export interface SelectMenuProps {
  /** The controlled option value. */
  value: string;
  /** Options are rendered as a listbox, rather than a browser-native popup. */
  options: readonly SelectMenuOption[];
  onChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
  disabled?: boolean;
  /** Text shown when the controlled value has no matching option. */
  placeholder?: string;
  leadingIcon?: ReactNode;
}

/**
 * A small, dependency-free combobox for the workbench controls.
 *
 * The trigger retains the familiar combobox semantics while the menu uses a
 * real listbox/option tree.  Keeping this component controlled means changing
 * a filter from another control immediately updates the visible label.
 */
export function SelectMenu({
  value,
  options,
  onChange,
  ariaLabel,
  className = "",
  disabled = false,
  placeholder = "请选择",
  leadingIcon,
}: SelectMenuProps) {
  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const baseId = useId().replace(/:/g, "");
  const listboxId = `${baseId}-listbox`;

  const selectedIndex = useMemo(
    () => options.findIndex((option) => option.value === value && !option.disabled),
    [options, value],
  );
  const selectedOption = options.find((option) => option.value === value);

  const nextEnabledIndex = (start: number, direction: 1 | -1) => {
    if (options.length === 0) return -1;
    let index = start;
    for (let count = 0; count < options.length; count += 1) {
      index = (index + direction + options.length) % options.length;
      if (!options[index]?.disabled) return index;
    }
    return -1;
  };

  const openMenu = (direction?: 1 | -1) => {
    if (disabled) return;
    const initial = selectedIndex >= 0 ? selectedIndex : options.findIndex((option) => !option.disabled);
    const next = direction ? nextEnabledIndex(initial, direction) : initial;
    setHighlightedIndex(next);
    setOpen(true);
  };

  const closeMenu = (restoreFocus = false) => {
    setOpen(false);
    setHighlightedIndex(-1);
    if (restoreFocus) {
      // Focus restoration is intentionally deferred until after the state
      // update, which avoids scrolling the page when Escape closes the menu.
      window.setTimeout(() => triggerRef.current?.focus(), 0);
    }
  };

  const commitHighlighted = () => {
    const option = options[highlightedIndex];
    if (!option || option.disabled) return;
    onChange(option.value);
    closeMenu(true);
  };

  const onTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open) commitHighlighted();
      else openMenu();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) openMenu(event.key === "ArrowDown" ? 1 : -1);
      else {
        const current = highlightedIndex >= 0 ? highlightedIndex : selectedIndex;
        setHighlightedIndex(nextEnabledIndex(current, event.key === "ArrowDown" ? 1 : -1));
      }
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      if (!open) return;
      event.preventDefault();
      const direction: 1 | -1 = event.key === "Home" ? 1 : -1;
      const start = event.key === "Home" ? -1 : options.length;
      setHighlightedIndex(nextEnabledIndex(start, direction));
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
    if (option && typeof option.scrollIntoView === "function") {
      option.scrollIntoView({ block: "nearest" });
    }
  }, [highlightedIndex, open]);

  useEffect(() => {
    if (!open) return;
    // If options are replaced while the menu is open, keep the highlight on a
    // valid enabled item instead of leaving aria-activedescendant stale.
    if (highlightedIndex >= options.length || options[highlightedIndex]?.disabled) {
      setHighlightedIndex(selectedIndex >= 0 ? selectedIndex : options.findIndex((option) => !option.disabled));
    }
  }, [highlightedIndex, open, options, selectedIndex]);

  const rootClassName = ["select-menu", className].filter(Boolean).join(" ");
  const activeDescendant = highlightedIndex >= 0 ? `${baseId}-option-${highlightedIndex}` : undefined;

  return (
    <div ref={rootRef} className={rootClassName}>
      <button
        ref={triggerRef}
        className="select-menu-trigger"
        type="button"
        role="combobox"
        value={value}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-activedescendant={activeDescendant}
        disabled={disabled}
        onClick={() => { if (open) closeMenu(); else openMenu(); }}
        onKeyDown={onTriggerKeyDown}
      >
        {leadingIcon ? <span className="select-menu-leading" aria-hidden="true">{leadingIcon}</span> : null}
        <span className="select-menu-value">{selectedOption?.label ?? placeholder}</span>
        <CaretDown className="select-menu-chevron" size={13} aria-hidden="true" />
      </button>
      {open ? (
        <div
          ref={listRef}
          id={listboxId}
          className="select-menu-list"
          role="listbox"
          aria-label={`${ariaLabel}选项`}
          tabIndex={-1}
        >
          {options.map((option, index) => (
            <div
              id={`${baseId}-option-${index}`}
              key={option.value}
              className={`select-menu-option ${index === highlightedIndex ? "highlighted" : ""} ${option.disabled ? "disabled" : ""}`}
              role="option"
              aria-selected={option.value === value}
              aria-disabled={option.disabled || undefined}
              data-index={index}
              onMouseEnter={() => { if (!option.disabled) setHighlightedIndex(index); }}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                if (!option.disabled) {
                  onChange(option.value);
                  closeMenu(true);
                }
              }}
            >
              {option.label}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
