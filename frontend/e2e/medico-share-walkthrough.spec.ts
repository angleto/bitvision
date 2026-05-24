// Walkthrough: simula un medico che vuole "mandare i DICOM al collega".
//
// Scopo NON è verificare assertion strette ma produrre evidenze visive
// dei pain UX correnti, screenshot per ogni step in
// ``/tmp/playwright-pre-fix/``. Quando questa suite gira di nuovo dopo
// le modifiche P0 (target dir ``/tmp/playwright-post-p0/``) il diff
// dimostra il guadagno di click count e di discoverability.
//
// Modello mentale del soggetto: medico non programmatore, parlante
// italiano, abituato a WeTransfer per "drag, ottieni link, manda".
// Ogni step misura quanti click ha fatto e cosa vede.

import fs from "node:fs";
import path from "node:path";

import { type Page, type Route, expect, test } from "@playwright/test";

const PATIENT_ID = "00000000-0000-0000-0000-000000000099";
const STUDY_ID = "00000000-0000-0000-0000-0000000000aa";
const SERIES_ID = "00000000-0000-0000-0000-0000000000bb";
const AUTH_TOKEN = "e2e-mock-token";

const SCREEN_DIR = process.env.WALKTHROUGH_DIR ?? "/tmp/playwright-pre-fix";

async function jsonRoute(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const ME = {
  subject_id: "00000000-0000-0000-0000-0000000000aa",
  email: "medico@bv.test",
  display_name: "Dr. Angelo",
  is_admin: true,
  email_verified: true,
};

const PATIENT = {
  id: PATIENT_ID,
  display_name: "Mario Rossi",
  external_id: null,
  birth_date: "1960-05-12",
  sex: "M",
  tax_id: "RSSMRA60E12H501Z",
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
  created_at: "2026-01-01T00:00:00Z",
  etag: "etag-pat-99",
};

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
  study_date: "2026-04-12",
  modalities: ["CT"],
  created_at: "2026-04-12T10:00:00Z",
  series: [
    {
      id: SERIES_ID,
      study_id: STUDY_ID,
      series_instance_uid: "1.2.3.4.5.1",
      series_number: 1,
      modality: "CT",
      body_part_examined: "CHEST",
      series_description: "axial",
      expected_instance_count: 120,
      received_instance_count: 120,
      ingestion_complete: true,
    },
  ],
};

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
      series_count: 1,
      instance_count: 120,
    },
  ],
};

async function installCommonMocks(page: Page) {
  await page.route(/\/api\/.*/, (route) => route.fulfill({ status: 200, body: "[]" }));
  await page.route("**/api/auth/me", (r) => jsonRoute(r, ME));
  await page.route(/\/api\/jobs(\?.*)?$/, (r) => jsonRoute(r, { items: [] }));
  await page.route(/\/api\/system\/features$/, (r) => jsonRoute(r, { llm_classifier: false }));
  await page.route(/\/api\/me\/scopes$/, (r) => jsonRoute(r, { scopes: [] }));
  await page.route(`**/api/patients/${PATIENT_ID}`, (r) => jsonRoute(r, PATIENT));
  await page.route(/\/api\/patients(\?.*)?$/, (r) =>
    jsonRoute(r, { items: [PATIENT], total: 1, limit: 50, offset: 0 }),
  );
  await page.route(`**/api/studies/${STUDY_ID}`, (r) => jsonRoute(r, STUDY));
  await page.route(/\/api\/studies(\?.*)?$/, (r) =>
    jsonRoute(r, { items: [STUDY], total: 1, limit: 50, offset: 0 }),
  );
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
      generated_at: "2026-05-10T00:00:00Z",
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
  // Share-link create mock: ritorna dati di successo coerenti con
  // SendStudyDialog post-create state. Stessa shape per le 3 routes
  // (study/folder/patient) per coprire entrambi i percorsi (PatientCard
  // share inline → patient-scope, study card ✉ → study-scope).
  const shareResponse = {
    id: "share-link-test-01",
    token: "tok-walkthrough",
    url: "https://bitvision.example/shared/tok-walkthrough/info",
    password: "Hk7M-9pQX-3vNw-rAcZ",
    permissions: ["shared:download"],
    revoked: false,
    use_count: 0,
    max_uses: null,
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 7 * 24 * 3600_000).toISOString(),
    label: null,
    mode: "claim",
    recipient_name: "Dr. Bianchi",
    recipient_email: "bianchi@example.org",
    requires_password: true,
    generated_password: "Hk7M-9pQX-3vNw-rAcZ",
    deidentify: true,
    received_at: null,
    download_count: 0,
    prepared_status: "running",
    prepared_progress_done: 0,
    prepared_progress_total: 120,
  };
  await page.route(/\/api\/studies\/[^/]+\/share$/, (r) => jsonRoute(r, shareResponse));
  await page.route(/\/api\/patients\/[^/]+\/share$/, (r) => jsonRoute(r, shareResponse));
  await page.route(/\/api\/folders\/[^/]+\/share-link$/, (r) => jsonRoute(r, shareResponse));
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
  page.on("pageerror", (err) => {
    // eslint-disable-next-line no-console
    console.warn(`[pageerror] ${err.message}`);
  });
}

function snap(page: Page, name: string) {
  fs.mkdirSync(SCREEN_DIR, { recursive: true });
  return page.screenshot({
    path: path.join(SCREEN_DIR, `${name}.png`),
    fullPage: true,
  });
}

interface Finding {
  step: string;
  clicks: number;
  notes: string[];
}

const findings: Finding[] = [];

function record(step: string, clicks: number, ...notes: string[]) {
  findings.push({ step, clicks, notes });
}

test.afterAll(() => {
  fs.mkdirSync(SCREEN_DIR, { recursive: true });
  const lines: string[] = [
    "# Walkthrough medico: pre-fix",
    "",
    `Generato: ${new Date().toISOString()}`,
    `Screenshot dir: ${SCREEN_DIR}`,
    "",
    "| Step | Click cumulati | Note |",
    "|---|---:|---|",
  ];
  for (const f of findings) {
    const note = f.notes.join("<br/>");
    lines.push(`| ${f.step} | ${f.clicks} | ${note} |`);
  }
  fs.writeFileSync(path.join(SCREEN_DIR, "report.md"), `${lines.join("\n")}\n`);
});

test.describe("Medico mittente: dalla home al 'link da copiare'", () => {
  test("walkthrough con screenshot a ogni step", async ({ page }) => {
    await setup(page);
    let clicks = 0;

    // ---- Step 1: landing post-login ----
    await page.goto("/patients");
    await expect(page.getByRole("link", { name: /Mario Rossi/ }).first()).toBeVisible({
      timeout: 5_000,
    });
    await snap(page, "01-patients-list");

    // Dopo P0a: la PatientCard espone un bottone "Invia" inline
    // (aria-label "Condividi questo paziente con un collega"). Conta
    // sulla pagina i bottoni di share visibili — il pre-fix aveva 0.
    const inlineShareButtons = await page
      .getByRole("button", { name: /Condividi questo paziente/i })
      .count();
    record(
      "1. Patient list",
      clicks,
      `Bottoni 'Invia' inline su PatientCard: ${inlineShareButtons}`,
      "Target P0a: >= 1 (era 0 nel pre-fix)",
    );

    // ---- Step 2: percorso veloce — click diretto su 'Invia' ----
    // Apre il SendStudyDialog patient-scoped via deep-link
    // ``?dialog=share``. Salta il "scroll cerca study card cerca ✉".
    if (inlineShareButtons > 0) {
      await page
        .getByRole("button", { name: /Condividi questo paziente/i })
        .first()
        .click();
      clicks++;
      await page.waitForURL(/\?dialog=share/);
      const dialogQuick = page.getByRole("dialog");
      await expect(dialogQuick).toBeVisible({ timeout: 5_000 });
      await snap(page, "02-quick-path-dialog-opened");
      record(
        "2. Quick path: click 'Invia' su PatientCard",
        clicks,
        "Click sul bottone 'Invia' inline → SendStudyDialog aperto direttamente",
        `Click cumulati: ${clicks} (target P0: <= 1 per arrivare al dialog)`,
      );
      // Esci da questa modalità per testare anche il percorso "lungo"
      // (study card ✉) che resta valido come fallback.
      await page.keyboard.press("Escape");
      await page.waitForURL((url) => !url.searchParams.has("dialog"), {
        timeout: 4_000,
      });
      // Torno alla home per riprovare il percorso lungo da pulito.
      await page.goto("/patients");
      clicks = 0;
    }

    // ---- Step 3 (percorso lungo): card → detail → study card → ✉ ----
    await page
      .getByRole("link", { name: /Mario Rossi/ })
      .first()
      .click();
    clicks++;
    await page.waitForURL(new RegExp(`/patients/${PATIENT_ID}(\\?|$)`));
    await snap(page, "03-patient-detail-loaded");

    const sendButtons = await page.getByRole("button", { name: /Invia studio/i }).count();
    record(
      "3. Patient detail (percorso lungo)",
      clicks,
      `Bottoni 'Invia studio' (study card ✉ → SendIcon dopo P0b): ${sendButtons}`,
    );

    const firstSend = page.getByRole("button", { name: /Invia studio/i }).first();
    await firstSend.click();
    clicks++;
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible({ timeout: 5_000 });
    await snap(page, "04-send-study-dialog-opened");

    // ---- Step 4: scelgo "copia link" + submit ----
    const copyMode = dialog.getByRole("radio", {
      name: /Copia il link|Copia link/i,
    });
    if (await copyMode.count()) {
      await copyMode.first().click();
      clicks++;
    }
    const submitButton = dialog.getByRole("button", {
      name: /Crea link e copia|Invia$|Crea link/i,
    });
    await submitButton.first().click();
    clicks++;

    // ---- Step 5: post-create — verifica QR + bottoni ----
    // Il post-fix ha SuccessView vera (form nascosto, link in primo
    // piano). Aspetto che il link box compaia.
    const linkBox = dialog.getByText(/Link condivisione|Link pronto/i);
    try {
      await linkBox.first().waitFor({ state: "visible", timeout: 5_000 });
    } catch {
      // ignored — screenshot still captures it
    }

    const formVisible = await dialog.getByText(/Destinatario/i).count();
    const qrButton = dialog.getByRole("button", { name: /Mostra QR|Show QR/i });
    const hasQrButton = (await qrButton.count()) > 0;
    record(
      "5. Post-create state (success view)",
      clicks,
      `Sezione 'Destinatario' del form ancora visibile: ${formVisible > 0 ? "sì (pain)" : "no (form collassato)"}`,
      `Bottone 'Mostra QR' visibile: ${hasQrButton ? "sì (P0c ok)" : "no"}`,
    );

    // Apriamo il QR e verifichiamo che venga renderizzato.
    if (hasQrButton) {
      await qrButton.first().click();
      clicks++;
      const qrImage = dialog.getByRole("img", {
        name: /Inquadra con il telefono|Have the recipient scan/i,
      });
      try {
        await qrImage.waitFor({ state: "visible", timeout: 4_000 });
      } catch {
        // QR generation may take a tick
      }
      await snap(page, "05-send-study-dialog-with-qr");
      record(
        "6. QR code rendered",
        clicks,
        `QR <img> visibile: ${(await qrImage.count()) > 0 ? "sì" : "no"}`,
      );
    } else {
      await snap(page, "05-send-study-dialog-post-create");
    }

    record(
      "TOTALE click sul percorso lungo (PatientCard → study ✉ → submit)",
      clicks,
      `Click totali: ${clicks} (era 4 nel pre-fix). Il percorso QUICK (PatientCard 'Invia') costa 1 click.`,
    );
  });
});

test.describe("PatientCard share button gating", () => {
  test("non mostrato quando origin=shared (non sei tu il creatore)", async ({ page }) => {
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
    // Override the patients-list mock with a "shared" origin: the share
    // button must hide so the user doesn't try to re-share something
    // they only have read access to.
    await page.route(/\/api\/.*/, (r) => r.fulfill({ status: 200, body: "[]" }));
    await page.route("**/api/auth/me", (r) => jsonRoute(r, ME));
    await page.route(/\/api\/jobs(\?.*)?$/, (r) => jsonRoute(r, { items: [] }));
    await page.route(/\/api\/system\/features$/, (r) => jsonRoute(r, { llm_classifier: false }));
    await page.route(/\/api\/me\/scopes$/, (r) => jsonRoute(r, { scopes: [] }));
    await page.route(/\/api\/patients(\?.*)?$/, (r) =>
      jsonRoute(r, {
        items: [{ ...PATIENT, origin: "shared" }],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    );

    await page.goto("/patients");
    await expect(page.getByRole("link", { name: /Mario Rossi/ }).first()).toBeVisible();
    const shareCount = await page
      .getByRole("button", { name: /Condividi questo paziente/i })
      .count();
    expect(shareCount).toBe(0);
  });

  test("mostrato quando origin=mine", async ({ page }) => {
    await setup(page);
    await page.goto("/patients");
    const shareButton = page.getByRole("button", { name: /Condividi questo paziente/i }).first();
    await expect(shareButton).toBeVisible({ timeout: 5_000 });
  });
});

test.describe("Medico destinatario: dal link al download", () => {
  test("/shared/{token}/info — landing pubblica con prep running", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.context().addCookies([
      { name: "BVP_LOCALE", value: "it", domain: "localhost", path: "/" },
      { name: "BVP_LOCALE", value: "it", domain: "127.0.0.1", path: "/" },
    ]);
    const TOKEN = "tok-walkthrough-running";
    await page.route(`**/api/shared/${TOKEN}/info`, (r) =>
      jsonRoute(r, {
        study_title: "TC torace 2026-04-12",
        modalities: ["CT"],
        study_date: "2026-04-12",
        requires_password: true,
        expires_at: new Date(Date.now() + 7 * 24 * 3600_000).toISOString(),
        permissions: ["shared:download"],
        max_uses: null,
        uses_remaining: null,
        resource_kind: "study",
        resource_id: "study-aaaa",
        mode: "claim",
        claimable: false,
        recipient_name: "Dr. Bianchi",
        recipient_email: "bianchi@example.org",
        deidentified: true,
        total_files: 120,
        total_bytes: 350 * 1024 * 1024,
        grantor_display: "Dr. Angelo",
        prepared_status: "running",
        prepared_progress_done: 30,
        prepared_progress_total: 120,
      }),
    );

    await page.goto(`/shared/${TOKEN}/info`);
    await snap(page, "10-recipient-landing-running");
    record(
      "10. Recipient landing (prep running)",
      0,
      "Mostra titolo studio + modalità + scadenza + barra di progresso",
      "Download disabilitato finché prep non finisce",
    );
  });

  test("/shared/{token}/info — landing pubblica con prep ready", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.context().addCookies([
      { name: "BVP_LOCALE", value: "it", domain: "localhost", path: "/" },
      { name: "BVP_LOCALE", value: "it", domain: "127.0.0.1", path: "/" },
    ]);
    const TOKEN = "tok-walkthrough-ready";
    await page.route(`**/api/shared/${TOKEN}/info`, (r) =>
      jsonRoute(r, {
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
        recipient_name: "Dr. Bianchi",
        recipient_email: "bianchi@example.org",
        deidentified: true,
        total_files: 120,
        total_bytes: 350 * 1024 * 1024,
        grantor_display: "Dr. Angelo",
        prepared_status: "succeeded",
        prepared_progress_done: 120,
        prepared_progress_total: 120,
      }),
    );

    await page.goto(`/shared/${TOKEN}/info`);
    await snap(page, "11-recipient-landing-ready");

    const dlAnchor = page.getByRole("link", { name: /Scarica DICOM/i });
    const hasDownload = (await dlAnchor.count()) > 0;
    record(
      "11. Recipient landing (prep ready)",
      0,
      `Bottone 'Scarica DICOM' visibile: ${hasDownload ? "sì" : "no"}`,
      "Niente viewer in browser — solo download ZIP",
    );
  });
});
