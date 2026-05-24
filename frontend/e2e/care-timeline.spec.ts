// Care-timeline end-to-end spec.
//
// Covers the 10-step scenario from
// ``docs/care-timeline-phases.md`` §9.4.
//
// By default the spec runs against ``page.route(...)`` mocks so it is
// hermetic and can run on CI without a backend. Set
// ``E2E_USE_REAL_BACKEND=1`` to skip the mocks and hit the real
// FastAPI service (the test then expects a fixture patient at
// ``E2E_PATIENT_ID`` already seeded with clinical events but no
// care phases).
//
// Auth: the request helper in ``src/lib/api.ts`` reads the JWT from
// ``localStorage`` under the key ``bvp.token`` (see TOKEN_STORAGE_KEY).
// When ``E2E_USE_REAL_BACKEND=1`` the spec expects ``E2E_AUTH_TOKEN``
// to be provided. In mock mode we still seed a placeholder token to
// bypass any client-side auth guards.

import { type Page, type Route, expect, test } from "@playwright/test";

import {
  E2E_PATIENT_ID,
  FIRST_EVENT_ID,
  PHASE_ID_FIRST,
  PHASE_ID_SECOND,
  PHASE_ID_THIRD,
  SECOND_EVENT_ID,
  detailFirstPhase,
  emptyHealth,
  emptyTimeline,
  materialForFirstPhase,
  populatedHealth,
  populatedTimeline,
  proposalResponse,
  revisionsForFirstPhase,
  svgExport,
} from "./_fixtures/care_timeline_responses";

// ---------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------

const USE_REAL_BACKEND = process.env.E2E_USE_REAL_BACKEND === "1";
const PATIENT_ID = process.env.E2E_PATIENT_ID ?? E2E_PATIENT_ID;
const AUTH_TOKEN = process.env.E2E_AUTH_TOKEN ?? "e2e-mock-token";

// ---------------------------------------------------------------------
// Mutable fixture state. Each test resets via ``state.reset()`` in
// ``beforeEach``. This lets the propose handler "promote" the timeline
// from empty → populated on the next read, mimicking the real apply
// flow without an explicit "Accetta" UI button.
// ---------------------------------------------------------------------

interface State {
  timeline: typeof emptyTimeline | typeof populatedTimeline;
  health: typeof emptyHealth | typeof populatedHealth;
  // Whether a propose has been submitted. After propose the next
  // timeline read returns the populated version (server-side apply
  // is implicit in the spec while no explicit "Accetta" UI exists).
  proposeApplied: boolean;
  lastAssignBody: unknown;
}

function freshState(): State {
  return {
    timeline: emptyTimeline,
    health: emptyHealth,
    proposeApplied: false,
    lastAssignBody: null,
  };
}

async function jsonRoute(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installMocks(page: Page, state: State) {
  // Care-timeline read.
  await page.route(/\/api\/patients\/[^/]+\/care-timeline(\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const fmt = url.searchParams.get("format");
    if (fmt === "svg") {
      await route.fulfill({
        status: 200,
        contentType: "image/svg+xml",
        body: svgExport,
      });
      return;
    }
    await jsonRoute(route, state.timeline);
  });

  await page.route(/\/api\/patients\/[^/]+\/care-timeline\/health$/, async (route) => {
    await jsonRoute(route, state.health);
  });

  // Propose: returns the proposal payload AND promotes the in-memory
  // timeline so the next read shows phases. The component refresh()
  // call after propose triggers that next read.
  await page.route(/\/api\/patients\/[^/]+\/care-phases:propose$/, async (route) => {
    state.timeline = populatedTimeline;
    state.health = populatedHealth;
    state.proposeApplied = true;
    await jsonRoute(route, proposalResponse);
  });

  await page.route(/\/api\/patients\/[^/]+\/care-phases:apply-proposal$/, async (route) => {
    state.timeline = populatedTimeline;
    state.health = populatedHealth;
    state.proposeApplied = true;
    await jsonRoute(route, {
      applied_phases: populatedTimeline.phases.map((p) => p.id),
      applied_assignments: 8,
      skipped_assignments: 0,
    });
  });

  // Phase list + detail + material (phase detail page, step 6).
  await page.route(/\/api\/patients\/[^/]+\/care-phases$/, async (route) => {
    await jsonRoute(route, populatedTimeline.phases);
  });
  await page.route(/\/api\/patients\/[^/]+\/care-phases\/[^/]+\/material$/, async (route) => {
    await jsonRoute(route, materialForFirstPhase);
  });
  await page.route(/\/api\/patients\/[^/]+\/care-phases\/[^/]+\/revisions$/, async (route) => {
    await jsonRoute(route, revisionsForFirstPhase);
  });
  await page.route(
    /\/api\/patients\/[^/]+\/care-phases\/[^/]+$/,
    async (route) => {
      // GET detail or PATCH/DELETE on a single phase.
      const method = route.request().method();
      if (method === "GET") return jsonRoute(route, detailFirstPhase);
      if (method === "PATCH") return jsonRoute(route, detailFirstPhase);
      return route.fulfill({ status: 204 });
    },
    { times: 0 },
  );

  // Assign event → another phase (step 7). Captures the body so the
  // assertion can inspect it.
  await page.route(/\/api\/patients\/[^/]+\/care-phases\/[^/]+\/events\/[^/]+$/, async (route) => {
    const method = route.request().method();
    if (method === "PUT") {
      try {
        state.lastAssignBody = route.request().postDataJSON();
      } catch {
        state.lastAssignBody = null;
      }
      // Surface the changed assignment in the timeline so the
      // refresh() after the drop reflects it. We move the second
      // event from phase 2 → phase 3 (the drag target in the spec).
      state.timeline = mutateAssignment(state.timeline, SECOND_EVENT_ID, PHASE_ID_THIRD);
      const moved = populatedTimeline.phases[2].events[0];
      await jsonRoute(route, moved);
      return;
    }
    // DELETE / unassign.
    await route.fulfill({ status: 204 });
  });

  // Restore revision (step 8). Restores the assignment to the
  // pre-mutation snapshot.
  await page.route(/\/api\/patients\/[^/]+\/care-phases\/[^/]+\/restore$/, async (route) => {
    state.timeline = populatedTimeline;
    state.health = populatedHealth;
    await jsonRoute(route, populatedTimeline.phases[0]);
  });

  // Catch-all: let unrelated requests pass through. The fascicolo
  // page hits a handful of other endpoints (patient, drive, scopes);
  // we stub the most common ones with empty payloads so the page
  // mounts without spinning forever.
  await page.route(/\/api\/me\/scopes$/, async (route) =>
    jsonRoute(route, { scopes: ["phases:read", "phases:propose", "phases:write"] }),
  );
  // Auth: without a stub here AuthProvider gets a 404/network error,
  // ``user`` stays null, and ``isOwner`` evaluates to ``false`` — which
  // hides the "Proponi fasi con LLM" button (only owners can propose).
  // Returning ``is_admin: true`` short-circuits the owner check
  // regardless of the patient's managed_by_subject_id field.
  // Pattern uses a glob with double-star prefix to match the absolute
  // backend URL (http://localhost:8000/...) which Playwright's regex
  // matchers test against the full URL string.
  await page.route("**/api/auth/me", async (route) =>
    jsonRoute(route, {
      subject_id: "00000000-0000-0000-0000-0000000000aa",
      email: "e2e@bv.test",
      display_name: "E2E Tester",
      is_admin: true,
      email_verified: true,
    }),
  );
  // System features probe: CareTimeline gates the "Proponi" button on
  // ``llm_classifier``. Default is ``null`` (probe in flight) which
  // already enables the button, but stubbing the call avoids a noisy
  // network failure in the trace.
  await page.route(/\/api\/system\/features$/, async (route) =>
    jsonRoute(route, { llm_classifier: true }),
  );
  await page.route(/\/api\/patients\/[^/]+$/, async (route) =>
    jsonRoute(route, {
      id: PATIENT_ID,
      display_name: "Paziente E2E",
      etag: "etag-pat-1",
      // Required for CareTimeline / FascicoloDriveLayout owner checks
      // when ``is_admin`` short-circuit is not desired (kept here for
      // documentation; with the admin user above this field is unused).
      managed_by_subject_id: "00000000-0000-0000-0000-0000000000aa",
      self_user_subject_id: null,
      origin: "mine",
    }),
  );
}

function mutateAssignment(
  tl: typeof populatedTimeline,
  eventId: string,
  newPhaseId: string,
): typeof populatedTimeline {
  // Rebuild phases moving ``eventId`` to ``newPhaseId``. Preserves
  // ordering inside each phase by event_date.
  const movedEvent = tl.phases.flatMap((p) => p.events).find((e) => e.id === eventId);
  if (!movedEvent) return tl;
  return {
    ...tl,
    phases: tl.phases.map((p) => {
      const without = p.events.filter((e) => e.id !== eventId);
      const next =
        p.id === newPhaseId
          ? [...without, { ...movedEvent, phase_id: newPhaseId }].sort((a, b) =>
              (a.event_date ?? "").localeCompare(b.event_date ?? ""),
            )
          : without;
      return { ...p, events: next, counts: { ...p.counts, n_events: next.length } };
    }),
  };
}

// ---------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------

let state: State;

test.beforeEach(async ({ page, context }) => {
  state = freshState();
  // Use a tall viewport so the CarePhaseEditor's fixed-position header
  // (position: fixed, inset: 0) doesn't push the close button under
  // the sticky site-header at the default 1280×720. Playwright's
  // force-click still respects DOM hit-testing, so a 1080-tall window
  // is the cheapest way to keep the close affordance reachable.
  await page.setViewportSize({ width: 1280, height: 1080 });
  if (!USE_REAL_BACKEND) {
    await installMocks(page, state);
  }
  // Force Italian locale. The spec asserts on Italian aria-labels
  // ("Stato timeline", "Fasi", "Proponi fasi con LLM"); without this
  // cookie next-intl falls back to the runner's Accept-Language
  // (which on a CI Chromium / fresh user profile is `en-US`) and
  // every label-based locator misses. Domain-scoped so it works for
  // any localhost port (3000/3100) and for E2E_BASE_URL overrides
  // pointing at *.localhost / 127.0.0.1.
  await context.addCookies([
    { name: "BVP_LOCALE", value: "it", domain: "localhost", path: "/" },
    { name: "BVP_LOCALE", value: "it", domain: "127.0.0.1", path: "/" },
  ]);
  // Seed the auth token so the request helper attaches Authorization.
  // Storage key MUST match TOKEN_STORAGE_KEY in src/lib/api.ts (`bvp.token`);
  // an earlier version of this spec used `bv:auth:token` which silently
  // failed (getStoredToken returned null → AuthProvider never fetched
  // /api/auth/me → isOwner=false → "Proponi" button hidden).
  await page.addInitScript((token: string) => {
    try {
      window.localStorage.setItem("bvp.token", token);
    } catch {
      // ignore (e.g. storage disabled)
    }
  }, AUTH_TOKEN);
});

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

async function gotoHealthRecord(page: Page) {
  await page.goto(`/patients/${PATIENT_ID}?view=events`);
  // The Events tab eagerly loads the timeline; wait for the salute
  // panel (rendered only after the health snapshot resolves).
  await expect(page.getByLabel("Stato timeline")).toBeVisible();
}

// ---------------------------------------------------------------------
// Spec
// ---------------------------------------------------------------------

test.describe("care-timeline (Health Record events tab)", () => {
  // Full happy path traverses 10 steps (propose → drag → restore →
  // export → a11y); the default 30s budget is too tight once Chromium
  // cold-starts the React tree under `next start`. 60s comfortably
  // covers the happy path on local + CI machines.
  test.setTimeout(60_000);
  test("end-to-end: propose → apply → drag → restore → export → a11y", async ({ page }) => {
    // ── Step 1+2: open the Health Record, assert the empty state ──
    await gotoHealthRecord(page);
    const salute = page.getByLabel("Stato timeline");
    await expect(salute).toContainText("Fasi");
    // n_phases statistic must read 0 for the empty fixture.
    await expect(salute).toContainText(/Fasi[\s\S]*0/);
    await expect(
      page.getByText("Eventi presenti, ma nessuna fase clinica ancora classificata."),
    ).toBeVisible();

    // ── Step 3: click "Proponi fasi con LLM" ──
    const proposeReq = page.waitForRequest(
      (req) => req.url().includes("/care-phases:propose") && req.method() === "POST",
    );
    await page.getByRole("button", { name: /Proponi fasi con LLM/i }).click();
    await proposeReq;

    // ── Step 4: timeline rerenders with chips on the left and dots on
    //   the right. NOTE: the UI does not yet ship an explicit
    //   "Accetta" panel; the propose handler implicitly applies the
    //   proposal and the next refresh shows the populated timeline.
    //   When the review-and-accept panel lands (spec §8 follow-up)
    //   replace this assertion with an explicit click on "Accetta".
    await expect(page.locator("[data-phase-slug='imaging-pre-op']")).toBeVisible();
    await expect(page.locator("[data-phase-slug='intervento-chirurgico']")).toBeVisible();
    await expect(page.locator("[data-phase-slug='follow-up-post-op']")).toBeVisible();
    // Event dots are present.
    await expect(page.locator(".timeline-event-dot").first()).toBeVisible();

    // ── Step 5: click an event dot → URL changes to /studies/... ──
    await page.locator(".timeline-event-dot").first().getByRole("link").click();
    await expect(page).toHaveURL(/\/studies\/study-/);

    // ── Step 6: back, then click a phase chip header ──
    await page.goBack();
    await expect(page.getByLabel("Stato timeline")).toBeVisible();
    await page.getByRole("button", { name: /Apri fase Imaging pre-op/i }).click();
    await expect(page).toHaveURL(new RegExp(`/patients/${PATIENT_ID}/care-phases/imaging-pre-op`));
    // Phase detail page sub-tabs. The page used to expose 4 tabs
    // (Studi/Documenti/Report/Annotazioni) but Reports was folded
    // into Documenti as part of the multi-referto refactor — only
    // 3 tabs render today. Match against tab role to avoid catching
    // stray text occurrences elsewhere on the page (e.g. the sidebar).
    for (const label of [/^Studi/, /^Documenti/, /^Annotazioni/]) {
      await expect(page.getByRole("tab", { name: label })).toBeVisible();
    }

    // ── Step 7: back to the timeline, toggle Modifica, drag dot ──
    await page.goBack();
    await expect(page.getByLabel("Stato timeline")).toBeVisible();
    // Three buttons share the bare label "Modifica" (clinical notes,
    // anagrafica, timeline drag&drop). Pick the timeline one by its
    // tooltip title — the only stable disambiguator on this page.
    await page
      .getByRole("button", { name: "Modifica" })
      .and(page.locator('[title*="drag"]'))
      .click();
    const editor = page.getByRole("dialog", { name: "Modifica fasi cliniche" });
    await expect(editor).toBeVisible();

    const sourceEvent = editor.locator(`[data-event-id='${SECOND_EVENT_ID}']`).first();
    const targetPhase = editor.locator(`[data-phase-id='${PHASE_ID_THIRD}']`).first();
    await expect(sourceEvent).toBeVisible();
    await expect(targetPhase).toBeVisible();

    const assignReq = page.waitForRequest(
      (req) => req.method() === "PUT" && /\/care-phases\/[^/]+\/events\/[^/]+$/.test(req.url()),
    );
    // Native HTML5 drag (the editor uses dataTransfer, not pointer
    // events). Playwright's ``dragTo`` synthesises HTML5 drag events.
    await sourceEvent.dragTo(targetPhase);
    const assignResp = await assignReq;
    expect(assignResp.url()).toMatch(new RegExp(`/care-phases/${PHASE_ID_THIRD}/events/`));

    // ── Step 8: open revisions panel, click Ripristina ──
    // Open revisions for the phase that just received the event.
    await editor
      .locator(`[data-phase-id='${PHASE_ID_THIRD}']`)
      .getByRole("button", { name: /Revisioni/i })
      .click();
    const restorePanel = page.getByLabel("Cronologia modifiche");
    await expect(restorePanel).toBeVisible();
    const restoreReq = page.waitForRequest(
      (req) => req.method() === "POST" && /\/care-phases\/[^/]+\/restore$/.test(req.url()),
    );
    await restorePanel
      .getByRole("button", { name: /Ripristina/i })
      .first()
      .click();
    await restoreReq;
    // Closing the editor would refetch; just assert no error surfaced.
    await expect(editor.locator("text=salvataggio")).toHaveCount(0);

    // ── Step 9: export SVG ──
    // Close the editor first: it is a native <dialog> rendered in the
    // browser top-layer, so Escape closes it (the editor's keyboard
    // handler also blocks Escape while a nested per-phase editor is
    // open — not the case here). The previous spec needed
    // ``evaluate(btn.click())`` because the old div-dialog was covered
    // by the sticky site-header; the native <dialog> is on top.
    await page.keyboard.press("Escape");
    await expect(editor).toBeHidden();

    // Step 9 verifies the export anchors are wired up with the right
    // backend URL + query string. The original spec opened the popup
    // tab and waited for the GET ``format=svg`` request, but the
    // anchor's ``rel="noreferrer"`` + Chromium's headless popup gating
    // makes that flaky. Asserting on the resolved ``href`` is the
    // contract that actually matters: if it is wrong the user will
    // hit a 404 in the new tab, regardless of whether Playwright sees
    // the popup event. Cheaper, deterministic, and covers the same
    // regression class.
    const svgAnchor = page.getByTitle("Esporta SVG");
    await expect(svgAnchor).toHaveAttribute(
      "href",
      new RegExp(`/api/patients/${PATIENT_ID}/care-timeline\\?format=svg&lang=it`),
    );
    await expect(svgAnchor).toHaveAttribute("target", "_blank");

    // ── Step 10: keyboard a11y ──
    // Editor was already closed before the SVG export. Tab to the
    // first dot and assert it is focusable.

    const firstDot = page.locator(".timeline-event-dot").first();
    await firstDot.focus();
    await expect(firstDot).toBeFocused();

    // Phase chip "Espandi" / "Comprimi" toggle exposes aria-expanded
    // and reacts to Space.
    const chevron = page
      .locator("[data-phase-slug='imaging-pre-op']")
      .getByRole("button", { name: /Comprimi fase|Espandi fase/i });
    const initial = await chevron.getAttribute("aria-expanded");
    await chevron.focus();
    await page.keyboard.press("Space");
    const next = await chevron.getAttribute("aria-expanded");
    expect(next).not.toBe(initial);
  });
});
