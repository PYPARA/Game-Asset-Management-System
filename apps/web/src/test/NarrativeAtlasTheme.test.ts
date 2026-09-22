import { describe, expect, it } from "vitest";
import styles from "../styles.css?raw";

function narrativeStyles() {
  const start = styles.indexOf("/* M7 · Narrative Atlas");
  const end = styles.indexOf("@media (max-width: 1240px)", start);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(start);
  return styles.slice(start, end);
}

describe("Narrative Atlas theme", () => {
  it("inherits the production workbench dark theme", () => {
    const css = narrativeStyles();

    expect(css).not.toContain("color-scheme: light");
    expect(css).not.toMatch(/\.narrative-mode \.topbar/);
    expect(css).toContain("--atlas-bg: var(--bg)");
    expect(css).toContain("--atlas-surface: var(--surface-1)");
    expect(css).toContain("--atlas-panel: var(--surface-2)");
    expect(css).toContain("--atlas-raised: var(--surface-3)");
    expect(css).toContain("--atlas-raised-strong: var(--surface-4)");
    expect(css).toContain("--atlas-line: var(--line)");
    expect(css).toContain("--atlas-line-soft: var(--line-soft)");
    expect(css).toContain("--atlas-text: var(--text)");
    expect(css).toContain("--atlas-copy: var(--text-soft)");
    expect(css).toContain("--atlas-muted: var(--muted)");
    expect(css).toContain("--atlas-blue: #84b5d5");
    expect(css).toContain("--atlas-green: #9bc696");
    expect(css).toContain("--atlas-red: var(--red)");
    expect(css).toContain("--atlas-amber: var(--amber)");
    expect(css).not.toMatch(/background(?:-color)?:\s*#(?:f[0-9a-f]{2,7}|e[8-9a-f][0-9a-f]{1,6})/i);
  });
});
