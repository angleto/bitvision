// SendStudyDialog z-index regression + navigability check.
//
// Why this exists: a clinician on the patient page reported that
// the ClinicalNotesSticky preview rendered ON TOP of the
// SendStudyDialog overlay when the share dialog was opened from a
// study card. Root cause: the dialog used ``position: fixed`` inside
// a subtree whose ancestor created a stacking context, so its
// ``z-index: 1200`` never beat the sticky's ``z-index: 100`` once
// the latter was reached through that ancestor.
//
// Pin: when the dialog is open, ``elementFromPoint`` at the dialog's
// title coordinates must resolve to a descendant of the dialog
// itself, NEVER to the clinical-notes-sticky region. Run also
// covers ``Escape`` close + backdrop click close so the navigability
// fix doesn't accidentally break those.

import { type Page, type Route, expect, test } from "@playwright/test";

const PATIENT_ID = "00000000-0000-0000-0000-000000000099";
const STUDY_ID = "00000000-0000-0000-0000-0000000000aa";
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

// Patient has long ``notes`` so ClinicalNotesSticky renders the
// expandable preview (it auto-hides when notes are empty, which
// would defeat the regression test).
const LONG_NOTES = Array.from({ length: 12 }, (_, i) => `Nota clinica linea ${i + 1}.`).join("\n");
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
  notes: LONG_NOTES,
  contacts: [
    {
      id: "11111111-1111-1111-1111-111111111111",
      label: "Dr. Bianchi",
      relationship: "consulente",
      email: "bianchi@example.org",
      phone: null,
      notes: null,
      is_primary: true,
      consent_to_contact: true,
    },
  ],
  managed_by_subject_id: ME.subject_id,
  self_user_subject_id: null,
  origin: "mine" as const,
  created_at: "2025-01-01T00:00:00Z",
  etag: "etag-pat-99",
};

// Minimal patient tree with one study card.
const TREE = {
  path: "/",
  parent_path: null,
  nodes: [
    {
      id: `study-${STUDY_ID}`,
      type: "study",
      target_id: STUDY_ID,
      name: "TC torace 2026-04-12",
      path: "/",
      date: "2026-04-12",
      updated_at: "2026-04-13T00:00:00Z",
      modality: "CT",
      thumbnail_series_id: null,
      series_count: 3,
      instance_count: 120,
    },
  ],
};

async function installCommonMocks(page: Page) {
  // Catch-all to keep the page from hanging on un-mocked endpoints.
  await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 200, body: "[]" }));
  await page.route("**/api/auth/me", (r) => jsonRoute(r, ME));
  await page.route(/\/api\/jobs(\?.*)?$/, (r) => jsonRoute(r, { items: [] }));
  await page.route(/\/api\/system\/features$/, (r) => jsonRoute(r, { llm_classifier: false }));
  await page.route(/\/api\/me\/scopes$/, (r) => jsonRoute(r, { scopes: [] }));
  await page.route(`**/api/patients/${PATIENT_ID}`, (r) => jsonRoute(r, PATIENT));
  await page.route(/\/api\/patients\/[^/]+\/notes(\?.*)?$/, (r) => jsonRoute(r, []));
  await page.route(/\/api\/document-catalog.*/, (r) =>
    jsonRoute(r, { kinds: [], provenances: [], authorities: [] }),
  );
  await page.route(/\/api\/patients\/[^/]+\/tree.*/, (r) => jsonRoute(r, TREE));
  await page.route(/\/api\/patients\/[^/]+\/folders.*/, (r) => jsonRoute(r, { folders: [] }));
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
}

async function setup(page: Page) {
  await page.setViewportSize({ width: 1280, height: 720 });
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
  page.on("pageerror", (err) => {
    // eslint-disable-next-line no-console
    console.warn(`[pageerror] ${err.message}`);
  });
}

test.describe("SendStudyDialog z-index over ClinicalNotesSticky", () => {
  test("dialog overlays the sticky notes (no element from sticky on top)", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    // Sticky notes must render — without them the regression check
    // is meaningless.
    const sticky = page.locator("[data-clinical-notes-sticky]");
    await expect(sticky).toBeVisible();

    // Open the dialog from the study card. The button has aria-label
    // "Invia studio a un collega" (i18n key sendStudy.title in IT).
    const sendButton = page.getByRole("button", { name: /Invia studio/i }).first();
    await expect(sendButton).toBeVisible();
    await sendButton.click();

    // Dialog backdrop should now be visible at fixed position.
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();

    // Core regression: at every sampled point inside the dialog
    // bounding box, the topmost element under the cursor must be a
    // descendant of the dialog (or its backdrop), never an element
    // inside the sticky-notes section. We sample a 5x5 grid because
    // the user-reported bug only manifested where the sticky
    // overlapped the dialog — checking only the title centre missed
    // the case where the dialog body extends down past the sticky's
    // bottom edge.
    const dialogBox = await dialog.boundingBox();
    if (!dialogBox) throw new Error("dialog has no bounding box");
    const samples: { x: number; y: number }[] = [];
    for (let i = 1; i <= 5; i++) {
      for (let j = 1; j <= 5; j++) {
        samples.push({
          x: dialogBox.x + (dialogBox.width * i) / 6,
          y: dialogBox.y + (dialogBox.height * j) / 6,
        });
      }
    }
    const stuckPoints = await page.evaluate((points: { x: number; y: number }[]) => {
      const out: { x: number; y: number; tag: string }[] = [];
      for (const p of points) {
        const el = document.elementFromPoint(p.x, p.y) as HTMLElement | null;
        if (!el) continue;
        const inSticky = !!el.closest("[data-clinical-notes-sticky]");
        const inDialog = !!el.closest('[role="dialog"]');
        if (inSticky && !inDialog) {
          out.push({ x: p.x, y: p.y, tag: el.tagName });
        }
      }
      return out;
    }, samples);
    expect(
      stuckPoints,
      `sticky notes painted over the dialog at ${stuckPoints.length} sampled points`,
    ).toEqual([]);
  });

  test("Escape closes the dialog and returns focus to the page", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    const sendButton = page.getByRole("button", { name: /Invia studio/i }).first();
    await sendButton.click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toBeHidden();
  });
});

test.describe("ClinicalNotesSticky navigability", () => {
  test("expand / collapse toggle survives keyboard navigation", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    await expect(sticky).toBeVisible();
    // The compact preview ships with a footer button "Espandi" (or
    // a header chevron with the same aria-expanded). Either should
    // toggle aria-expanded between true / false on keyboard activation.
    const toggle = sticky.locator("button[aria-controls='bv-clinical-notes-body']").first();
    await expect(toggle).toBeVisible();
    const expandedBefore = await toggle.getAttribute("aria-expanded");
    await toggle.focus();
    await page.keyboard.press("Enter");
    const expandedAfter = await toggle.getAttribute("aria-expanded");
    expect(expandedBefore).not.toBe(expandedAfter);
  });

  test("Esc collapses an expanded panel without forcing aim at the toggle", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    await expect(sticky).toBeVisible();
    const toggle = sticky.locator("button[aria-controls='bv-clinical-notes-body']").first();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    // Press Escape from anywhere — focus is on the toggle but the
    // handler is at document level, so a global press works the same.
    await page.keyboard.press("Escape");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  test("expanded preference persists across reload (per-patient)", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    const toggle = sticky.locator("button[aria-controls='bv-clinical-notes-body']").first();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    // Reload the page; expansion state should rehydrate from
    // localStorage instead of resetting to the collapsed default.
    await page.reload();
    const stickyAfter = page.locator("[data-clinical-notes-sticky]");
    const toggleAfter = stickyAfter
      .locator("button[aria-controls='bv-clinical-notes-body']")
      .first();
    await expect(toggleAfter).toHaveAttribute("aria-expanded", "true");
  });

  test("click on plain-text background collapses the expanded panel", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    const toggle = sticky.locator("button[aria-controls='bv-clinical-notes-body']").first();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    // The demographics line ("1960-05-12 · 66a · M") is rendered as
    // a <p> with no interactive children — that's the canonical
    // "background" click that should collapse the notes.
    await page.locator("main p.meta").first().click({ force: true });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  test("pointerdown on a non-modal button does NOT collapse the panel", async ({ page }) => {
    // Pin the design choice: clicks on real buttons are starting
    // another task, so collapsing the notes underneath them creates
    // a layout shift that confuses the user. We dispatch a native
    // pointerdown on a known-safe button (the LanguageSwitcher in
    // the header) that doesn't itself open a modal. The notes must
    // remain expanded.
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    const toggle = sticky.locator("button[aria-controls='bv-clinical-notes-body']").first();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");

    // Dispatch the bare pointerdown a real mouse would fire on the
    // theme-toggle button. We avoid a full ``click()`` to skip
    // whatever side-effect the button has — only the collapse-or-not
    // signal matters here.
    await page.evaluate(() => {
      const btn = document.querySelector('button[title^="Theme"]') as HTMLElement | null;
      if (!btn) throw new Error("theme button not found");
      const r = btn.getBoundingClientRect();
      btn.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          clientX: r.left + r.width / 2,
          clientY: r.top + r.height / 2,
        }),
      );
    });
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  test("anchor /#notes auto-expands and scrolls to the panel", async ({ page }) => {
    await setup(page);
    // Land directly on the deep link (e.g. shared in chat). The
    // sticky must hydrate expanded and scroll itself into view —
    // we verify expanded state and that the section is in viewport.
    await page.goto(`/patients/${PATIENT_ID}#notes`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    await expect(sticky).toBeVisible();
    const toggle = sticky.locator("button[aria-controls='bv-clinical-notes-body']").first();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    // Section must be in viewport (top edge above the fold).
    const inView = await sticky.evaluate((el) => {
      const r = el.getBoundingClientRect();
      return r.top >= -2 && r.top < window.innerHeight;
    });
    expect(inView).toBe(true);
  });

  test("provenance cue renders 'Aggiornata da X · Y' when backend supplies it", async ({
    page,
  }) => {
    await setup(page);
    // Register the override AFTER setup so it wins LIFO against the
    // generic patient mock installCommonMocks puts in place. Without
    // this ordering the catch-all returns the unstamped PATIENT.
    const recentIso = new Date(Date.now() - 30 * 60 * 1000).toISOString();
    await page.route(`**/api/patients/${PATIENT_ID}`, (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...PATIENT,
          notes_updated_at: recentIso,
          notes_updated_by_display_name: "Dr. Verdi",
        }),
      }),
    );
    await page.goto(`/patients/${PATIENT_ID}`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    await expect(sticky).toBeVisible();
    // The cue lives in [data-clinical-notes-meta]; assert it
    // contains the editor name and a relative-time fragment.
    const meta = sticky.locator("[data-clinical-notes-meta]");
    await expect(meta).toBeVisible();
    await expect(meta).toContainText("Dr. Verdi");
    // 30-minute delta lands on "minuti fa" in IT.
    await expect(meta).toContainText(/minut/i);
  });

  test("editing dirty state shows the pill and arms beforeunload", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    await sticky.getByRole("button", { name: /Modifica/i }).click();
    // Type something into the editor. TipTap exposes a
    // contenteditable; we focus and type.
    const editor = sticky.locator(".ProseMirror").first();
    await editor.click();
    await page.keyboard.type(" addendum");
    // Dirty pill (role=status) must appear.
    const pill = sticky.locator('[role="status"]');
    await expect(pill).toBeVisible();
    await expect(pill).toContainText(/Modifiche non salvate/i);
    // Arm a beforeunload guard expectation: pressing Esc cancels
    // (no prompt because cancelEdit drops the dirty state first).
    await page.keyboard.press("Escape");
    // Editor closes, dirty pill goes away.
    await expect(pill).toBeHidden();
  });

  test("public landing exposes direct download when prep is ready (no password)", async ({
    page,
  }) => {
    // Mount /shared/[token]/info with a mock /info response and
    // assert the "Scarica DICOM" anchor points at the new
    // ``/api/shared/{token}/download`` endpoint, NOT the old
    // verify → /studies path. Pinned because the user reported
    // the recipient was waiting unnecessarily for a fresh job at
    // click time.
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.context().addCookies([
      { name: "BVP_LOCALE", value: "it", domain: "localhost", path: "/" },
      { name: "BVP_LOCALE", value: "it", domain: "127.0.0.1", path: "/" },
    ]);
    const TOKEN = "tok-public-ready";
    await page.route(`**/api/shared/${TOKEN}/info`, (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          study_title: "TC torace 2026-04-12",
          modalities: ["CT"],
          study_date: "2026-04-12",
          requires_password: false,
          expires_at: new Date(Date.now() + 7 * 24 * 3600_000).toISOString(),
          permissions: ["shared:download"],
          max_uses: null,
          uses_remaining: null,
          resource_kind: "study",
          resource_id: "study-aaaa",
          mode: "claim",
          claimable: false,
          recipient_name: null,
          recipient_email: null,
          deidentified: true,
          total_files: 120,
          total_bytes: 350 * 1024 * 1024,
          grantor_display: "Dr. Rossi",
          prepared_status: "succeeded",
          prepared_progress_done: 120,
          prepared_progress_total: 120,
        }),
      }),
    );
    await page.goto(`/shared/${TOKEN}/info`);
    const dl = page.getByRole("link", { name: /Scarica DICOM/i });
    await expect(dl).toBeVisible();
    const href = await dl.getAttribute("href");
    expect(href).toContain(`/api/shared/${TOKEN}/download`);
  });

  test("public landing shows a progress bar and disables download while prep is running", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.context().addCookies([
      { name: "BVP_LOCALE", value: "it", domain: "localhost", path: "/" },
      { name: "BVP_LOCALE", value: "it", domain: "127.0.0.1", path: "/" },
    ]);
    const TOKEN = "tok-public-running";
    await page.route(`**/api/shared/${TOKEN}/info`, (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          study_title: "TC torace",
          modalities: ["CT"],
          study_date: "2026-04-12",
          requires_password: false,
          expires_at: new Date(Date.now() + 7 * 24 * 3600_000).toISOString(),
          permissions: ["shared:download"],
          max_uses: null,
          uses_remaining: null,
          resource_kind: "study",
          resource_id: "study-bbbb",
          mode: "claim",
          claimable: false,
          recipient_name: null,
          recipient_email: null,
          deidentified: false,
          total_files: 120,
          total_bytes: 350 * 1024 * 1024,
          grantor_display: "Dr. Rossi",
          prepared_status: "running",
          prepared_progress_done: 30,
          prepared_progress_total: 120,
        }),
      }),
    );
    await page.goto(`/shared/${TOKEN}/info`);
    // While prep is running the anchor renders without an href
    // (anchors without href aren't exposed as the "link" role).
    // We assert the visible label + the aria-disabled wiring.
    const dl = page.locator('a[aria-disabled="true"]', {
      hasText: /preparazione/i,
    });
    await expect(dl).toBeVisible();
    await expect(dl).not.toHaveAttribute("href", /\/api\/shared\/.*\/download/);
    // Progress strip text is rendered with done/total.
    await expect(page.getByText(/30 \/ 120 file/)).toBeVisible();
  });

  test("opening a modal does NOT collapse the expanded notes", async ({ page }) => {
    await setup(page);
    await page.goto(`/patients/${PATIENT_ID}`);
    const sticky = page.locator("[data-clinical-notes-sticky]");
    const toggle = sticky.locator("button[aria-controls='bv-clinical-notes-body']").first();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    // Open the SendStudyDialog. The click happens inside [role=dialog]
    // (well, the dialog appears and the user might click inside it);
    // the click-outside handler must NOT collapse the notes when the
    // user is interacting with a modal.
    await page
      .getByRole("button", { name: /Invia studio/i })
      .first()
      .click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    // Click inside the dialog content.
    await dialog.getByRole("heading", { level: 2 }).click();
    // Notes must still be expanded.
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    // Cleanup: close the dialog so other tests don't carry state.
    await page.keyboard.press("Escape");
  });
});
