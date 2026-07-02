// Targeted prod verification of two viewer features left in Mycelium "verify":
//   cde63ced — annotation undo/redo + finding-class colour
//   3af7a33d — MedSAM-2 click-to-segment tool UI
//
// Both were shipped in frontend 4.4.108 but flagged "needs the running viewer
// + human eyes". This spec drives the LIVE viewer against prod with an owner
// JWT and asserts the interaction contracts via window.__viewer + the network.
//
// It is self-cleaning: any marker it creates that wasn't on the series before
// is deleted in afterAll, so the real study is left byte-identical.
//
// Run (browsers already installed):
//   E2E_USE_REAL_BACKEND=1 E2E_BASE_URL=https://bitvision.xeno.garden \
//   BVP_AUDIT_TOKEN=$TOK BVP_AUDIT_SERIES_ID=<uuid> BVP_AUDIT_PATIENT_ID=<uuid> \
//   pnpm exec playwright test feature-verify-cde63ced-3af7a33d

import fs from "node:fs";
import path from "node:path";
import { type Page, type Request, expect, test } from "@playwright/test";

const TOKEN = process.env.BVP_AUDIT_TOKEN;
const BASE_URL = process.env.E2E_BASE_URL;
const SERIES_ID = process.env.BVP_AUDIT_SERIES_ID;
const PATIENT_ID = process.env.BVP_AUDIT_PATIENT_ID;

const STAMP = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
const OUT_DIR = path.join(process.cwd(), "playwright", "reports");
const SHOTS = path.join(OUT_DIR, `feature-verify-${STAMP}`);
const REPORT = path.join(OUT_DIR, `feature-verify-${STAMP}.md`);

function emit(line: string): void {
  fs.appendFileSync(REPORT, `${line}\n`);
}
async function shot(page: Page, name: string): Promise<string> {
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false }).catch(() => {});
  return path.relative(OUT_DIR, file);
}
type Probe = {
  volume?: { dims?: number[]; hasGeometry?: boolean } | null;
  activeTool?: string | null;
  measurementCount?: number;
  undoDepth?: number;
  redoDepth?: number;
  notes?: string[];
  error?: string | null;
};
async function probe(page: Page): Promise<Probe | null> {
  return page.evaluate(
    () => (window as unknown as { __viewer?: Probe }).__viewer ?? null,
  ) as Promise<Probe | null>;
}
async function waitProbe<T>(
  page: Page,
  pick: (p: Probe) => T,
  ok: (v: T) => boolean,
  timeoutMs = 12_000,
): Promise<T> {
  const start = Date.now();
  let last: T = pick((await probe(page)) ?? {});
  while (Date.now() - start < timeoutMs) {
    const p = await probe(page);
    if (p) {
      last = pick(p);
      if (ok(last)) return last;
    }
    await page.waitForTimeout(400);
  }
  return last;
}

test.describe("feature verify (cde63ced + 3af7a33d)", () => {
  test.describe.configure({ timeout: 240_000 });
  test.skip(
    !TOKEN || !BASE_URL || !SERIES_ID || !PATIENT_ID,
    "set BVP_AUDIT_TOKEN + E2E_BASE_URL + BVP_AUDIT_SERIES_ID + BVP_AUDIT_PATIENT_ID",
  );

  const createdMarkerIds = new Set<string>();
  let baselineMarkerIds = new Set<string>();

  test.beforeAll(() => {
    fs.mkdirSync(SHOTS, { recursive: true });
    fs.writeFileSync(
      REPORT,
      `# Feature verify — ${new Date().toISOString()}\n\n- base: ${BASE_URL}\n- series: ${SERIES_ID}\n\n`,
    );
  });

  test.beforeEach(async ({ page }) => {
    const url = new URL(BASE_URL as string);
    await page.context().addCookies([
      {
        name: "bvp_session",
        value: TOKEN as string,
        domain: url.hostname,
        path: "/",
        httpOnly: true,
        secure: url.protocol === "https:",
        sameSite: "Lax",
      },
    ]);
  });

  test.afterAll(async ({ request }) => {
    // Delete only markers this run created — leave the real study untouched.
    for (const id of createdMarkerIds) {
      const res = await request
        .delete(`${BASE_URL}/api/markers/${id}`, {
          headers: { Authorization: `Bearer ${TOKEN}` },
        })
        .catch(() => null);
      emit(`- cleanup DELETE marker ${id} → ${res ? res.status() : "error"}`);
    }
  });

  async function openViewer(page: Page): Promise<Probe | null> {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto(`${BASE_URL}/viewer/series/${SERIES_ID}`, {
      waitUntil: "domcontentloaded",
    });
    await page
      .waitForFunction(
        () => {
          const v = (window as unknown as { __viewer?: { volume?: unknown } }).__viewer;
          return !!v?.volume || !!document.querySelector('[data-testid="viewer-error"]');
        },
        null,
        { timeout: 90_000 },
      )
      .catch(() => {});
    await page.waitForTimeout(2000);
    return probe(page);
  }

  async function canvasBoxes(page: Page) {
    const canvases = page.locator("canvas");
    const n = await canvases.count();
    const boxes: { i: number; x: number; y: number; w: number; h: number }[] = [];
    for (let i = 0; i < n; i++) {
      const b = await canvases
        .nth(i)
        .boundingBox()
        .catch(() => null);
      if (b && b.width > 120 && b.height > 120) {
        boxes.push({ i, x: b.x, y: b.y, w: b.width, h: b.height });
      }
    }
    // Largest first — the meatiest visible pane.
    boxes.sort((a, b) => b.w * b.h - a.w * a.h);
    return boxes;
  }

  test("3af7a33d — segment tool click produces a valid interactivePredict request", async ({
    page,
  }) => {
    emit("## 3af7a33d — MedSAM-2 click-to-segment\n");
    const predicts: { axis: unknown; slice_idx: unknown; points: unknown; label: unknown }[] = [];
    const statuses: number[] = [];
    page.on("request", (req: Request) => {
      if (req.url().includes("/segmentations/interactive/predict") && req.method() === "POST") {
        try {
          predicts.push(JSON.parse(req.postData() ?? "{}"));
        } catch {
          /* ignore */
        }
      }
    });
    page.on("response", (res) => {
      if (res.url().includes("/segmentations/interactive/predict")) statuses.push(res.status());
    });

    const p0 = await openViewer(page);
    expect(p0, "viewer probe present (instrumentation on + volume loaded)").toBeTruthy();
    emit(`- volume: \`${JSON.stringify(p0?.volume ?? null)}\``);
    emit(`- screenshot: \`${await shot(page, "01-loaded")}\``);

    // Activate the Segment tool.
    const segBtn = page.getByRole("button", { name: "Segment", exact: true });
    await segBtn.click();
    const tool = await waitProbe(
      page,
      (p) => p.activeTool ?? null,
      (v) => v === "segment",
      6000,
    );
    emit(`- activeTool after clicking Segment: \`${tool}\``);
    expect(tool, "segment tool activates").toBe("segment");
    emit(`- screenshot: \`${await shot(page, "02-segment-active")}\``);

    // Click the center of each visible pane; a center click lands on real
    // anatomy so ijkInBounds passes and the request fires.
    const boxes = await canvasBoxes(page);
    emit(`- visible panes: ${boxes.length}`);
    for (const b of boxes) {
      await page.mouse.click(b.x + b.w / 2, b.y + b.h / 2);
      await page.waitForTimeout(1500);
    }
    // Give the last request time to land + the error to render.
    await page.waitForTimeout(2500);
    emit(`- interactivePredict requests captured: ${predicts.length}`);
    for (const r of predicts) emit(`  - \`${JSON.stringify(r)}\``);
    emit(`- response statuses: \`${JSON.stringify(statuses)}\``);
    emit(`- screenshot: \`${await shot(page, "03-after-clicks")}\``);

    // Structural contract: a real in-bounds click yields axis∈{0,1,2}, an
    // integer slice_idx≥0, and points=[[col,row]] finite.
    expect(predicts.length, "at least one predict request from a center click").toBeGreaterThan(0);
    const seenAxes = new Set<number>();
    for (const r of predicts) {
      expect([0, 1, 2]).toContain(r.axis as number);
      expect(Number.isInteger(r.slice_idx as number) && (r.slice_idx as number) >= 0).toBe(true);
      const pts = r.points as number[][];
      expect(Array.isArray(pts) && pts.length === 1).toBe(true);
      expect(pts[0].length).toBe(2);
      expect(pts[0].every((n) => Number.isFinite(n) && n >= 0)).toBe(true);
      seenAxes.add(r.axis as number);
    }
    emit(`- distinct axes exercised: \`${JSON.stringify([...seenAxes])}\``);

    // Error handling: sam2 not installed in prod workers → 502 → friendly msg.
    if (statuses.some((s) => s === 502)) {
      const err = await page
        .locator("text=/MedSAM worker|isn't available/i")
        .first()
        .textContent()
        .catch(() => null);
      emit(`- 502 friendly error shown: \`${err?.trim() ?? "(text not located)"}\``);
      const probeErr = (await probe(page))?.error ?? null;
      emit(`- probe.error: \`${probeErr}\``);
    }
    emit("");
  });

  test("cde63ced — annotation undo/redo round-trip + class-colour map", async ({
    page,
    request,
  }) => {
    emit("## cde63ced — undo/redo + finding-class colour\n");

    // Baseline markers on the series (so cleanup deletes only what we add).
    const listUrl = `${BASE_URL}/api/patients/${PATIENT_ID}/markers?target_kind=series&target_id=${SERIES_ID}&limit=500`;
    const before = await request.get(listUrl, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    if (before.ok()) {
      const arr = (await before.json()) as { id: string }[];
      baselineMarkerIds = new Set(arr.map((m) => m.id));
      emit(`- baseline markers on series: ${baselineMarkerIds.size}`);
    } else {
      emit(`- ⚠️ baseline marker list → ${before.status()}`);
    }

    const p0 = await openViewer(page);
    expect(p0, "viewer probe present").toBeTruthy();
    const count0 = p0?.measurementCount ?? 0;
    emit(`- measurementCount at load: ${count0}, undoDepth: ${p0?.undoDepth}`);

    // Draw a Distance measurement (drag) on the largest pane.
    await page.getByRole("button", { name: "Distance", exact: true }).click();
    const boxes = await canvasBoxes(page);
    expect(boxes.length, "at least one pane").toBeGreaterThan(0);
    const b = boxes[0];
    const x1 = b.x + b.w * 0.42;
    const y1 = b.y + b.h * 0.45;
    const x2 = b.x + b.w * 0.58;
    const y2 = b.y + b.h * 0.55;
    await page.mouse.move(x1, y1);
    await page.mouse.down();
    await page.mouse.move((x1 + x2) / 2, (y1 + y2) / 2, { steps: 6 });
    await page.mouse.move(x2, y2, { steps: 6 });
    await page.mouse.up();

    // Wait for the create to persist (marker POST) and register in history.
    const undoDepth = await waitProbe(
      page,
      (p) => p.undoDepth ?? 0,
      (v) => v >= 1,
      15_000,
    );
    const afterDraw = await probe(page);
    const count1 = afterDraw?.measurementCount ?? count0;
    emit(`- after draw: measurementCount ${count0} → ${count1}, undoDepth ${undoDepth}`);
    emit(`- screenshot: \`${await shot(page, "10-drawn")}\``);
    expect(undoDepth, "a session-drawn annotation is undoable").toBeGreaterThanOrEqual(1);
    expect(count1, "draw created a measurement").toBeGreaterThan(count0);

    // Track any new marker id for cleanup, regardless of undo outcome.
    const midList = await request.get(listUrl, { headers: { Authorization: `Bearer ${TOKEN}` } });
    if (midList.ok()) {
      for (const m of (await midList.json()) as { id: string }[]) {
        if (!baselineMarkerIds.has(m.id)) createdMarkerIds.add(m.id);
      }
    }

    // Undo (Ctrl+Z) removes it.
    await page.keyboard.press("Control+z");
    const count2 = await waitProbe(
      page,
      (p) => p.measurementCount ?? count1,
      (v) => v === count0,
      12_000,
    );
    const afterUndo = await probe(page);
    emit(`- after Ctrl+Z: measurementCount → ${count2}, redoDepth ${afterUndo?.redoDepth}`);
    emit(`- screenshot: \`${await shot(page, "11-undone")}\``);
    expect(count2, "undo removed the drawn annotation").toBe(count0);
    expect(afterUndo?.redoDepth ?? 0, "undo populated the redo stack").toBeGreaterThanOrEqual(1);

    // Redo (Ctrl+Shift+Z) restores it.
    await page.keyboard.press("Control+Shift+z");
    const count3 = await waitProbe(
      page,
      (p) => p.measurementCount ?? count2,
      (v) => v > count0,
      12_000,
    );
    emit(`- after Ctrl+Shift+Z: measurementCount → ${count3}`);
    emit(`- screenshot: \`${await shot(page, "12-redone")}\``);
    expect(count3, "redo restored the annotation").toBeGreaterThan(count0);

    // Refresh created-marker set after the redo re-persist (new uid/id).
    const afterList = await request.get(listUrl, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    if (afterList.ok()) {
      for (const m of (await afterList.json()) as { id: string }[]) {
        if (!baselineMarkerIds.has(m.id)) createdMarkerIds.add(m.id);
      }
    }

    // Class-colour: verify the vocab→category map the feature relies on loads
    // and the study's findings expose categories (the colour source).
    const vocabRes = await request.get(`${BASE_URL}/api/findings/vocab`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    if (vocabRes.ok()) {
      const vocab = (await vocabRes.json()) as {
        finding_types?: { key: string; category: string }[];
      };
      const cats = new Set((vocab.finding_types ?? []).map((f) => f.category));
      emit(`- finding vocab categories: ${cats.size} (${[...cats].slice(0, 12).join(", ")})`);
      expect(cats.size, "finding vocabulary exposes categories for colour mapping").toBeGreaterThan(
        0,
      );
    } else {
      emit(`- ⚠️ /api/findings/vocab → ${vocabRes.status()}`);
    }
    emit("");
  });
});
