// Heuristics shared across the viewer UX audit specs.
//
// Each helper is a pure, side-effect-free function that takes a
// Playwright Page and returns either findings or a boolean. The spec
// composes them per flow; the markdown report is built by the spec
// itself so each finding is grouped under the flow that produced it.
//
// Why not axe-core: ``@axe-core/playwright`` adds a real devDependency
// and per repo policy Claude doesn't run ``pnpm add``. The audit
// surfaces a hook for it (``runAxe``) that's a no-op until the
// dependency is installed by the operator.

import type { Locator, Page } from "@playwright/test";

/** Tap-target minimum per WCAG 2.5.5 Target Size (AAA = 44×44 px on
 *  desktop is the practical Apple HIG / Material baseline). Returns
 *  per-control findings: ``ok`` (passed) and ``issues`` with the
 *  bounding-box dims of any control below threshold. */
export async function checkTapTargets(
  locators: Locator,
  label: string,
): Promise<{ ok: number; issues: Array<{ name: string; width: number; height: number }> }> {
  const count = await locators.count();
  const issues: Array<{ name: string; width: number; height: number }> = [];
  let ok = 0;
  for (let i = 0; i < count; i++) {
    const el = locators.nth(i);
    const box = await el.boundingBox().catch(() => null);
    if (!box) continue;
    if (box.width < 44 || box.height < 44) {
      const text = (await el.innerText().catch(() => ""))?.slice(0, 32) || `${label}#${i}`;
      issues.push({ name: text, width: box.width, height: box.height });
    } else {
      ok += 1;
    }
  }
  return { ok, issues };
}

/** Walk focus through every focusable element and record the active
 *  element id at each step. ``trap`` is true if focus returned to the
 *  same element twice in a row, which signals a trap (e.g. modal
 *  without escape). */
export async function walkFocusable(
  page: Page,
  maxSteps: number,
): Promise<{ visited: string[]; trap: boolean }> {
  const visited: string[] = [];
  // Reset focus to body so the walk starts from a known position.
  await page.evaluate(() => document.body.focus());
  let prev: string | null = null;
  let trap = false;
  for (let step = 0; step < maxSteps; step++) {
    await page.keyboard.press("Tab");
    const cur = await page.evaluate(() => {
      const ae = document.activeElement as HTMLElement | null;
      if (!ae) return "(none)";
      const id = ae.id ? `#${ae.id}` : "";
      const tag = ae.tagName.toLowerCase();
      const cls = ae.className?.toString().split(" ")[0] ?? "";
      return `${tag}${id}${cls ? `.${cls}` : ""}`;
    });
    visited.push(cur);
    if (prev !== null && cur === prev) {
      trap = true;
      break;
    }
    prev = cur;
  }
  return { visited, trap };
}

/** Expected ``cursor`` CSS regex for each tool. The toolbar in the
 *  viewer page binds tools by setting CSS-level cursor on the
 *  ``<div ref={ref}>`` wrapping the Cornerstone canvas; this helper
 *  reads the computed style and matches the regex. */
export function cursorRegexFor(tool: string): RegExp {
  switch (tool) {
    case "pan":
      return /grab|move|hand/;
    case "wl":
      return /ew-resize|ns-resize|move|default/;
    case "measure-dist":
    case "measure-angle":
    case "measure-area":
    case "measure-ellipse":
    case "measure-sphere":
    case "measure-freehand":
    case "measure-arrow":
    case "measure-text":
    case "measure-probe":
    case "measure-lens":
      return /crosshair|cell|default/;
    default:
      return /.*/;
  }
}

/** Latency budgeting helper. Wraps a step in a perf measure and
 *  returns the elapsed ms; the spec asserts against per-action
 *  budgets. */
export async function timed<T>(fn: () => Promise<T>): Promise<{ value: T; ms: number }> {
  const t0 = Date.now();
  const value = await fn();
  return { value, ms: Date.now() - t0 };
}

/** Optional axe-core hook. Returns ``null`` when the dependency
 *  isn't installed; otherwise returns the violations summary. */
export async function runAxe(
  page: Page,
): Promise<null | { violations: Array<{ id: string; impact: string | null }> }> {
  try {
    // Dynamic import so the spec doesn't statically fail when
    // ``@axe-core/playwright`` isn't installed.
    const mod = (await import(
      // @ts-ignore — optional peer dep, hard-fail tolerated at import.
      "@axe-core/playwright"
    ).catch(() => null)) as null | { default: { new (): unknown } };
    if (!mod) return null;
    const Builder = (mod as unknown as { AxeBuilder: new (opts: { page: Page }) => unknown })
      .AxeBuilder;
    if (!Builder) return null;
    const builder = new Builder({ page }) as unknown as {
      analyze: () => Promise<{
        violations: Array<{ id: string; impact: string | null }>;
      }>;
    };
    const result = await builder.analyze();
    return { violations: result.violations };
  } catch {
    return null;
  }
}
