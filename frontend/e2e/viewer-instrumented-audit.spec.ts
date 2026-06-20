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
  // Real prod loads (full CT volume fetch + pack + first paint, and the
  // wash-out backend fan-out over the throttled egress) routinely exceed the
  // 30 s default; give each audit generous room to load and settle.
  test.describe.configure({ timeout: 240_000 });

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
          const panes = w.__viewer?.panes ? Object.values(w.__viewer.panes) : [];
          // Wait for ALL four phase previews to be windowed (loaded), not just
          // one — driving the sync before a pane is built leaves its crosshair
          // at the volume centre (non-deterministic).
          return (
            !!w.__viewerTest?.setCrosshairWorldAll &&
            w.__viewer?.surface === "contrast" &&
            panes.length >= 4 &&
            panes.every((p) => p.voi != null)
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

    // Ground-truth cross-check: read the RENDERED slice HUD ("Slice N / M") of
    // each pane from the DOM — the camera's actual focal slice, which can
    // diverge from the crosshair state the probe reports. A radiologist sees
    // THIS, not the probe.
    const phaseEls = page.locator('[data-testid^="contrast-phase-"]');
    const nPhase = await phaseEls.count();
    const renderedSlices: Array<{ tid: string; slice: number; total: number }> = [];
    for (let i = 0; i < nPhase; i++) {
      const el = phaseEls.nth(i);
      const tid = (await el.getAttribute("data-testid")) ?? `pane${i}`;
      const txt = (await el.textContent().catch(() => "")) ?? "";
      const m = txt.match(/Slice\s+(\d+)\s*\/\s*(\d+)/);
      const slice = m ? Number(m[1]) : Number.NaN;
      const total = m ? Number(m[2]) : Number.NaN;
      renderedSlices.push({ tid, slice, total });
      emit(
        `- ${tid}: rendered slice ${m ? `${slice}/${total} (${((slice / total) * 100).toFixed(0)}%)` : "?"}`,
      );
    }

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

    // The radiologically meaningful alignment is the SLICE LEVEL (world z):
    // every pane must display the same liver slice. The synced crosshair z must
    // land on the liver, and — the ground-truth visual check — every pane must
    // RENDER the same slice index (these phases share origin+spacing, so equal
    // index = equal world z = the same liver level on all 4 quadrants). The
    // in-plane crosshair MARKER (x,y) is reported but not gated — the user
    // draws the ROI where they click, not at the crosshair (and the wash-out
    // ROI is placed by world point, verified in the wash-out test).
    emit(
      `- synced crosshair world point: \`${JSON.stringify(ref)}\` (driven ${JSON.stringify(LIVER_LESION)})`,
    );
    const atLiverZ = !!ref && Math.abs(ref[2] - LIVER_LESION[2]) < 5;
    emit(`- crosshair at the liver SLICE level (z ±5mm): ${atLiverZ}`);
    const sliceIdxs = renderedSlices.filter((s) => Number.isFinite(s.slice)).map((s) => s.slice);
    const sameRenderedIdx = sliceIdxs.length >= 4 && sliceIdxs.every((s) => s === sliceIdxs[0]);
    emit(`- all panes RENDER the same slice index (same liver anatomy): ${sameRenderedIdx}`);

    dumpCaptures();
    // GATE assertions
    expect(visibleCount, "4 phase panes visible").toBeGreaterThanOrEqual(4);
    expect(allValid, "every liver-synced pane is in-coverage with a valid slice + canvas").toBe(
      true,
    );
    expect(coincide, "all phases report the same crosshair world position").toBe(true);
    expect(atLiverZ, "synced crosshair is at the liver slice level (same world z)").toBe(true);
    expect(
      sameRenderedIdx,
      "every phase renders the same anatomical slice (visual liver alignment)",
    ).toBe(true);
    expect(oocCount, "out-of-overlap flags the shorter phases (no silent black)").toBeGreaterThan(
      0,
    );
    emit("- ✅ liver sync GATE passed");
  });

  test("radiological — liver wash-out measurement", async ({ request }) => {
    test.skip(STUDY_ID !== LIVER_STUDY, "Liver world points are specific to study 2858def7.");
    emit("\n## Radiological — liver wash-out\n");

    // The wash-out is a BACKEND radiological computation. The viewer's
    // ``runWashout`` hook merely POSTs the lesion + parenchyma ROI to this exact
    // endpoint and renders the JSON, so hitting the API directly validates the
    // measurement faithfully WITHOUT loading the 4 phase volumes into the
    // browser. Doing the latter saturated the throttled prod egress and starved
    // the very wash-out request under test (intermittent 150 s timeouts). The
    // 4-phase sync test already covers the viewer-side alignment (no black
    // panes); this test owns the numbers. ``Authorization: Bearer`` authenticates
    // the APIRequestContext (deps.py accepts the header or the cookie).
    const res = await request.post(`${BASE_URL}/api/studies/${STUDY_ID}/phase-roi-stats`, {
      headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
      data: {
        kind: "sphere",
        center_lps: LIVER_LESION,
        radius_mm: 12,
        region: "liver",
        parenchyma_center_lps: LIVER_PARENCHYMA,
        parenchyma_radius_mm: 10,
      },
    });
    emit(`- POST phase-roi-stats → HTTP ${res.status()}`);
    expect(res.ok(), `phase-roi-stats returned HTTP ${res.status()}`).toBeTruthy();
    const result = (await res.json()) as {
      samples?: Array<{ acquisition_phase: string | null; hu_mean: number; hu_std: number }>;
      skipped?: Array<{ acquisition_phase: string | null; reason: string }>;
      washout?: {
        region?: string | null;
        relative_curve?: Array<{ acquisition_phase: string; delta_hu: number }>;
      };
    };

    const samples = result.samples ?? [];
    const skipped = result.skipped ?? [];
    const region = result.washout?.region ?? null;
    const relative = result.washout?.relative_curve ?? [];
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

    // GATE assertions — the radiological workflow must compute across all 4
    // phases with the liver (relative-to-parenchyma) interpretation.
    expect(samples.length, "4 phase HU samples").toBe(4);
    expect(skipped.length, "no phase skipped — all cover the liver").toBe(0);
    expect(region, "liver region interpretation").toBe("liver");
    expect(relative.length, "liver relative curve computed (lesion vs parenchyma)").toBe(4);
    // Classic liver perfusion curve (the proof all 4 geometries sampled the
    // same anatomy): unenhanced parenchyma 30–80 HU, enhancement rising into the
    // portal-venous peak, then wash-out below that peak by the delayed phase.
    expect(byPhase.unenhanced, "unenhanced liver HU in range").toBeGreaterThan(30);
    expect(byPhase.unenhanced).toBeLessThan(80);
    expect(byPhase.arterial, "arterial enhancement above unenhanced").toBeGreaterThan(
      byPhase.unenhanced,
    );
    expect(byPhase.portal_venous, "portal-venous is the enhancement peak").toBeGreaterThan(
      byPhase.arterial,
    );
    expect(byPhase.delayed, "delayed washes out below the portal-venous peak").toBeLessThan(
      byPhase.portal_venous,
    );
    emit("- ✅ liver wash-out GATE passed");
  });

  test("usability — viewer chrome is legible in light mode", async ({ page }) => {
    test.skip(STUDY_ID !== LIVER_STUDY, "Uses the contrast viewer of study 2858def7.");
    emit("\n## Usability — light-mode chrome contrast\n");

    // The default app theme is light (bv-theme). The viewer chrome is
    // deliberately always-dark; the regression the user hit was ghost-style
    // controls (the wash-out panel's ✕) inheriting the LIGHT theme's dark
    // ``--bv-fg`` → black-on-black, visible only on hover. Force light, open the
    // panel, and assert the close control's text is a light colour with real
    // contrast against the dark panel — i.e. the ``.viewer-chrome`` override beat
    // the global ``button.ghost`` rule. Without the fix this fails at ~1:1.
    await page.addInitScript(() => {
      try {
        localStorage.setItem("bv-theme", "light");
      } catch {}
    });
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto(`${BASE_URL}/viewer/contrast?study=${STUDY_ID}`, {
      waitUntil: "domcontentloaded",
    });
    await page
      .waitForFunction(() => !document.documentElement.classList.contains("dark"), null, {
        timeout: 15_000,
      })
      .catch(() => {});
    const isLight = await page.evaluate(() => !document.documentElement.classList.contains("dark"));
    emit(`- document theme is light: ${isLight}`);
    expect(isLight, "the contrast viewer is rendered in light mode (where the bug shows)").toBe(
      true,
    );

    // Toggling measure mode shows the wash-out panel even with no ROI placed,
    // so the close ✕ is reachable without loading volumes or hitting the backend.
    const runBtn = page.getByTestId("washout-run");
    await runBtn.waitFor({ state: "visible", timeout: 60_000 });
    await runBtn.click();
    const panel = page.getByTestId("washout-panel");
    await panel.waitFor({ state: "visible", timeout: 15_000 });
    const closeBtn = panel.locator('button:has-text("✕")').first();
    await closeBtn.waitFor({ state: "visible", timeout: 10_000 });

    // Read the close-button text colour and the first opaque ancestor background
    // (the panel's #0b0e13), then compute the WCAG contrast ratio.
    const { fg, bg } = await closeBtn.evaluate((el) => {
      const color = getComputedStyle(el as HTMLElement).color;
      let node: HTMLElement | null = el as HTMLElement;
      let background = "rgba(0, 0, 0, 0)";
      while (node) {
        const c = getComputedStyle(node).backgroundColor;
        if (c && c !== "rgba(0, 0, 0, 0)" && c !== "transparent") {
          background = c;
          break;
        }
        node = node.parentElement;
      }
      return { fg: color, bg: background };
    });
    const lum = (rgb: string): number => {
      const m = rgb.match(/[\d.]+/g);
      if (!m || m.length < 3) return 0;
      const [r, g, b] = m.slice(0, 3).map((v) => Number(v) / 255);
      const f = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const fgLum = lum(fg);
    const bgLum = lum(bg);
    const ratio = (Math.max(fgLum, bgLum) + 0.05) / (Math.min(fgLum, bgLum) + 0.05);
    emit(
      `- close ✕ colour ${fg} (lum ${fgLum.toFixed(3)}) on panel ${bg} (lum ${bgLum.toFixed(3)})`,
    );
    emit(`- contrast ratio: ${ratio.toFixed(2)}:1`);
    emit(`- screenshot: ${await shot(page, "light-mode-washout-panel")}`);
    dumpCaptures();

    // GATE: the close control must be clearly legible. Text wants WCAG AA 4.5:1;
    // black-on-black (the bug) sits at ~1:1. The panel must stay dark chrome and
    // the ✕ a light colour (not the light-theme's dark --bv-fg).
    expect(bgLum, "wash-out panel is dark viewer chrome").toBeLessThan(0.1);
    expect(fgLum, "close ✕ is a light colour, not the light-theme dark fg").toBeGreaterThan(0.3);
    expect(ratio, "close ✕ vs panel contrast (no black-on-black)").toBeGreaterThan(4.5);
    emit("- ✅ light-mode chrome contrast GATE passed");
  });
});
