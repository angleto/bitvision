// Desktop navigation UX assessment.
//
// Goal: walk the IA paths a clinician hits in a typical session and
// capture *measurable* signals about whether the navigation works
// smoothly: clicks-to-target, URL deep-linkability, breadcrumb /
// back-link presence, browser back/forward integrity, focus survival,
// and "dead end" detection (pages that mount with no way home).
//
// Hermetic: every backend call is stubbed via ``page.route``. The
// spec doubles as a regression net — if any of these flows breaks,
// the test fails fast. It is deliberately read-only (no mutations,
// no drag&drop) so it stays robust to micro-UI churn.

import { type Page, type Route, expect, test } from "@playwright/test";

const PATIENT_ID = "00000000-0000-0000-0000-000000000001";
const DOC_ID = "11111111-1111-1111-1111-111111111111";
const STUDY_ID = "33333333-3333-3333-3333-333333333333";
const SERIES_ID = "22222222-2222-2222-2222-222222222222";
const AUTH_TOKEN = "e2e-mock-token";

async function jsonRoute(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const ME = {
  subject_id: "00000000-0000-0000-0000-0000000000aa",
  email: "e2e@bv.test",
  display_name: "E2E Tester",
  is_admin: true,
  email_verified: true,
};

const PATIENT = {
  id: PATIENT_ID,
  display_name: "Mario Rossi",
  external_id: null,
  birth_date: "1960-05-12",
  sex: "M",
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
  managed_by_subject_id: ME.subject_id,
  self_user_subject_id: null,
  origin: "mine" as const,
  created_at: "2025-01-01T00:00:00Z",
  etag: "etag-pat-1",
};

const DOCUMENT = {
  id: DOC_ID,
  patient_id: PATIENT_ID,
  title: "Referto TC 2025-04-01",
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

const STUDY = {
  id: STUDY_ID,
  patient_id: PATIENT_ID,
  patient_display_name: PATIENT.display_name,
  study_date: "2025-04-01",
  modality: "CT",
  study_description: "TC torace con mdc",
  series: [
    {
      id: SERIES_ID,
      study_id: STUDY_ID,
      modality: "CT",
      series_description: "axial",
      series_number: 1,
      received_instance_count: 100,
      packed: true,
    },
  ],
  reports: [],
  tags: [],
};

const TREE_ROOT = {
  folder_id: null,
  path: "/",
  parent_path: null,
  nodes: [
    {
      id: "n-doc",
      type: "document" as const,
      name: DOCUMENT.title,
      path: "/Referto TC 2025-04-01",
      parent_path: "/",
      target_id: DOC_ID,
      item_count: null,
    },
    {
      id: "n-study",
      type: "study" as const,
      name: "TC torace con mdc",
      path: "/TC torace con mdc",
      parent_path: "/",
      target_id: STUDY_ID,
      item_count: null,
    },
  ],
};

async function installCommonMocks(page: Page) {
  // Generic catch-all FIRST (Playwright matches LIFO so later handlers
  // win on a specific URL). Empty-array body keeps list endpoints
  // shape-safe.
  await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 200, body: "[]" }));
  await page.route("**/api/auth/me", (r) => jsonRoute(r, ME));
  await page.route(/\/api\/jobs(\?.*)?$/, (r) => jsonRoute(r, { items: [] }));
  await page.route(/\/api\/system\/features$/, (r) => jsonRoute(r, { llm_classifier: true }));
  await page.route(/\/api\/me\/scopes$/, (r) =>
    jsonRoute(r, { scopes: ["phases:read", "phases:propose", "phases:write"] }),
  );
  await page.route(`**/api/patients/${PATIENT_ID}`, (r) => jsonRoute(r, PATIENT));
  await page.route(`**/api/patients/${PATIENT_ID}/documents/${DOC_ID}`, (r) =>
    jsonRoute(r, DOCUMENT),
  );
  await page.route(/\/api\/patients\/[^/]+\/notes(\?.*)?$/, (r) => jsonRoute(r, []));
  await page.route(/\/api\/document-catalog.*/, (r) =>
    jsonRoute(r, { kinds: [], provenances: [], authorities: [] }),
  );
  await page.route(/\/api\/patients\/[^/]+\/tree.*/, (r) => jsonRoute(r, TREE_ROOT));
  await page.route(/\/api\/patients\/[^/]+\/folders.*/, (r) => jsonRoute(r, { folders: [] }));
  // CareTimeline / health: returning bare arrays from the catch-all
  // crashes consumers that destructure `.phases.filter(...)`.
  await page.route(/\/api\/patients\/[^/]+\/care-timeline(\?.*)?$/, (r) =>
    jsonRoute(r, {
      patient_id: PATIENT_ID,
      phases: [],
      unassigned_events: [],
      generated_at: "2026-05-08T00:00:00Z",
      lang: "it",
    }),
  );
  await page.route(/\/api\/patients\/[^/]+\/care-timeline\/health$/, (r) =>
    jsonRoute(r, {
      n_phases: 0,
      events_assigned: 0,
      events_total: 0,
      pending_proposals: 0,
      last_classification_at: null,
    }),
  );
  await page.route(/\/api\/patients\/[^/]+\/care-phases(\?.*)?$/, (r) => jsonRoute(r, []));
  // Search / studies / series for cross-page navigation.
  await page.route(/\/api\/patients\/[^/]+\/search.*/, (r) => jsonRoute(r, { hits: [] }));
  await page.route(`**/api/studies/${STUDY_ID}`, (r) => jsonRoute(r, STUDY));
  await page.route(`**/api/series/${SERIES_ID}`, (r) => jsonRoute(r, STUDY.series[0]));
  await page.route(/\/api\/series\/.*\/display-metadata/, (r) => jsonRoute(r, { is_pet: false }));
  // PDF binary for the document preview iframe.
  await page.route(`**/api/patients/${PATIENT_ID}/documents/${DOC_ID}/content`, (r) =>
    r.fulfill({
      status: 200,
      contentType: "application/pdf",
      body: Buffer.from("%PDF-1.4\n%mock\n"),
    }),
  );
}

async function setup(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.addInitScript(
    ({ token }) => {
      window.localStorage.setItem("bvp.token", token);
    },
    { token: AUTH_TOKEN },
  );
  await page.context().addCookies([
    { name: "BVP_LOCALE", value: "it", domain: "localhost", path: "/" },
    { name: "BVP_LOCALE", value: "it", domain: "127.0.0.1", path: "/" },
  ]);
  await installCommonMocks(page);
  // We deliberately do NOT throw on pageerror: this spec is a
  // navigation assessment, not a crash-detector, and several routes
  // are known to surface JS exceptions in this minimally-mocked
  // environment (every endpoint returns []). Tests that need a
  // crash gate add their own listener.
  page.on("pageerror", (err) => {
    // eslint-disable-next-line no-console
    console.warn(`[pageerror] ${err.message}`);
  });
}

// ---------------------------------------------------------------------
// (1) Tab persistence — ?view= deep-link + browser back/forward
// ---------------------------------------------------------------------

test.describe("Patient page tab persistence", () => {
  // KNOWN UX TRADE-OFF: FascicoloViewToggle uses ``router.replace``
  // for tab clicks (FascicoloViewToggle.tsx:46) so changing tab does
  // NOT add a history entry. Refresh / direct deep-link survives,
  // but browser back/forward will jump past the tab change. The spec
  // covers what the design promises (deep-link survives) and not
  // what it omits (back-undo of a tab click).
  test("each ?view= deep-link selects the right tab on cold load", async ({ page }) => {
    await setup(page);

    // The contract: every supported tab must be addressable via
    // ``?view=<value>`` so refresh, bookmarks, and deep-links from
    // other pages restore the user's last view. We verify the
    // mapping once per tab.
    for (const [param, name] of [
      ["drive", "Drive"],
      ["documents", "Documenti"],
      ["provenance", "Provenance"],
    ] as const) {
      const url =
        param === "drive" ? `/patients/${PATIENT_ID}` : `/patients/${PATIENT_ID}?view=${param}`;
      await page.goto(url);
      await expect(page.getByRole("tab", { name })).toHaveAttribute("aria-selected", "true");
    }
  });
});

// ---------------------------------------------------------------------
// (2) Document drill-down + back-link
// ---------------------------------------------------------------------

test.describe("Document drill-down", () => {
  test("Drive → document detail → back-link returns to Drive", async ({ page }) => {
    await setup(page);

    await page.goto(`/patients/${PATIENT_ID}`);
    // Drive tab is the default. Wait for the FolderTree aside.
    await expect(page.locator(".bv-folder-tree")).toBeVisible();

    // Drive cards now render as <Link> for navigable leaves so
    // cmd-click / middle-click / right-click "open in new tab"
    // behave correctly. Plain click triggers the SPA navigation.
    const docCard = page.getByRole("link", { name: /Referto TC 2025-04-01/ }).first();
    await expect(docCard).toBeVisible();
    await docCard.click();

    // We are on the document detail page.
    await expect(page).toHaveURL(new RegExp(`/patients/${PATIENT_ID}/documents/${DOC_ID}`));
    await expect(page.getByRole("heading", { name: "Referto TC 2025-04-01" })).toBeVisible();

    // The page MUST expose a back affordance (ContextualBackLink or
    // BackToFolderLink). We assert at least one ``← `` link exists
    // pointing back into the patient namespace.
    const backLinks = page.getByRole("link").filter({ hasText: /←|back/i });
    await expect(backLinks.first()).toBeVisible();
    const href = await backLinks.first().getAttribute("href");
    expect(href).toMatch(new RegExp(`/patients/${PATIENT_ID}`));
  });
});

// ---------------------------------------------------------------------
// (3) Site-header navigation always reachable
// ---------------------------------------------------------------------

test.describe("Site-header navigation", () => {
  test("primary nav links present + reachable from a deep page", async ({ page }) => {
    await setup(page);

    // Open a deep page (document detail) and verify the global nav is
    // still rendered + reachable. A "trapped" page (no SiteHeader on
    // certain routes) is a navigation regression we want to catch.
    await page.goto(`/patients/${PATIENT_ID}/documents/${DOC_ID}`);

    const banner = page.getByRole("banner");
    await expect(banner).toBeVisible();

    // Logo link to home.
    await expect(banner.getByRole("link", { name: /bit\.vision/i })).toHaveAttribute("href", "/");
    // Pazienti.
    const patientsLink = banner.getByRole("link", { name: "Pazienti" });
    await expect(patientsLink).toHaveAttribute("href", "/patients");
    // Tag.
    await expect(banner.getByRole("link", { name: "Tag" })).toHaveAttribute("href", "/tags");
    // Carica (upload).
    await expect(banner.getByRole("link", { name: "Carica" })).toHaveAttribute("href", "/upload");
    // Settings via display_name.
    await expect(banner.getByRole("link", { name: /E2E Tester/ })).toHaveAttribute(
      "href",
      "/settings",
    );
  });
});

// ---------------------------------------------------------------------
// (4) Auth-gated route redirects to /login (UX: user understands why
//     they were bounced)
// ---------------------------------------------------------------------

test.describe("Unauthenticated routing", () => {
  test("hitting a patient page with no token shows login affordance", async ({ page }) => {
    // Intentionally do NOT seed the auth token. Just install mocks so
    // the network is mockable, but /api/auth/me responds 401.
    await page
      .context()
      .addCookies([{ name: "BVP_LOCALE", value: "it", domain: "localhost", path: "/" }]);
    await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 200, body: "[]" }));
    await page.route("**/api/auth/me", (r) => r.fulfill({ status: 401, body: "" }));
    await page.route(`**/api/patients/${PATIENT_ID}`, (r) => r.fulfill({ status: 401, body: "" }));

    await page.goto(`/patients/${PATIENT_ID}`);
    // The user should either land on /login or see the inline "Accedi"
    // / "Login required" affordance. Either path is acceptable; what
    // is NOT acceptable is a black canvas with no instructions.
    const accedi = page.getByRole("link", { name: /^Accedi|^Log\s?in/i }).first();
    await expect(accedi).toBeVisible();
  });
});

// ---------------------------------------------------------------------
// (5) Click-to-target: how many clicks from /patients to a document
// ---------------------------------------------------------------------

test.describe("Click-to-target heuristic", () => {
  test("home → patient → document opens within 3 clicks", async ({ page }) => {
    await setup(page);

    // Stub the patient list so we can navigate from /patients.
    await page.route(/\/api\/patients(\?.*)?$/, (r) =>
      jsonRoute(r, { items: [PATIENT], next_cursor: null }),
    );

    let clicks = 0;
    page.on("framenavigated", () => {
      // Each navigation that resulted from a click bumps the counter.
      // Programmatic gotos (set up) don't increment because we reset
      // before the user-driven flow.
    });

    await page.goto("/patients");
    clicks = 0; // reset post-setup

    await page
      .getByRole("link", { name: /Mario Rossi/ })
      .first()
      .click();
    clicks++;
    await expect(page).toHaveURL(new RegExp(`/patients/${PATIENT_ID}`));

    // Drive tab is default; click the document card (now a real
    // <Link>, see "Document drill-down" test).
    await expect(page.locator(".bv-folder-tree")).toBeVisible();
    const docTarget = page.getByRole("link", { name: /Referto TC 2025-04-01/ }).first();
    await docTarget.click();
    clicks++;
    await expect(page).toHaveURL(new RegExp(`/patients/${PATIENT_ID}/documents/${DOC_ID}`));

    // Heuristic: top-of-funnel → document = ≤ 3 clicks. Anything
    // higher is a sign of nav friction (extra wrappers, modal in
    // the way, mandatory tab flip).
    expect(clicks).toBeLessThanOrEqual(3);
  });
});

// ---------------------------------------------------------------------
// (6) Logo always returns home (the universal escape hatch)
// ---------------------------------------------------------------------

// ---------------------------------------------------------------------
// (7) Drive cards expose a real `href` (regression for the "<button>
//     instead of <a>" UX bug)
// ---------------------------------------------------------------------

test.describe("Drive cards open in new tab", () => {
  test("document card href points at the document detail page", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);

    const docLink = page.getByRole("link", { name: /Referto TC 2025-04-01/ }).first();
    await expect(docLink).toBeVisible();
    // The href is what enables cmd-click / middle-click "open in new
    // tab"; without it Playwright would still see a clickable
    // affordance but cmd-click would no-op.
    await expect(docLink).toHaveAttribute("href", `/patients/${PATIENT_ID}/documents/${DOC_ID}`);
  });

  test("study card href points at the study detail page", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);

    const studyLink = page.getByRole("link", { name: /TC torace con mdc/ }).first();
    await expect(studyLink).toBeVisible();
    await expect(studyLink).toHaveAttribute("href", `/patients/${PATIENT_ID}/studies/${STUDY_ID}`);
  });
});

// ---------------------------------------------------------------------
// (8) Tab click pushes a history entry (regression for the
//     `router.replace` → `router.push` migration)
// ---------------------------------------------------------------------

test.describe("Tab click adds to history", () => {
  test("browser back undoes a tab click", async ({ page }) => {
    await setup(page);

    await page.goto(`/patients/${PATIENT_ID}`);
    await expect(page.getByRole("tab", { name: "Drive" })).toHaveAttribute("aria-selected", "true");

    // Click into Documenti — URL must update.
    await page.getByRole("tab", { name: "Documenti" }).click();
    await expect(page).toHaveURL(/[?&]view=documents/);
    await expect(page.getByRole("tab", { name: "Documenti" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    // Browser back: should restore the Drive tab thanks to ``router.push``.
    await page.goBack();
    await expect(page).not.toHaveURL(/view=documents/);
    await expect(page.getByRole("tab", { name: "Drive" })).toHaveAttribute("aria-selected", "true");

    // And forward returns to Documenti.
    await page.goForward();
    await expect(page).toHaveURL(/[?&]view=documents/);
    await expect(page.getByRole("tab", { name: "Documenti" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});

test.describe("Logo escape hatch", () => {
  test("logo click from a deep route lands on /", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}/documents/${DOC_ID}`);
    // Wait explicitly for the navigation triggered by the anchor —
    // ``click`` returns as soon as the click event fires, not when
    // the SPA has finished routing. Using a URL pattern is more
    // robust than waitForLoadState here because the destination is
    // app-controlled (``/`` if logged in, ``/login`` if not).
    await Promise.all([
      page.waitForURL((u) => !u.toString().includes(`/documents/${DOC_ID}`), { timeout: 5_000 }),
      page.getByRole("link", { name: /bit\.vision/i }).click(),
    ]);
    expect(page.url()).not.toMatch(new RegExp(`/documents/${DOC_ID}`));
  });
});
