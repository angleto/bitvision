// Desktop UX regression spec for the mobile/responsive work landed in
// `feat(F): UI mobile per Documents + Viewer (axial-only) + browser-support gate`.
//
// Scope (desktop only — mobile would need viewport + device emulation
// runs that the user explicitly de-scoped for this iteration):
//
//   1. `BrowserSupportGate` — when WebGL2 is unavailable the gate
//      panel ("Browser non supportato") renders; when it IS available
//      the gate transparently mounts its children.
//   2. `/patients/<id>/documents/<docId>` — desktop renders the
//      two-column grid (`.bv-doc-grid`), the document filename and
//      type, and the `DocumentPreview` aside. The grid is NOT
//      collapsed (i.e. the sticky NotesPanel sibling is present).
//   3. `/patients/<id>` Drive layout — `.bv-folder-tree` aside is
//      visible (no `display: none`) at 1280×800.
//   4. `/viewer/series/<id>` — the viewer page mounts with the
//      desktop variant: `.viewer-layout` is present, NOT
//      `.viewer-layout--mobile`.
//
// All tests are hermetic: backend calls are stubbed via `page.route`.
// We never hit the FastAPI service, never decode a real volume, and
// never wait for Cornerstone to render — for (4) we just verify the
// chrome sets up before the volume request fires (the request is
// stubbed to a 504 so the rest of the page renders the error card).

import { type Page, type Route, expect, test } from "@playwright/test";

const PATIENT_ID = "00000000-0000-0000-0000-000000000001";
const DOC_ID = "11111111-1111-1111-1111-111111111111";
const SERIES_ID = "22222222-2222-2222-2222-222222222222";
const STUDY_ID = "33333333-3333-3333-3333-333333333333";
const AUTH_TOKEN = "e2e-mock-token";

async function jsonRoute(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function seedAuth(page: Page) {
  // The app reads the JWT from `localStorage["bvp.token"]` (see
  // TOKEN_STORAGE_KEY in src/lib/api.ts); the value is opaque here
  // because every API call is mocked.
  await page.addInitScript(
    ({ token }) => {
      window.localStorage.setItem("bvp.token", token);
    },
    { token: AUTH_TOKEN },
  );
}

/**
 * Install the global chrome mocks every fascicolo / viewer test needs:
 * `/api/auth/me` (AuthProvider), `/api/jobs?active=…` (ActiveJobsPanel
 * in SiteHeader). Both crash hard on mismatched shapes — the auth path
 * blows up `user.contacts.filter`, the jobs path blows up
 * `jobs.length`. Call AFTER the test's catch-all and BEFORE any test-
 * specific stub so LIFO matching still favours the latter.
 */
async function installChromeMocks(page: Page) {
  await page.route("**/api/auth/me", (r) =>
    jsonRoute(r, {
      subject_id: "00000000-0000-0000-0000-0000000000aa",
      email: "e2e@bv.test",
      display_name: "E2E Tester",
      is_admin: true,
      email_verified: true,
    }),
  );
  // /api/jobs?active=… returns JobListOut (= {items: [...]}). Catch-all
  // body of `[]` makes `list.items` undefined and the `.length` call in
  // ActiveJobsPanel surfaces as the Next.js error boundary.
  await page.route(/\/api\/jobs(\?.*)?$/, (r) => jsonRoute(r, { items: [] }));
}

// Minimal fixtures — only the fields the components actually read.

// Full Patient shape (lib/api.ts → Patient interface). Several
// components dereference fields like `patient.contacts.length` or
// `patient.notes` without optional-chaining, so missing keys here turn
// into "Cannot read properties of undefined" crashes that surface as
// the Next.js error boundary in the test snapshot.
const PATIENT = {
  id: PATIENT_ID,
  display_name: "Mock Patient",
  external_id: null,
  birth_date: "1980-01-01",
  sex: "F",
  tax_id: null,
  phone: null,
  email: null,
  address: null,
  blood_type: null,
  birth_place_city: null,
  birth_place_province: null,
  asl_code: null,
  asl_name: null,
  allergies: null,
  notes: null,
  contacts: [],
  managed_by_subject_id: "00000000-0000-0000-0000-0000000000aa",
  self_user_subject_id: null,
  origin: "mine" as const,
  created_at: "2025-01-01T00:00:00Z",
  etag: "etag-mock-1",
};

const DOCUMENT_PDF = {
  id: DOC_ID,
  patient_id: PATIENT_ID,
  title: "Referto MOCK",
  document_type: "referto",
  document_date: "2025-04-01",
  text: null,
  file_s3_key: "mock/referto.pdf",
  file_content_type: "application/pdf",
  file_size_bytes: 12345,
  files: [],
  created_at: "2025-04-02T00:00:00Z",
  updated_at: "2025-04-02T00:00:00Z",
};

const TREE_ROOT = {
  folder_id: null,
  path: "/",
  parent_path: null,
  nodes: [
    {
      id: "f1",
      type: "folder" as const,
      name: "Referti",
      path: "/Referti",
      parent_path: "/",
      target_id: null,
      item_count: 1,
    },
  ],
};

const SERIES = {
  id: SERIES_ID,
  study_id: STUDY_ID,
  modality: "CT",
  series_description: "Mock CT axial",
  series_number: 1,
  received_instance_count: 100,
  packed: true,
};

// ---------------------------------------------------------------------
// (1) BrowserSupportGate
// ---------------------------------------------------------------------

test.describe("BrowserSupportGate", () => {
  test("shows the unsupported panel when WebGL2 is unavailable", async ({ page }) => {
    await seedAuth(page);
    // Stub `getContext("webgl2")` to return null BEFORE the React tree
    // mounts — this is exactly the symptom the gate is meant to catch
    // (mobile Safari < 15, in-app webviews, headless engines without GL).
    await page.addInitScript(() => {
      const orig = HTMLCanvasElement.prototype.getContext;
      // biome-ignore lint/suspicious/noExplicitAny: monkeypatch
      (HTMLCanvasElement.prototype as any).getContext = function patched(
        // biome-ignore lint/suspicious/noExplicitAny: monkeypatch
        type: any,
        // biome-ignore lint/suspicious/noExplicitAny: monkeypatch
        ...rest: any[]
      ) {
        if (type === "webgl2") return null;
        return orig.apply(this, [type, ...rest]);
      };
    });
    // The page itself fetches volume metadata; stub everything to 504
    // so we don't depend on a backend.
    await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 504, body: "" }));

    await page.goto(`/viewer/cornerstone/${SERIES_ID}`);

    // The gate panel is `role=alert` and carries the localized title.
    // Next.js injects its own `__next-route-announcer__` div with the
    // same role, so we filter by the panel's text content.
    const alert = page.locator('[role="alert"]').filter({
      hasText: /Browser non supportato|Browser not supported/,
    });
    await expect(alert).toBeVisible();
  });

  test("renders children when WebGL2 is available", async ({ page }) => {
    await seedAuth(page);
    await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 504, body: "" }));

    await page.goto(`/viewer/cornerstone/${SERIES_ID}`);

    // The gate transparently passes through; the unsupported panel must
    // NOT appear. Filter by the panel's text so we don't false-positive
    // on the Next.js route announcer.
    await page.waitForTimeout(300);
    const gateAlert = page.locator('[role="alert"]').filter({
      hasText: /Browser non supportato|Browser not supported/,
    });
    await expect(gateAlert).toHaveCount(0);
  });
});

// ---------------------------------------------------------------------
// (2) Document detail page
// ---------------------------------------------------------------------

test.describe("Document detail (desktop)", () => {
  test("renders the two-column grid and the PDF preview iframe", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await seedAuth(page);

    // Catch any uncaught client error so the test runner surfaces it
    // instead of a generic Next.js error boundary.
    page.on("pageerror", (err) => console.error("[page-error]", err.message));

    // Playwright matches handlers LIFO: catch-all FIRST, common
    // chrome mocks NEXT, test-specific stubs LAST.
    await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 200, body: "[]" }));
    await installChromeMocks(page);
    await page.route(`**/api/patients/${PATIENT_ID}`, (r) => jsonRoute(r, PATIENT));
    await page.route(`**/api/patients/${PATIENT_ID}/documents/${DOC_ID}`, (r) =>
      jsonRoute(r, DOCUMENT_PDF),
    );
    // useDocumentCatalog hits /api/document-catalog and expects the
    // full {kinds, provenances, authorities} triple — partial shapes
    // crash the consumer's `.find()` calls.
    await page.route(/\/api\/document-catalog.*/, (r) =>
      jsonRoute(r, { kinds: [], provenances: [], authorities: [] }),
    );
    await page.route(/\/api\/patients\/[^/]+\/notes(\?.*)?$/, (r) => jsonRoute(r, []));
    // Document binary: respond with a tiny PDF so the iframe has
    // something to load (we don't assert on the iframe contents).
    await page.route(`**/api/patients/${PATIENT_ID}/documents/${DOC_ID}/content`, (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/pdf",
        body: Buffer.from("%PDF-1.4\n%mock\n"),
      }),
    );

    await page.goto(`/patients/${PATIENT_ID}/documents/${DOC_ID}`);

    // The h1 carries the document title.
    await expect(page.getByRole("heading", { name: "Referto MOCK" })).toBeVisible();

    // Two-column desktop grid: `.bv-doc-grid` exists and the sticky
    // notes aside is rendered as a sibling (sibling check is loose —
    // we just verify the grid class is on the container).
    const grid = page.locator(".bv-doc-grid");
    await expect(grid).toBeVisible();
    // At 1280px the grid is two columns: gridTemplateColumns should
    // contain a non-1fr value (the 320-380px notes column). We probe
    // via computed style.
    const cols = await grid.evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(cols.split(" ").length).toBeGreaterThanOrEqual(2);

    // The document preview iframe is mounted (desktop branch — the
    // mobile CTA card would render <a>/<button> instead, no iframe).
    const iframe = page.locator("iframe[title='document preview']");
    await expect(iframe).toBeVisible({ timeout: 5_000 });
  });
});

// ---------------------------------------------------------------------
// (3) Patient Drive layout — folder tree visible on desktop
// ---------------------------------------------------------------------

test.describe("Patient Drive layout (desktop)", () => {
  test("FolderTree aside is visible at 1280px", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await seedAuth(page);

    // Catch-all first (LIFO matching), then chrome, then test-specific.
    await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 200, body: "[]" }));
    await installChromeMocks(page);
    await page.route(`**/api/patients/${PATIENT_ID}`, (r) => jsonRoute(r, PATIENT));
    await page.route(/\/api\/patients\/[^/]+\/tree.*/, (r) => jsonRoute(r, TREE_ROOT));
    await page.route(/\/api\/patients\/[^/]+\/folders.*/, (r) => jsonRoute(r, { folders: [] }));

    await page.goto(`/patients/${PATIENT_ID}`);

    const tree = page.locator(".bv-folder-tree").first();
    await expect(tree).toBeVisible({ timeout: 10_000 });
    // Confirm the CSS rule that hides it on mobile is NOT applied at
    // desktop width.
    const display = await tree.evaluate((el) => getComputedStyle(el).display);
    expect(display).not.toBe("none");
  });
});

// ---------------------------------------------------------------------
// (4) Viewer chrome — desktop layout class set, mobile class absent
// ---------------------------------------------------------------------

test.describe("Viewer chrome (desktop)", () => {
  test("renders viewer-layout without --mobile modifier at 1440px", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await seedAuth(page);

    // Catch-all first (LIFO matching), then chrome, then test-specific.
    await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 200, body: "[]" }));
    await installChromeMocks(page);
    await page.route(`**/api/series/${SERIES_ID}`, (r) => jsonRoute(r, SERIES));
    await page.route(/\/api\/series\/.*\/display-metadata/, (r) => jsonRoute(r, { is_pet: false }));
    // volume.raw — return a 504 so the viewer falls back to the error
    // card rather than waiting for a real volume. The chrome (sidebar,
    // toggle button, layout class) must render regardless.
    await page.route(/\/api\/series\/.*\/volume\.raw.*/, (r) =>
      r.fulfill({ status: 504, body: "" }),
    );

    await page.goto(`/viewer/series/${SERIES_ID}`);

    const layout = page.locator(".viewer-layout").first();
    await expect(layout).toBeVisible({ timeout: 10_000 });
    const cls = await layout.evaluate((el) => el.className);
    expect(cls).toContain("viewer-layout");
    expect(cls).not.toContain("viewer-layout--mobile");
  });
});
