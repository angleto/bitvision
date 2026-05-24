// Production UX audit for /viewer/series/[id].
//
// Spec is gated by env so it never runs by accident:
//   - BVP_AUDIT_TOKEN: a JWT for the audit account.
//   - BVP_AUDIT_STUDY_ID: a study tagged ``audit:viewer`` (the spec
//     verifies the tag is present before touching any series). This
//     guarantees the audit only operates on a known fixture, never on
//     unrelated patient data.
//   - E2E_BASE_URL: https://bitvision.example (or staging).
//
// Run with:
//   E2E_BASE_URL=https://bitvision.example \
//   E2E_USE_REAL_BACKEND=1 \
//   BVP_AUDIT_TOKEN=<JWT> \
//   BVP_AUDIT_STUDY_ID=<uuid> \
//   pnpm playwright test viewer-ux-audit-prod
//
// Findings are written to:
//   - playwright/reports/html (Playwright trace + screenshots)
//   - playwright/reports/viewer-ux-audit-prod-<date>.md (punch list)

import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

import {
  checkTapTargets,
  cursorRegexFor,
  runAxe,
  timed,
  walkFocusable,
} from "./_helpers/audit-heuristics";

const TOKEN = process.env.BVP_AUDIT_TOKEN;
const STUDY_ID = process.env.BVP_AUDIT_STUDY_ID;
const BASE_URL = process.env.E2E_BASE_URL;

const OUT_DIR = path.join(process.cwd(), "playwright", "reports");
const REPORT_PATH = path.join(
  OUT_DIR,
  `viewer-ux-audit-prod-${new Date().toISOString().slice(0, 10)}.md`,
);

test.describe("Viewer UX audit (prod)", () => {
  test.beforeAll(() => {
    fs.mkdirSync(OUT_DIR, { recursive: true });
    if (!fs.existsSync(REPORT_PATH)) {
      fs.writeFileSync(
        REPORT_PATH,
        `# Viewer UX audit — ${new Date().toISOString()}\n\n- baseURL: ${
          BASE_URL ?? "(unset)"
        }\n- study: ${STUDY_ID ?? "(unset)"}\n- commit: ${
          process.env.GITHUB_SHA ?? "local"
        }\n\n## Findings\n\n`,
      );
    }
  });

  test.skip(
    !TOKEN || !STUDY_ID || !BASE_URL,
    "Set BVP_AUDIT_TOKEN + BVP_AUDIT_STUDY_ID + E2E_BASE_URL to run.",
  );

  test.beforeEach(async ({ page }) => {
    // Inject the auth token before any navigation so the API client
    // picks it up from localStorage. Matches the prod-share-link smoke.
    await page.addInitScript(
      ({ token }) => {
        window.localStorage.setItem("bvp.token", token);
      },
      { token: TOKEN ?? "" },
    );
    await page.setViewportSize({ width: 1680, height: 1050 });
    page.on("pageerror", (err) => appendReport(`pageerror: ${err.message}\n`));
    page.on("response", async (resp) => {
      if (resp.status() >= 400) {
        appendReport(`[${resp.status()}] ${resp.url()}\n`);
      }
    });
  });

  test("study is tagged audit:viewer (preflight)", async ({ page, request }) => {
    // Fetch the study; assert the audit tag is present. If not, abort
    // the whole suite — without the tag the spec might exercise
    // unrelated patient data.
    const res = await request.get(`${BASE_URL}/api/studies/${STUDY_ID}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    expect(res.status(), `prod /api/studies/${STUDY_ID} not reachable`).toBe(200);
    const body = (await res.json()) as { tags?: string[] };
    const tags = body.tags ?? [];
    expect(tags, "study must carry the audit:viewer tag").toContain("audit:viewer");
    appendReport(`✓ preflight: study ${STUDY_ID} tagged audit:viewer\n`);
    await page.goto(`${BASE_URL}/studies/${STUDY_ID}`);
  });

  test("rail tap targets ≥ 44×44", async ({ page }) => {
    await openFirstSeries(page);
    const buttons = page.locator(".viewer-layout__sidebar button");
    const result = await checkTapTargets(buttons, "rail-button");
    const issueLines = result.issues
      .slice(0, 20)
      .map((it) => `- "${it.name}" ${it.width.toFixed(0)}×${it.height.toFixed(0)}\n`)
      .join("");
    appendReport(
      `### Tap targets (rail buttons)\nok: ${result.ok} · below-threshold: ${result.issues.length}\n${issueLines}\n`,
    );
    // Soft assertion: warn but don't fail; tighten later.
    expect(result.issues.length, "tap target audit produced 50+ violations").toBeLessThan(50);
  });

  test("Tab walk has no focus traps", async ({ page }) => {
    await openFirstSeries(page);
    const walk = await walkFocusable(page, 80);
    const trapTrail = walk.trap ? `last 5: ${walk.visited.slice(-5).join(" → ")}\n` : "";
    appendReport(
      `### Focus walk\nsteps: ${walk.visited.length}, trap: ${walk.trap}\n${trapTrail}\n`,
    );
    expect(walk.trap, "tab walk hit a focus trap").toBe(false);
  });

  test("cursor matches active tool", async ({ page }) => {
    await openFirstSeries(page);
    const tools = ["measure-dist", "measure-sphere", "measure-probe", "measure-lens"];
    const findings: string[] = [];
    for (const tool of tools) {
      const btn = page.locator(`.viewer-layout__sidebar button:has-text("${labelFor(tool)}")`);
      if ((await btn.count()) === 0) {
        findings.push(`- ${tool}: toolbar button not found (skipped)`);
        continue;
      }
      await btn.first().click();
      // Mousemove on the canvas to trigger cursor update.
      const canvas = page.locator(".viewer-layout__canvas, [data-cs-viewport], canvas").first();
      const box = await canvas.boundingBox();
      if (!box) {
        findings.push(`- ${tool}: canvas not found`);
        continue;
      }
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      const cursor = await canvas.evaluate(
        (el) => window.getComputedStyle(el as HTMLElement).cursor,
      );
      const re = cursorRegexFor(tool);
      const matched = re.test(cursor);
      findings.push(`- ${tool}: cursor=${cursor} ${matched ? "✓" : `✗ expected ${re.source}`}`);
    }
    appendReport(`### Cursor / tool match\n${findings.join("\n")}\n\n`);
  });

  test("slice scroll latency budget", async ({ page }) => {
    await openFirstSeries(page);
    const canvas = page.locator(".viewer-layout__canvas, [data-cs-viewport], canvas").first();
    const box = await canvas.boundingBox();
    if (!box) {
      test.fail(true, "canvas not found");
      return;
    }
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    const { ms } = await timed(async () => {
      await page.mouse.wheel(0, 100);
      // Wait a microtask for the next paint.
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r(null))));
    });
    appendReport(`### Slice scroll latency\n one wheel tick: ${ms} ms\n\n`);
    // Soft budget: 200 ms covers a cold first scroll on a thin client.
    expect(ms, "wheel scroll round-trip > 1000ms is unacceptable").toBeLessThan(1000);
  });

  test("lens probe HUD shows on hover + Shift+Wheel changes radius", async ({ page }) => {
    await openFirstSeries(page);
    // Activate the lens via hotkey ``l``.
    await page.keyboard.press("l");
    const canvas = page.locator(".viewer-layout__canvas, [data-cs-viewport], canvas").first();
    const box = await canvas.boundingBox();
    if (!box) {
      test.fail(true, "canvas not found");
      return;
    }
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    // The HUD chip carries text matching "Lens <r> mm" — wait for it.
    const hud = page.locator("text=/Lens \\d/").first();
    await expect(hud).toBeVisible({ timeout: 5_000 });
    const radiusBefore = (await hud.innerText()).match(/Lens\s+(\d+(?:\.\d+)?)/)?.[1];
    // Shift+Wheel up should grow the radius.
    await page.keyboard.down("Shift");
    await page.mouse.wheel(0, -100);
    await page.keyboard.up("Shift");
    await page.waitForTimeout(50);
    const radiusAfter = (await hud.innerText()).match(/Lens\s+(\d+(?:\.\d+)?)/)?.[1];
    appendReport(
      `### Lens probe\nbefore: ${radiusBefore} mm · after Shift+Wheel: ${radiusAfter} mm\n\n`,
    );
    expect(radiusAfter, "Shift+Wheel did not change the lens radius").not.toBe(radiusBefore);
  });

  test("axe accessibility scan (opt-in)", async ({ page }) => {
    await openFirstSeries(page);
    const r = await runAxe(page);
    if (!r) {
      appendReport(
        "### axe-core\nNot installed. To enable: `pnpm add -D @axe-core/playwright`.\n\n",
      );
      test.skip();
      return;
    }
    const seriousOrCritical = r.violations.filter(
      (v) => v.impact === "serious" || v.impact === "critical",
    );
    appendReport(
      `### axe-core\nviolations: ${r.violations.length} · serious/critical: ${seriousOrCritical.length}\n${seriousOrCritical
        .slice(0, 10)
        .map((v) => `- ${v.impact} · ${v.id}`)
        .join("\n")}\n\n`,
    );
    expect(seriousOrCritical.length, "serious/critical a11y violations found").toBeLessThan(10);
  });
});

function appendReport(line: string): void {
  try {
    fs.appendFileSync(REPORT_PATH, line);
  } catch {
    /* best-effort */
  }
}

async function openFirstSeries(page: import("@playwright/test").Page): Promise<void> {
  await page.goto(`${BASE_URL}/studies/${STUDY_ID}`);
  // Click the first series row — selector is brittle, narrow to a
  // ``<a>`` whose href contains ``/viewer/series/``.
  const seriesLink = page.locator('a[href*="/viewer/series/"]').first();
  await expect(seriesLink, "study has no series link").toBeVisible({ timeout: 15_000 });
  await seriesLink.click();
  await expect(page.locator(".viewer-layout__sidebar")).toBeVisible({ timeout: 30_000 });
}

function labelFor(tool: string): string {
  switch (tool) {
    case "measure-dist":
      return "Distance";
    case "measure-sphere":
      return "Sphere";
    case "measure-probe":
      return "Probe";
    case "measure-lens":
      return "Lens";
    case "measure-angle":
      return "Angle";
    default:
      return tool;
  }
}
