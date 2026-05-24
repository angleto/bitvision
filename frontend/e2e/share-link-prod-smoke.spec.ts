// Smoke test contro produzione (o staging) per il flow recipient di
// uno share link. Non scarica byte, non fa POST: solo GET /info per
// verificare che la landing renda e che il backend risponda 200.
//
// Uso pre-deploy (baseline) e post-deploy (regressione):
//
//   E2E_BASE_URL=https://bitvision.example \
//   E2E_USE_REAL_BACKEND=1 \
//   E2E_PROD_TOKEN=<token-pubblico> \
//   pnpm playwright test share-link-prod-smoke
//
// Il test si auto-skippa se le env var non sono settate, così girare
// l'intera suite hermetic ``pnpm playwright test`` non lo esegue.
//
// Cosa NON fa:
//   - Non immette password
//   - Non chiama /api/shared/{tok}/download
//   - Non confirm-receipt
// Lo scopo è solo verificare il contratto base: la pagina si monta
// e il backend serve i metadati.

import { expect, test } from "@playwright/test";

const TOKEN = process.env.E2E_PROD_TOKEN;
const PROD_BASE = process.env.E2E_BASE_URL;

test.describe("Share link recipient flow — production smoke", () => {
  test.skip(!TOKEN, "E2E_PROD_TOKEN non settato");
  test.skip(!PROD_BASE, "E2E_BASE_URL non settato");

  test("backend /api/shared/{token}/info ritorna 200 con shape attesa", async ({ request }) => {
    const res = await request.get(`/api/shared/${TOKEN}/info`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    // Campi che il frontend recipient consuma. Se il backend dovesse
    // togliere uno di questi, il rendering si romperebbe — qui ce ne
    // accorgiamo prima della tua telefonata col medico.
    for (const field of [
      "study_title",
      "modalities",
      "study_date",
      "requires_password",
      "expires_at",
      "permissions",
      "resource_kind",
      "resource_id",
      "mode",
      "deidentified",
      "prepared_status",
    ]) {
      expect(body, `field "${field}" missing in /info response`).toHaveProperty(field);
    }
  });

  test("/shared/{token}/info renderizza senza client-side error", async ({ page }) => {
    await page.goto(`/shared/${TOKEN}/info`);
    // La landing deve montare entro 8s e NON rendere il boundary di
    // Next.js per errori client-side. Aspettiamo un piccolo settle
    // perché la pagina fa un fetch /info al mount.
    await page.waitForLoadState("networkidle", { timeout: 10_000 });
    const html = await page.content();
    expect(html).not.toContain("Application error: a client-side exception");
    // La pagina deve mostrare almeno una "ancora di senso" tra:
    // - download CTA (it/en)
    // - prompt password (it/en)
    // - prep status (it/en)
    // - heading risorsa condivisa (study / fascicolo / folder, it/en)
    const hasContent =
      (await page.getByRole("link", { name: /Scarica DICOM|Download DICOM/i }).count()) > 0 ||
      (await page
        .getByRole("button", { name: /Verifica|Accedi|Conferma|Verify|Sign\s?in|Confirm/i })
        .count()) > 0 ||
      (await page.getByText(/preparazione|in coda|preparing|queued/i).count()) > 0 ||
      (await page
        .getByRole("heading", {
          name: /Fascicolo|Studio|Health\s?Record|Study|Cartella|Folder|Shared/i,
        })
        .count()) > 0;
    expect(hasContent, "landing senza affordance riconoscibile").toBe(true);
  });
});
