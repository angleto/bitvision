// Scroll the right rail and snapshot every chunk so we can audit the
// full section list (some sections are only visible after scrolling).

import fs from "node:fs";
import path from "node:path";

import { type Page, type Route, expect, test } from "@playwright/test";

const SERIES_ID = "22222222-2222-2222-2222-222222222222";
const STUDY_ID = "99999999-9999-9999-9999-999999999999";
const PATIENT_ID = "00000000-0000-0000-0000-000000000001";
const TOKEN = "audit-mock-token";
const OUT_DIR = process.env.AUDIT_DIR ?? "/tmp/bvp-viewer-audit";

async function jsonRoute(r: Route, body: unknown) {
  await r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

test("Full rail scroll capture", async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 1050 });
  await page.addInitScript(({ token }) => window.localStorage.setItem("bvp.token", token), {
    token: TOKEN,
  });
  await page.route(/\/api\/.*/, (r) => r.fulfill({ status: 200, body: "[]" }));
  await page.route("**/api/auth/me", (r) =>
    jsonRoute(r, {
      subject_id: "00000000-0000-0000-0000-0000000000aa",
      email: "e2e@bv.test",
      display_name: "A",
      is_admin: true,
      email_verified: true,
    }),
  );
  await page.route(/\/api\/jobs(\?.*)?$/, (r) => jsonRoute(r, { items: [] }));
  await page.route(`**/api/series/${SERIES_ID}`, (r) =>
    jsonRoute(r, {
      id: SERIES_ID,
      study_id: STUDY_ID,
      modality: "PT",
      series_description: "Mock PT axial WB",
      series_number: 2,
      received_instance_count: 250,
      packed: true,
      body_part_examined: "WHOLEBODY",
    }),
  );
  await page.route(/\/api\/series\/.*\/display-metadata/, (r) =>
    jsonRoute(r, {
      is_pet: true,
      primary_plane: "axial",
      instance_count: 250,
      suv_factor_bw: 0.0001,
      radionuclide_total_dose_bq: 370_000_000,
      patient_weight_kg: 70,
      units: "BQML",
    }),
  );
  await page.route(/\/api\/series\/.*\/volume\.raw.*/, (r) => r.fulfill({ status: 504, body: "" }));
  await page.route(/\/api\/studies\/.*\/series/, (r) => jsonRoute(r, []));
  await page.route(/\/api\/studies\/.*$/, (r) =>
    jsonRoute(r, {
      id: STUDY_ID,
      patient_id: PATIENT_ID,
      study_instance_uid: "1.2.3",
      modalities_in_study: "PT",
      etag: "etag-s1",
    }),
  );

  await page.goto(`/viewer/series/${SERIES_ID}`);

  const sidebarScroll = page.locator(".viewer-layout__sidebar-scroll").first();
  await expect(sidebarScroll).toBeVisible({ timeout: 15_000 });

  fs.mkdirSync(OUT_DIR, { recursive: true });

  const totalH = await sidebarScroll.evaluate((el) => el.scrollHeight);
  const visibleH = await sidebarScroll.evaluate((el) => el.clientHeight);
  console.log(`rail scrollHeight=${totalH} clientHeight=${visibleH}`);

  let i = 0;
  let offset = 0;
  while (offset < totalH) {
    await sidebarScroll.evaluate((el, y) => {
      el.scrollTop = y;
    }, offset);
    await page.waitForTimeout(150);
    await sidebarScroll.screenshot({
      path: path.join(OUT_DIR, `rail-scroll-${String(i).padStart(2, "0")}.png`),
    });
    offset += visibleH - 80; // overlap of 80px
    i++;
    if (i > 20) break;
  }

  // Section nav chips
  const chips = await page
    .locator(".viewer-layout__sidebar a, .viewer-layout__sidebar button")
    .allInnerTexts();
  const allH2 = await page.locator(".viewer-layout__sidebar h2").allInnerTexts();
  const ratio = (totalH / visibleH).toFixed(2);
  fs.writeFileSync(
    path.join(OUT_DIR, "full-rail.md"),
    `# Full rail capture (PT primary, no fusion volume)\n\nscrollHeight=${totalH}, clientHeight=${visibleH}, vertical-pages=${ratio}\n\nH2 sections (${allH2.length}):\n${allH2.map((s, i) => `${i + 1}. ${s.trim()}`).join("\n")}\n`,
  );
});
