import { getTranslations } from "next-intl/server";

/**
 * Shown by the service worker when a navigation cannot reach the
 * network. Deliberately static and free of any record data: it is the
 * one page allowed into the device cache, so it must be safe to leave
 * there indefinitely on a phone that may be shared or lost.
 *
 * It says what is missing and what to do, and offers a retry that is
 * just a reload — there is nothing to recover locally, because nothing
 * clinical is stored offline by design.
 */
export default async function OfflinePage() {
  const t = await getTranslations("offline");
  return (
    <main
      style={{
        maxWidth: "34rem",
        margin: "4rem auto",
        padding: "0 1.25rem",
        display: "flex",
        flexDirection: "column",
        gap: "0.9rem",
      }}
    >
      <h1 style={{ margin: 0, fontSize: "1.3rem" }}>{t("title")}</h1>
      <p style={{ margin: 0, color: "var(--bv-fg-soft)" }}>{t("body")}</p>
      <p style={{ margin: 0, color: "var(--bv-fg-soft)", fontSize: "0.88rem" }}>{t("why")}</p>
      {/* A plain link, not a button: the page is served from cache with
          no JavaScript guarantee, and a link works either way. */}
      <p style={{ margin: 0 }}>
        <a href="/patients">{t("retry")}</a>
      </p>
    </main>
  );
}
