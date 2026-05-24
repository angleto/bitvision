// Visual UX audit spec for /viewer/series/[id].
//
// Goals
//   1. Enumerate every <h2> section heading in the right rail (the
//      "menù a destra" the user complains is a mess) for three
//      scenarios: CT primary, PT primary, CT+PT fusion.
//   2. Snapshot the rail and the canvas region so we can review the
//      visual density and ordering off-line.
//   3. Probe the rail's section-nav chips (SidebarSectionNav) and
//      record the chip labels.
//
// Mocks: we never hit FastAPI. ``volume.raw`` returns 504 so the
// canvas falls back to its error card; the chrome (rail + section
// nav + toggle buttons) renders independently.

import fs from "node:fs";
import path from "node:path";

import { type Page, type Route, expect, test } from "@playwright/test";

const SERIES_ID_CT = "11111111-1111-1111-1111-111111111111";
const SERIES_ID_PT = "22222222-2222-2222-2222-222222222222";
const SERIES_ID_FUSION = "33333333-3333-3333-3333-333333333333";
const STUDY_ID = "99999999-9999-9999-9999-999999999999";
const PATIENT_ID = "00000000-0000-0000-0000-000000000001";
const TOKEN = "audit-mock-token";

const OUT_DIR = process.env.AUDIT_DIR ?? "/tmp/bvp-viewer-audit";

async function jsonRoute(r: Route, body: unknown, status = 200) {
  await r.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function seedAuth(page: Page) {
  await page.addInitScript(
    ({ token }) => {
      window.localStorage.setItem("bvp.token", token);
    },
    { token: TOKEN },
  );
}

const ME = {
  subject_id: "00000000-0000-0000-0000-0000000000aa",
  email: "e2e@bv.test",
  display_name: "Audit",
  is_admin: true,
  email_verified: true,
};

function makeSeries(id: string, modality: "CT" | "PT") {
  return {
    id,
    study_id: STUDY_ID,
    modality,
    series_description: `Mock ${modality} axial`,
    series_number: modality === "CT" ? 1 : 2,
    received_instance_count: 100,
    packed: true,
    body_part_examined: modality === "CT" ? "CHEST" : "WHOLEBODY",
  };
}

const STUDY = {
  id: STUDY_ID,
  study_instance_uid: "1.2.3.4.5",
  patient_id: PATIENT_ID,
  study_date: "2026-04-01",
  study_description: "PT/CT FUSION MOCK",
  modalities_in_study: "CT,PT",
  series_count: 2,
  instance_count: 200,
  etag: "etag-study-1",
};

async function installCommonMocks(page: Page) {
  await page.route(/\/api\/.*/, (r) => r.fulfill({ status: 200, body: "[]" }));
  await page.route("**/api/auth/me", (r) => jsonRoute(r, ME));
  await page.route(/\/api\/jobs(\?.*)?$/, (r) => jsonRoute(r, { items: [] }));
  await page.route(/\/api\/studies\/[^/]+\/series/, (r) =>
    jsonRoute(r, [makeSeries(SERIES_ID_CT, "CT"), makeSeries(SERIES_ID_PT, "PT")]),
  );
  await page.route(/\/api\/studies\/[^/]+$/, (r) => jsonRoute(r, STUDY));
  await page.route(/\/api\/patients\/[^/]+$/, (r) =>
    jsonRoute(r, {
      id: PATIENT_ID,
      display_name: "Audit Patient",
      contacts: [],
    }),
  );
  await page.route(/\/api\/series\/.*\/volume\.raw.*/, (r) => r.fulfill({ status: 504, body: "" }));
  await page.route(/\/api\/series\/.*\/markers.*/, (r) => jsonRoute(r, []));
  await page.route(/\/api\/studies\/.*\/markers.*/, (r) => jsonRoute(r, []));
  await page.route(/\/api\/app\/settings\/public.*/, (r) => jsonRoute(r, {}));
}

async function rightRailReport(
  page: Page,
  scenarioLabel: string,
): Promise<{ chips: string[]; sections: string[]; raw: string }> {
  // Wait for the sidebar to mount.
  const sidebar = page.locator(".viewer-layout__sidebar");
  await expect(sidebar).toBeVisible({ timeout: 15_000 });

  const chips = await page
    .locator(".viewer-layout__sidebar .bv-section-chip, .viewer-layout__sidebar button")
    .filter({ hasText: /^[A-ZÀ-Ÿa-z][A-ZÀ-Ÿa-z /]+$/ })
    .allInnerTexts()
    .catch(() => [] as string[]);

  const h2s = await page.locator(".viewer-layout__sidebar h2").allInnerTexts();
  const h3s = await page.locator(".viewer-layout__sidebar h3").allInnerTexts();
  const summary = `### ${scenarioLabel}\n\nH2 headings (${h2s.length}):\n${h2s
    .map((s, i) => `  ${i + 1}. ${s.trim()}`)
    .join("\n")}\n\nH3 headings (${h3s.length}):\n${h3s
    .map((s, i) => `  ${i + 1}. ${s.trim()}`)
    .join("\n")}\n`;
  return { chips, sections: h2s, raw: summary };
}

async function snapshotRail(page: Page, fileBase: string) {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const sidebar = page.locator(".viewer-layout__sidebar").first();
  await sidebar.screenshot({ path: path.join(OUT_DIR, `${fileBase}.png`) });

  await page.screenshot({
    path: path.join(OUT_DIR, `${fileBase}-full.png`),
    fullPage: false,
  });
}

test.describe("Right rail UX audit", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1680, height: 1050 });
    page.on("pageerror", (err) => console.error("[page-error]", err.message));
    page.on("console", (msg) => {
      if (msg.type() === "error") console.warn("[console-error]", msg.text());
    });
  });

  test("CT primary, no fusion", async ({ page }) => {
    await seedAuth(page);
    await installCommonMocks(page);
    await page.route(`**/api/series/${SERIES_ID_CT}`, (r) =>
      jsonRoute(r, makeSeries(SERIES_ID_CT, "CT")),
    );
    await page.route(/\/api\/series\/.*\/display-metadata/, (r) =>
      jsonRoute(r, { is_pet: false, primary_plane: "axial", instance_count: 100 }),
    );

    await page.goto(`/viewer/series/${SERIES_ID_CT}`);
    const r = await rightRailReport(page, "CT primary (no fusion)");
    await snapshotRail(page, "ct-only");
    fs.appendFileSync(path.join(OUT_DIR, "report.md"), r.raw + "\n");
    expect(r.sections.length).toBeGreaterThan(0);
  });

  test("PT primary, no fusion", async ({ page }) => {
    await seedAuth(page);
    await installCommonMocks(page);
    await page.route(`**/api/series/${SERIES_ID_PT}`, (r) =>
      jsonRoute(r, makeSeries(SERIES_ID_PT, "PT")),
    );
    await page.route(/\/api\/series\/.*\/display-metadata/, (r) =>
      jsonRoute(r, {
        is_pet: true,
        primary_plane: "axial",
        instance_count: 200,
        suv_factor: 0.0001,
        suv_factor_bw: 0.0001,
        suv_factor_sul_janma: 0.0001,
        radionuclide_total_dose_bq: 370_000_000,
        radiopharmaceutical: "F-18 FDG",
        patient_weight_kg: 70,
        units: "BQML",
      }),
    );

    await page.goto(`/viewer/series/${SERIES_ID_PT}`);
    const r = await rightRailReport(page, "PT primary (no fusion)");
    await snapshotRail(page, "pt-only");
    fs.appendFileSync(path.join(OUT_DIR, "report.md"), r.raw + "\n");
    expect(r.sections.length).toBeGreaterThan(0);
  });
});
