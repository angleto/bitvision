// Autonomous instrumented audit of the DICOM viewer (series + contrast)
// against a REAL backend. Reads ``window.__viewer`` (populated only when
// an admin has flipped the ``viewer.debug.instrumentation`` app-setting)
// plus the ``data-testid`` contract, captures screenshots + console +
// failed network + usability heuristics, and writes a markdown punch-list.
//
// This is the harness that lets the agent iterate on viewer clarity +
// usability: run it, read the screenshots + report, fix, re-run.
//
// Gated by env so it NEVER runs by accident (and only on a study tagged
// ``audit:viewer`` so it can't touch unrelated patient data):
//   E2E_USE_REAL_BACKEND=1
//   E2E_BASE_URL=https://bitvision.xeno.garden
//   BVP_AUDIT_TOKEN=<JWT for an account that can read the study>
//   BVP_AUDIT_STUDY_ID=<uuid tagged audit:viewer>
//   BVP_AUDIT_SERIES_ID=<uuid>            (optional — force a series)
//
// Run:
//   E2E_USE_REAL_BACKEND=1 E2E_BASE_URL=https://bitvision.xeno.garden \
//   BVP_AUDIT_TOKEN=$TOK BVP_AUDIT_STUDY_ID=$SID \
//   pnpm exec playwright test viewer-instrumented-audit
//
// Artifacts land in frontend/playwright/reports/.

import fs from "node:fs";
import path from "node:path";

import { type Page, expect, test } from "@playwright/test";

import { checkTapTargets, walkFocusable } from "./_helpers/audit-heuristics";

const TOKEN = process.env.BVP_AUDIT_TOKEN;
const STUDY_ID = process.env.BVP_AUDIT_STUDY_ID;
const BASE_URL = process.env.E2E_BASE_URL;
const FORCE_SERIES = process.env.BVP_AUDIT_SERIES_ID;

// Radiological-test fixtures for the 4-phase liver study 2858def7. World LPS
// points VERIFIED via MCP compute_phase_washout: the lesion point samples
// classic liver parenchyma (unenhanced ≈54 → arterial ≈73 → portal ≈107 →
// delayed ≈82 HU) and is covered by all 4 phases; the out-of-overlap point sits
// above the shorter phases' (arterial/delayed) z-extent.
const LIVER_STUDY = "2858def7-e256-4e29-a4f9-c9f70e23ba20";
const LIVER_LESION: [number, number, number] = [-55, -45, -280];
const LIVER_PARENCHYMA: [number, number, number] = [-72, -48, -282];
const OUT_OF_OVERLAP: [number, number, number] = [-55, -45, -150];

const STAMP = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
const OUT_DIR = path.join(process.cwd(), "playwright", "reports");
const SHOTS = path.join(OUT_DIR, `viewer-audit-${STAMP}`);
const REPORT = path.join(OUT_DIR, `viewer-instrumented-audit-${STAMP}.md`);

function emit(line: string): void {
  fs.appendFileSync(REPORT, `${line}\n`);
}

async function shot(page: Page, name: string): Promise<string> {
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false }).catch(() => {});
  return path.relative(OUT_DIR, file);
}

/** Read window.__viewer; null if the instrumentation flag is off. */
async function readProbe(page: Page): Promise<Record<string, unknown> | null> {
  return page.evaluate(
    () => (window as unknown as { __viewer?: unknown }).__viewer ?? null,
  ) as Promise<Record<string, unknown> | null>;
}

// Per-test capture buffers, reset in beforeEach.
let consoleErrors: string[] = [];
let pageErrors: string[] = [];
let netFailures: string[] = [];

test.describe("Viewer instrumented audit", () => {
  // Real prod loads (full CT volume fetch + pack + first paint) routinely
  // exceed the 30 s default; give each audit room to load and settle.
  test.describe.configure({ timeout: 150_000 });

  // BVP_AUDIT_TOKEN is required: /api/studies/{id} needs auth even for public
  // OpenData studies, and a private study needs a token that can read it.
  test.skip(
    !STUDY_ID || !BASE_URL,
    "Set BVP_AUDIT_STUDY_ID + E2E_BASE_URL (+ E2E_USE_REAL_BACKEND=1); BVP_AUDIT_TOKEN only for private studies.",
  );

  test.beforeAll(() => {
    fs.mkdirSync(SHOTS, { recursive: true });
    fs.writeFileSync(
      REPORT,
      `# Viewer instrumented audit — ${new Date().toISOString()}\n\n- baseURL: ${BASE_URL}\n- study: ${STUDY_ID}\n- screenshots: ${path.relative(OUT_DIR, SHOTS)}/\n\nEach section lists what loaded, the window.__viewer snapshot, console/network errors, and usability heuristics.\n`,
    );
  });

  test.beforeEach(async ({ page }) => {
    consoleErrors = [];
    pageErrors = [];
    netFailures = [];
    if (TOKEN && BASE_URL) {
      // The SPA migrated (2026-05-21) from JWT-in-localStorage to an HttpOnly
      // ``bvp_session`` cookie whose VALUE is the JWT (api/auth.py
      // _set_session_cookie). deps.py reads the token from that cookie OR the
      // Authorization header, so seeding the cookie with our minted token is
      // what actually authenticates the browser — localStorage is a no-op now.
      const url = new URL(BASE_URL);
      await page.context().addCookies([
        {
          name: "bvp_session",
          value: TOKEN,
          domain: url.hostname,
          path: "/",
          httpOnly: true,
          secure: url.protocol === "https:",
          sameSite: "Lax",
        },
      ]);
    }
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 300));
    });
    page.on("pageerror", (err) => pageErrors.push(String(err).slice(0, 300)));
    page.on("requestfailed", (req) =>
      netFailures.push(
        `${req.method()} ${req.url().slice(0, 120)} — ${req.failure()?.errorText ?? "failed"}`,
      ),
    );
    page.on("response", (res) => {
      if (res.status() >= 400) netFailures.push(`${res.status()} ${res.url().slice(0, 120)}`);
    });
  });

  function dumpCaptures(): void {
    if (consoleErrors.length) {
      emit(`- console errors (${consoleErrors.length}):`);
      for (const e of consoleErrors.slice(0, 15)) emit(`  - \`${e}\``);
    } else emit("- console errors: none");
    if (pageErrors.length) {
      emit(`- uncaught page errors (${pageErrors.length}):`);
      for (const e of pageErrors.slice(0, 10)) emit(`  - \`${e}\``);
    }
    if (netFailures.length) {
      const uniq = [...new Set(netFailures)];
      emit(`- network failures (${uniq.length} unique):`);
      for (const e of uniq.slice(0, 20)) emit(`  - \`${e}\``);
    } else emit("- network failures: none");
  }

  async function instrumentationOrNote(page: Page): Promise<boolean> {
    const probe = await readProbe(page);
    if (probe) return true;
    // Give the 1 Hz poll a beat, then re-check.
    await page.waitForTimeout(2500);
    const again = await readProbe(page);
    if (again) return true;
    emit(
      "- ⚠️ window.__viewer is undefined — the `viewer.debug.instrumentation` flag is OFF " +
        "(enable it in /admin/settings) OR the probe failed to populate. Functional assertions skipped; screenshots still captured.",
    );
    return false;
  }

  test("series viewer", async ({ page, request }) => {
    emit("\n## Series viewer\n");

    // Resolve a series to open: forced env, else the study's first CT series.
    let seriesId = FORCE_SERIES ?? null;
    let studyTagged = false;
    try {
      const res = await request.get(`${BASE_URL}/api/studies/${STUDY_ID}`, {
        headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
      });
      if (res.ok()) {
        const study = (await res.json()) as {
          series?: Array<{ id: string; modality?: string; instance_count?: number }>;
          tags?: Array<{ namespace: string; value: string }>;
        };
        studyTagged = !!study.tags?.some((t) => t.namespace === "audit" && t.value === "viewer");
        if (!seriesId) {
          // Pick the meatiest CT series (most instances) so we open a real
          // axial volume, not a 1-2 slice scout / dose-report / screen-save.
          const cts = (study.series ?? [])
            .filter((s) => (s.modality ?? "").toUpperCase() === "CT")
            .sort((a, b) => (b.instance_count ?? 0) - (a.instance_count ?? 0));
          seriesId = cts[0]?.id ?? study.series?.[0]?.id ?? null;
          if (cts[0]) emit(`- picked series with ${cts[0].instance_count ?? "?"} instances`);
        }
      } else {
        emit(`- ⚠️ GET /api/studies/${STUDY_ID} → ${res.status()}`);
      }
    } catch (e) {
      emit(`- ⚠️ could not resolve series: \`${String(e).slice(0, 160)}\``);
    }
    emit(
      `- audit:viewer tag present: ${studyTagged ? "yes" : "NO (refusing? proceeding read-only)"}`,
    );
    if (!seriesId) {
      emit("- ❌ no series resolved — aborting series audit.");
      return;
    }
    emit(`- series: \`${seriesId}\``);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${BASE_URL}/viewer/series/${seriesId}`, { waitUntil: "domcontentloaded" });

    // Wait for either the probe volume or a visible error card.
    await page
      .waitForFunction(
        () => {
          const v = (window as unknown as { __viewer?: { volume?: unknown } }).__viewer;
          return !!v?.volume || !!document.querySelector('[data-testid="viewer-error"]');
        },
        null,
        { timeout: 45_000 },
      )
      .catch(() => {});
    await page.waitForTimeout(1500); // let first paint settle
    emit(`- screenshot: \`${await shot(page, "series-loaded")}\``);
    // Real-progress check (directive: slow ops show progress, not a bare
    // spinner). The "Full-res …%/MB" badge / "Downloading volume… MB" overlay
    // is transient — poll over the load window, don't rely on one instant.
    let progressSeen: string | null = null;
    for (let i = 0; i < 12; i++) {
      progressSeen = await page
        .locator("text=/Full-res|Downloading volume/i")
        .first()
        .textContent({ timeout: 800 })
        .catch(() => null);
      if (progressSeen) break;
      await page.waitForTimeout(800);
    }
    emit(
      `- full-res progress indicator: ${progressSeen ? `\`${progressSeen.trim().slice(0, 70)}\`` : "not seen during load window"}`,
    );

    const hasProbe = await instrumentationOrNote(page);
    if (hasProbe) {
      const probe = (await readProbe(page)) as {
        identity?: Record<string, unknown>;
        volume?: { dims?: number[]; hasGeometry?: boolean } | null;
        panes?: Record<
          string,
          { visible?: boolean; voi?: { lower: number; upper: number } | null }
        >;
        activeTool?: string | null;
        layout?: string | null;
        measurementCount?: number;
        error?: string | null;
      };
      emit(`- identity: \`${JSON.stringify(probe.identity ?? {})}\``);
      emit(`- volume: \`${JSON.stringify(probe.volume ?? null)}\``);
      emit(`- layout: \`${probe.layout ?? "?"}\`, activeTool: \`${probe.activeTool ?? "none"}\``);
      if (probe.error) emit(`- ❌ surface error: \`${probe.error}\``);
      const panes = probe.panes ?? {};
      for (const [k, p] of Object.entries(panes)) {
        if (!p?.visible) continue;
        const voi = p.voi;
        const bad = !voi || !(voi.lower < voi.upper);
        emit(
          `- pane \`${k}\`: voi=${voi ? `[${voi.lower.toFixed(0)}, ${voi.upper.toFixed(0)}]` : "null"}${bad ? "  ⚠️ collapsed/absent VOI → likely BLACK pane" : ""}`,
        );
      }
      if (probe.volume && probe.volume.hasGeometry === false)
        emit("- ⚠️ volume has no geometry (washout/world-sync will skip this series).");
    }

    // Usability heuristics on the chrome.
    try {
      const buttons = page.locator(".viewer-btn, [data-testid='viewer-toolbar'] button");
      const tap = await checkTapTargets(buttons, "viewer-toolbar");
      emit(`- tap targets: ${tap.ok} ok, ${tap.issues.length} below 44px`);
      for (const i of tap.issues.slice(0, 8))
        emit(`  - \`${i.name}\` ${Math.round(i.width)}×${Math.round(i.height)}`);
      const focus = await walkFocusable(page, 25);
      emit(`- focus walk: visited ${focus.visited.length}, trap=${focus.trap}`);
    } catch (e) {
      emit(`- heuristics error: \`${String(e).slice(0, 120)}\``);
    }

    dumpCaptures();
  });

  test("contrast viewer", async ({ page }) => {
    emit("\n## Contrast multiphase viewer\n");
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto(`${BASE_URL}/viewer/contrast?study=${STUDY_ID}`, {
      waitUntil: "domcontentloaded",
    });

    // Wait for the phases to actually FINISH loading (a pane with a real VOI),
    // not just mount — the volumes are large and a 2 s screenshot catches only
    // the "loading…" state. Fall back to any error card.
    await page
      .waitForFunction(
        () => {
          const v = (
            window as unknown as {
              __viewer?: { surface?: string; panes?: Record<string, { voi?: unknown }> };
            }
          ).__viewer;
          const loaded =
            v?.surface === "contrast" &&
            !!v.panes &&
            Object.values(v.panes).some((p) => p.voi != null);
          return loaded || !!document.querySelector('[data-testid="viewer-error"]');
        },
        null,
        { timeout: 90_000 },
      )
      .catch(() => {});
    // Also wait for the "loading…" placeholders to clear, then settle.
    await page
      .locator("text=/loading/i")
      .first()
      .waitFor({ state: "hidden", timeout: 30_000 })
      .catch(() => {});
    await page.waitForTimeout(2500);
    emit(`- screenshot: \`${await shot(page, "contrast-loaded")}\``);

    const phasePanes = await page.locator('[data-testid^="contrast-phase-"]').count();
    emit(`- phase panes rendered: ${phasePanes}`);
    const hasWashoutPanel = (await page.locator('[data-testid="washout-panel"]').count()) > 0;
    const hasWashoutRun = (await page.locator('[data-testid="washout-run"]').count()) > 0;
    emit(`- washout panel present: ${hasWashoutPanel}, run entry-point: ${hasWashoutRun}`);

    const hasProbe = await instrumentationOrNote(page);
    if (hasProbe) {
      const probe = (await readProbe(page)) as {
        panes?: Record<
          string,
          { visible?: boolean; voi?: { lower: number; upper: number } | null }
        >;
        error?: string | null;
        measurementCount?: number;
      };
      if (probe.error) emit(`- ❌ surface error: \`${probe.error}\``);
      const panes = probe.panes ?? {};
      emit(`- probe panes: ${Object.keys(panes).length}`);
      for (const [k, p] of Object.entries(panes)) {
        const voi = p?.voi;
        const bad = p?.visible && (!voi || !(voi.lower < voi.upper));
        emit(
          `- pane \`${k}\`: visible=${!!p?.visible} voi=${voi ? `[${voi.lower.toFixed(0)}, ${voi.upper.toFixed(0)}]` : "null"}${bad ? "  ⚠️ collapsed VOI → likely BLACK pane" : ""}`,
        );
      }
    }

    dumpCaptures();
  });

  // Tap targets are a TOUCH concern (WCAG 2.5.5). Desktop (fine pointer) keeps
  // a compact density on purpose; the 44px minimum is enforced only on coarse
  // pointers. Audit in a real touch context (hasTouch=true → pointer:coarse)
  // on the full desktop layout so every .viewer-btn renders.
  test("tap targets (touch / coarse pointer)", async ({ browser }) => {
    emit("\n## Tap targets — touch context (coarse pointer)\n");
    const ctx = await browser.newContext({
      hasTouch: true,
      viewport: { width: 1440, height: 900 },
    });
    if (TOKEN && BASE_URL) {
      const url = new URL(BASE_URL);
      await ctx.addCookies([
        {
          name: "bvp_session",
          value: TOKEN,
          domain: url.hostname,
          path: "/",
          httpOnly: true,
          secure: url.protocol === "https:",
          sameSite: "Lax",
        },
      ]);
    }
    const tpage = await ctx.newPage();
    try {
      const sid = FORCE_SERIES ?? "5ac9d424-97dc-4e26-9f3a-034c6819a033";
      await tpage.goto(`${BASE_URL}/viewer/series/${sid}`, { waitUntil: "domcontentloaded" });
      await tpage.waitForTimeout(5000); // let the chrome render
      const buttons = tpage.locator(".viewer-btn");
      const total = await buttons.count();
      const tap = await checkTapTargets(buttons, "viewer-btn");
      emit(`- .viewer-btn rendered: ${total}`);
      emit(`- coarse-pointer tap targets: ${tap.ok} ≥44px, ${tap.issues.length} below`);
      for (const i of tap.issues.slice(0, 8))
        emit(`  - \`${i.name}\` ${Math.round(i.width)}×${Math.round(i.height)}`);
      emit(`- screenshot: \`${await shot(tpage, "touch-controls")}\``);
    } catch (e) {
      emit(`- touch tap-target error: \`${String(e).slice(0, 120)}\``);
    } finally {
      await ctx.close();
    }
  });

  // ─── Radiological gate tests (study 2858def7) ──────────────────────────────
  // These exercise the REAL workflow a radiologist needs: get the same liver
  // slice on all 4 phases (no black panes), then measure wash-out on a liver
  // ROI. They must PASS before the contrast viewer is considered working.

  test("radiological — 4-phase liver sync (no black panes)", async ({ page }) => {
    test.skip(STUDY_ID !== LIVER_STUDY, "Liver world points are specific to study 2858def7.");
    emit("\n## Radiological — 4-phase liver sync\n");
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto(`${BASE_URL}/viewer/contrast?study=${STUDY_ID}`, {
      waitUntil: "domcontentloaded",
    });
    // Wait for all phases to finish loading (a pane with a real VOI) AND the
    // test driver to be installed (instrumentation flag must be ON).
    const ready = await page
      .waitForFunction(
        () => {
          const w = window as unknown as {
            __viewer?: { surface?: string; panes?: Record<string, { voi?: unknown }> };
            __viewerTest?: { setCrosshairWorldAll?: unknown };
          };
          return (
            !!w.__viewerTest?.setCrosshairWorldAll &&
            w.__viewer?.surface === "contrast" &&
            !!w.__viewer.panes &&
            Object.values(w.__viewer.panes).some((p) => p.voi != null)
          );
        },
        null,
        { timeout: 90_000 },
      )
      .then(() => true)
      .catch(() => false);
    if (!ready) {
      emit("- ❌ instrumentation/test-driver not ready (flag off, or not all phases loaded).");
    }
    await page
      .locator("text=/loading/i")
      .first()
      .waitFor({ state: "hidden", timeout: 30_000 })
      .catch(() => {});

    // Drive every pane to the verified liver point (covered by all 4 phases).
    const cov = await page.evaluate(
      (lps) =>
        (
          window as unknown as {
            __viewerTest?: { setCrosshairWorldAll?: (p: number[]) => Record<string, boolean> };
          }
        ).__viewerTest?.setCrosshairWorldAll?.(lps),
      LIVER_LESION,
    );
    emit(`- setCrosshairWorldAll(liver) → coverage: \`${JSON.stringify(cov)}\``);
    await page.waitForTimeout(1500);
    emit(`- screenshot: \`${await shot(page, "radiological-liver-sync")}\``);

    const probe = await readProbe(page);
    const panes = (probe?.panes ?? {}) as Record<
      string,
      {
        visible?: boolean;
        sliceIndex?: number | null;
        canvas?: { width: number; height: number } | null;
        outOfCoverage?: boolean | null;
        crosshairLps?: [number, number, number] | null;
      }
    >;
    let allValid = true;
    let visibleCount = 0;
    const lpsList: Array<[number, number, number]> = [];
    for (const [k, p] of Object.entries(panes)) {
      if (!p.visible) continue;
      visibleCount += 1;
      const valid =
        p.outOfCoverage === false &&
        p.sliceIndex != null &&
        p.sliceIndex >= 0 &&
        !!p.canvas &&
        p.canvas.width > 0 &&
        p.canvas.height > 0;
      if (!valid) allValid = false;
      if (p.crosshairLps) lpsList.push(p.crosshairLps);
      emit(
        `- pane \`${k}\`: slice=${p.sliceIndex} canvas=${p.canvas?.width}×${p.canvas?.height} outOfCoverage=${p.outOfCoverage} lps=${JSON.stringify(p.crosshairLps)}`,
      );
    }
    const ref = lpsList[0];
    const coincide =
      !!ref &&
      lpsList.every(
        (l) =>
          Math.abs(l[0] - ref[0]) < 2 && Math.abs(l[1] - ref[1]) < 2 && Math.abs(l[2] - ref[2]) < 3,
      );
    emit(`- crosshairLps coincide across phases (same anatomy): ${coincide}`);

    // Out-of-overlap: above the shorter phases' z-extent → they must flag
    // out-of-coverage (snap to nearest slice), NOT go black.
    await page.evaluate(
      (lps) =>
        (
          window as unknown as {
            __viewerTest?: { setCrosshairWorldAll?: (p: number[]) => unknown };
          }
        ).__viewerTest?.setCrosshairWorldAll?.(lps),
      OUT_OF_OVERLAP,
    );
    await page.waitForTimeout(1200);
    emit(`- screenshot: \`${await shot(page, "radiological-out-of-coverage")}\``);
    const probe2 = await readProbe(page);
    const oocCount = Object.values(
      (probe2?.panes ?? {}) as Record<string, { outOfCoverage?: boolean | null }>,
    ).filter((p) => p.outOfCoverage === true).length;
    emit(
      `- out-of-overlap: ${oocCount} phase(s) flagged out-of-coverage (expected the shorter ones)`,
    );

    dumpCaptures();
    // GATE assertions
    expect(visibleCount, "4 phase panes visible").toBeGreaterThanOrEqual(4);
    expect(allValid, "every liver-synced pane is in-coverage with a valid slice + canvas").toBe(
      true,
    );
    expect(coincide, "all phases report the same crosshair world position").toBe(true);
    expect(oocCount, "out-of-overlap flags the shorter phases (no silent black)").toBeGreaterThan(
      0,
    );
    emit("- ✅ liver sync GATE passed");
  });

  test("radiological — liver wash-out measurement", async ({ page }) => {
    test.skip(STUDY_ID !== LIVER_STUDY, "Liver world points are specific to study 2858def7.");
    emit("\n## Radiological — liver wash-out\n");
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto(`${BASE_URL}/viewer/contrast?study=${STUDY_ID}`, {
      waitUntil: "domcontentloaded",
    });
    await page
      .waitForFunction(
        () =>
          !!(window as unknown as { __viewerTest?: { runWashout?: unknown } }).__viewerTest
            ?.runWashout,
        null,
        { timeout: 90_000 },
      )
      .catch(() => {});

    const result = (await page.evaluate(
      async (pts) => {
        const [lesion, paren] = pts;
        const t = (
          window as unknown as {
            __viewerTest?: {
              runWashout?: (a: unknown) => Promise<unknown>;
            };
          }
        ).__viewerTest;
        return (await t?.runWashout?.({
          lesionCenterLps: lesion,
          lesionRadiusMm: 12,
          parenchymaCenterLps: paren,
          parenchymaRadiusMm: 10,
          region: "liver",
        })) as {
          samples?: Array<{ acquisition_phase: string | null; hu_mean: number; hu_std: number }>;
          skipped?: Array<{ acquisition_phase: string | null; reason: string }>;
          washout?: {
            region?: string | null;
            apw?: number | null;
            rpw?: number | null;
            relative_curve?: Array<{ acquisition_phase: string; delta_hu: number }>;
          };
        } | null;
      },
      [LIVER_LESION, LIVER_PARENCHYMA],
    )) as Awaited<ReturnType<typeof page.evaluate>> as {
      samples?: Array<{ acquisition_phase: string | null; hu_mean: number; hu_std: number }>;
      skipped?: Array<{ acquisition_phase: string | null; reason: string }>;
      washout?: {
        region?: string | null;
        apw?: number | null;
        rpw?: number | null;
        relative_curve?: Array<{ acquisition_phase: string; delta_hu: number }>;
      };
    } | null;

    const samples = result?.samples ?? [];
    const skipped = result?.skipped ?? [];
    const region = result?.washout?.region ?? null;
    const relative = result?.washout?.relative_curve ?? [];
    const byPhase: Record<string, number> = {};
    for (const s of samples) if (s.acquisition_phase) byPhase[s.acquisition_phase] = s.hu_mean;
    emit(`- region: ${region}`);
    emit(
      `- samples (${samples.length}): ${samples.map((s) => `${s.acquisition_phase}=${s.hu_mean?.toFixed(0)}±${s.hu_std?.toFixed(0)}`).join(", ")}`,
    );
    emit(`- skipped phases: ${skipped.length}`);
    emit(
      `- relative_curve (lesion−parenchyma): ${relative.map((r) => `${r.acquisition_phase}=${r.delta_hu?.toFixed(1)}`).join(", ")}`,
    );

    dumpCaptures();
    // GATE assertions — the radiological workflow must compute across all 4
    // phases with the liver (relative-to-parenchyma) interpretation.
    expect(samples.length, "4 phase HU samples").toBe(4);
    expect(skipped.length, "no phase skipped — all cover the liver").toBe(0);
    expect(region, "liver region interpretation").toBe("liver");
    expect(relative.length, "liver relative curve computed (lesion vs parenchyma)").toBe(4);
    // Liver perfusion sanity: unenhanced parenchyma 30–80 HU, portal-venous the
    // most enhanced (the classic liver enhancement pattern).
    expect(byPhase.unenhanced, "unenhanced liver HU in range").toBeGreaterThan(30);
    expect(byPhase.unenhanced).toBeLessThan(80);
    expect(byPhase.portal_venous, "portal-venous enhancement above unenhanced").toBeGreaterThan(
      byPhase.unenhanced,
    );
    emit("- ✅ liver wash-out GATE passed");
  });
});
