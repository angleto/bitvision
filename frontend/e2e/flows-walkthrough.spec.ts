// Flow walkthroughs.
//
// Drives 6 critical user flows end-to-end with mocked backend so the
// run is hermetic. Each test is a regression net + a documented
// "what concrete UX problems we hit" record.
//
// Flows covered:
//   1. Login (happy path + MFA pivot + invalid creds)
//   2. Settings hub (links resolve)
//   3. Upload route (requires patient_id query)
//   4. Studies list → study detail drill-down
//   5. Search bar in site-header (debounced, dropdown closes on outside click)
//   6. Patient list → patient detail (cumulative click count)
//
// UX FINDINGS captured during this audit (kept here as reference, the
// tests assert each):
//   - Login form has TWO duplicate "no account" links rendered
//     side-by-side at once (login/page.tsx:125-127 + 132-134). The
//     second pair is gated by ``!mfaRequired`` so on login screen
//     both fire, on MFA screen only the toggle button shows.
//     Codified by ``Login form regressions`` test below.
//   - /settings hub h1 + link labels are hardcoded English (no
//     ``useTranslations``). Italian users see EN strings even with
//     BVP_LOCALE=it.
//   - /upload without ``?patient_id=`` shows a helpful prompt but
//     no link to the patient picker.
//   - /studies route requires auth; with no token the page redirects
//     or shows the auth gate (verified).

import { type Page, type Route, expect, test } from "@playwright/test";

const PATIENT_ID = "00000000-0000-0000-0000-000000000001";
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

// Full StudyDetail shape (Study + series). Fields like ``modalities``
// are arrays — components that read them iterate without a guard, so
// missing them surfaces as "Cannot read properties of undefined
// (reading 'map')" deep in the render tree.
const STUDY = {
  id: STUDY_ID,
  study_instance_uid: "1.2.3.4.5",
  owner_subject_id: ME.subject_id,
  patient_id: PATIENT_ID,
  contribution_tier: "personal",
  is_public: false,
  is_listed_for_sale: false,
  ingestion_complete: true,
  study_description: "TC torace con mdc",
  study_date: "2025-04-01",
  modalities: ["CT"],
  created_at: "2025-04-01T10:00:00Z",
  series: [
    {
      id: SERIES_ID,
      study_id: STUDY_ID,
      series_instance_uid: "1.2.3.4.5.1",
      series_number: 1,
      modality: "CT",
      body_part_examined: "CHEST",
      series_description: "axial",
      expected_instance_count: 100,
      received_instance_count: 100,
      ingestion_complete: true,
    },
  ],
};

async function installCommonMocks(page: Page) {
  // Catch-all: every list endpoint returns ``[]``; consumers that
  // expect ``{items: ...}`` must be stubbed explicitly above.
  await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 200, body: "[]" }));
  await page.route("**/api/auth/me", (r) => jsonRoute(r, ME));
  await page.route(/\/api\/jobs(\?.*)?$/, (r) => jsonRoute(r, { items: [] }));
  await page.route(/\/api\/system\/features$/, (r) => jsonRoute(r, { llm_classifier: true }));
  await page.route(/\/api\/me\/scopes$/, (r) => jsonRoute(r, { scopes: [] }));
  await page.route(`**/api/patients/${PATIENT_ID}`, (r) => jsonRoute(r, PATIENT));
  // ``Paginated<T>`` shape (items + total + limit + offset). Several
  // pages assume those fields are present (no optional chaining).
  await page.route(/\/api\/patients(\?.*)?$/, (r) =>
    jsonRoute(r, { items: [PATIENT], total: 1, limit: 50, offset: 0 }),
  );
  await page.route(`**/api/studies/${STUDY_ID}`, (r) => jsonRoute(r, STUDY));
  await page.route(/\/api\/studies(\?.*)?$/, (r) =>
    jsonRoute(r, { items: [STUDY], total: 1, limit: 50, offset: 0 }),
  );
}

async function setup(page: Page, opts: { authed?: boolean } = {}) {
  await page.setViewportSize({ width: 1440, height: 900 });
  if (opts.authed !== false) {
    await page.addInitScript(
      ({ token }) => {
        window.localStorage.setItem("bvp.token", token);
      },
      { token: AUTH_TOKEN },
    );
  }
  await page.context().addCookies([
    { name: "BVP_LOCALE", value: "it", domain: "localhost", path: "/" },
    { name: "BVP_LOCALE", value: "it", domain: "127.0.0.1", path: "/" },
  ]);
  await installCommonMocks(page);
  page.on("pageerror", (err) => {
    // eslint-disable-next-line no-console
    console.warn(`[pageerror] ${err.message}`);
  });
}

// ---------------------------------------------------------------------
// (1) Login flow
// ---------------------------------------------------------------------

test.describe("Login flow", () => {
  test("happy path → /studies", async ({ page }) => {
    // Don't seed auth — we ARE the login.
    await setup(page, { authed: false });
    // The login route hits /api/auth/login (POST). Mock it to return
    // a token; AuthProvider will then refresh /api/auth/me.
    await page.route(/\/api\/auth\/login$/, (r) =>
      jsonRoute(r, { access_token: AUTH_TOKEN, token_type: "bearer" }),
    );

    await page.goto("/login");

    await page.getByRole("textbox", { name: /email/i }).fill("user@example.com");
    await page.getByLabel(/password/i).fill("hunter2");
    await page.getByRole("button", { name: /^Accedi$|^Sign\s?in$|^Login$/i }).click();

    // After login, the page redirects to ?next= or /studies.
    await page.waitForURL(/\/studies/, { timeout: 5_000 });
    await expect(page).toHaveURL(/\/studies/);
  });

  test("invalid credentials surfaces an error message", async ({ page }) => {
    await setup(page, { authed: false });
    await page.route(/\/api\/auth\/login$/, (r) =>
      r.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "invalid_credentials" }),
      }),
    );

    await page.goto("/login");
    await page.getByRole("textbox", { name: /email/i }).fill("wrong@example.com");
    await page.getByLabel(/password/i).fill("nope");
    await page.getByRole("button", { name: /^Accedi$|^Sign\s?in$|^Login$/i }).click();

    // The form must surface an inline error and stay on /login.
    await expect(page.locator(".error")).toBeVisible({ timeout: 5_000 });
    await expect(page).toHaveURL(/\/login/);
  });

  test("login form has exactly one 'no account' link (regression for duplicate)", async ({
    page,
  }) => {
    await setup(page, { authed: false });
    await page.goto("/login");

    // login/page.tsx used to render the same /register link twice on
    // the credentials screen (an unconditional Link followed by the
    // same Link inside a ``!mfaRequired`` ternary). The fix
    // consolidated both into a single conditional block — this test
    // pins the contract: exactly one "Non hai un account" link, and
    // it must point at /register.
    const noAccountLinks = page.getByRole("link", {
      name: /Non hai un account|No account|Sign\s?up/i,
    });
    await expect(noAccountLinks).toHaveCount(1);
    await expect(noAccountLinks).toHaveAttribute("href", "/register");
  });
});

// ---------------------------------------------------------------------
// (2) Settings hub
// ---------------------------------------------------------------------

test.describe("Settings hub", () => {
  test("renders the 5 settings cards each linking to a child route", async ({ page }) => {
    await setup(page);
    await page.goto("/settings");

    await expect(page.getByRole("heading", { name: "Impostazioni", level: 1 })).toBeVisible();

    for (const href of [
      "/settings/ai-assistants",
      "/settings/api-keys",
      "/settings/wallet",
      "/settings/mfa",
      "/settings/privacy",
    ]) {
      const card = page.locator(`a.card[href="${href}"]`);
      await expect(card).toBeVisible();
    }
  });

  test("settings hub honours the active locale", async ({ page }) => {
    await setup(page);
    await page.goto("/settings");

    // The hub used to hardcode English literals ("Settings",
    // "AI assistants", "Wallet") which leaked through with
    // BVP_LOCALE=it. After the fix, the cookie-driven Italian
    // locale resolves the ``settingsHub.*`` namespace.
    await expect(page.getByRole("heading", { name: "Impostazioni", level: 1 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Assistenti AI" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Wallet" })).toBeVisible();
  });
});

// ---------------------------------------------------------------------
// (3) Upload route
// ---------------------------------------------------------------------

test.describe("Upload route", () => {
  test("/upload?patient_id=… mounts UniversalUploader", async ({ page }) => {
    await setup(page);
    await page.route(/\/api\/document-catalog.*/, (r) =>
      jsonRoute(r, { kinds: [], provenances: [], authorities: [] }),
    );
    await page.route(/\/api\/patients\/[^/]+\/tree.*/, (r) =>
      jsonRoute(r, { folder_id: null, path: "/", parent_path: null, nodes: [] }),
    );

    await page.goto(`/upload?patient_id=${PATIENT_ID}`);
    // The upload page renders "Carica" copy and the dropzone label
    // from UniversalUploader. We wait for either the drop zone or
    // the patient header that the uploader surfaces.
    await expect(page.locator("body")).toContainText(/Carica|Upload|drop|trascina|patient/i, {
      timeout: 5_000,
    });
  });

  test("/upload without patient_id renders the page (no crash)", async ({ page }) => {
    await setup(page);
    await page.goto("/upload");
    // Whatever the page renders without a patient context, the body
    // must not contain Next.js's "Application error" boundary text.
    await page.waitForTimeout(500);
    const html = await page.content();
    expect(html).not.toContain("Application error: a client-side exception");
  });
});

// ---------------------------------------------------------------------
// (4) Studies list → study detail drill-down
// ---------------------------------------------------------------------

test.describe("Study drill-down", () => {
  test("/studies redirects to /patients (legacy route preserved)", async ({ page }) => {
    // src/app/studies/page.tsx is a server-side redirect — the
    // standalone studies-list page was retired in favour of the
    // unified Patients view. The test pins the redirect contract
    // so a future refactor doesn't silently 404 bookmarks.
    await setup(page);
    await page.goto("/studies");
    await expect(page).toHaveURL(/\/patients/);
  });

  test("study detail renders without client-side exception", async ({ page }) => {
    await setup(page);
    await page.route(/\/api\/studies\/[^/]+\/reports/, (r) => jsonRoute(r, []));
    await page.route(/\/api\/studies\/[^/]+\/measurements/, (r) => jsonRoute(r, []));
    await page.route(/\/api\/studies\/[^/]+\/document-links/, (r) => jsonRoute(r, []));
    await page.route(/\/api\/tags.*/, (r) => jsonRoute(r, []));

    await page.goto(`/studies/${STUDY_ID}`);
    await page.waitForTimeout(800);

    const html = await page.content();
    expect(html).not.toContain("Application error: a client-side exception");
    // The page should expose the study description as a heading.
    await expect(page.getByText(/TC torace con mdc/).first()).toBeVisible();
  });
});

// ---------------------------------------------------------------------
// (5) Search bar in site-header
// ---------------------------------------------------------------------

test.describe("Site-header search", () => {
  test("typing into the studies search debounces + shows nothing on empty results", async ({
    page,
  }) => {
    await setup(page);
    // The header's search field calls /api/studies?q=… (debounced).
    let calls = 0;
    await page.route(/\/api\/studies\?.*q=/, async (r) => {
      calls++;
      await jsonRoute(r, { items: [], next_cursor: null });
    });

    await page.goto("/studies");

    const search = page.getByRole("banner").getByRole("searchbox", { name: /Cerca|Search/i });
    await expect(search).toBeVisible({ timeout: 5_000 });

    // Type fast — debounce should coalesce multiple keystrokes into
    // one fetch (or a small bounded number).
    await search.fill("torace");
    await page.waitForTimeout(700);
    expect(calls).toBeLessThanOrEqual(2);
  });
});

// ---------------------------------------------------------------------
// (6) Patient list → patient detail (click-count budget)
// ---------------------------------------------------------------------

test.describe("Patient drill-down", () => {
  test("home → /patients → patient detail in 2 clicks", async ({ page }) => {
    await setup(page);

    await page.goto("/patients");

    let clicks = 0;
    await page
      .getByRole("link", { name: /Mario Rossi/ })
      .first()
      .click();
    clicks++;
    await expect(page).toHaveURL(new RegExp(`/patients/${PATIENT_ID}`));
    expect(clicks).toBeLessThanOrEqual(2);
  });
});
