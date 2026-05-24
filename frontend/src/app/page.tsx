import { getTranslations } from "next-intl/server";
import Link from "next/link";

import { API_BASE_URL } from "@/lib/api";

// Server-component landing page. Anonymous-by-design: no auth probe,
// no per-user data. Renders the same HTML for everyone (the language
// catalogue is the only request-time variable, resolved server-side
// by next-intl).

const ICON_SIZE = 36;
const ICON_STROKE = 1.6;

function Icon({
  children,
  label,
}: {
  children: React.ReactNode;
  label: string;
}) {
  return (
    <span
      role="img"
      aria-label={label}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: 56,
        height: 56,
        borderRadius: 12,
        background: "var(--bv-card-bg-soft, rgba(0, 122, 204, 0.08))",
        color: "var(--bv-accent, #0a84ff)",
        marginBottom: "0.6rem",
      }}
    >
      <svg
        width={ICON_SIZE}
        height={ICON_SIZE}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={ICON_STROKE}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        {children}
      </svg>
    </span>
  );
}

// Small SVG path libraries for the six capability cards. Stroke-only,
// 24x24 viewBox, no fills — keeps the look consistent across light /
// dark themes via currentColor.
const ICONS = {
  viewer: (
    <>
      <rect x="3" y="4" width="18" height="14" rx="2" />
      <path d="M3 14h18" />
      <path d="M9 4v14" />
      <path d="M15 4v14" />
      <circle cx="12" cy="11" r="1.4" />
    </>
  ),
  fascicolo: (
    <>
      <circle cx="6" cy="6" r="2" />
      <circle cx="6" cy="18" r="2" />
      <circle cx="18" cy="12" r="2" />
      <path d="M6 8v8" />
      <path d="M8 6h6a4 4 0 0 1 4 4v0" />
      <path d="M8 18h6a4 4 0 0 0 4-4v0" />
    </>
  ),
  ai: (
    <>
      <path d="M12 2v3" />
      <path d="M12 19v3" />
      <path d="M4.2 4.2l2.1 2.1" />
      <path d="M17.7 17.7l2.1 2.1" />
      <path d="M2 12h3" />
      <path d="M19 12h3" />
      <path d="M4.2 19.8l2.1-2.1" />
      <path d="M17.7 6.3l2.1-2.1" />
      <circle cx="12" cy="12" r="3.5" />
    </>
  ),
  sharing: (
    <>
      <circle cx="6" cy="12" r="2.5" />
      <circle cx="18" cy="6" r="2.5" />
      <circle cx="18" cy="18" r="2.5" />
      <path d="M8.2 10.8l7.6-3.6" />
      <path d="M8.2 13.2l7.6 3.6" />
    </>
  ),
  openData: (
    <>
      <ellipse cx="12" cy="6" rx="8" ry="2.5" />
      <path d="M4 6v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5V6" />
      <path d="M4 12v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-6" />
    </>
  ),
  openSource: (
    <>
      <path d="M12 2l9 5v10l-9 5-9-5V7l9-5z" />
      <path d="M12 2v20" />
      <path d="M3 7l9 5 9-5" />
    </>
  ),
};

interface Capability {
  iconKey: keyof typeof ICONS;
  titleKey: string;
  bodyKey: string;
}

const CAPABILITIES: Capability[] = [
  { iconKey: "viewer", titleKey: "viewerTitle", bodyKey: "viewerBody" },
  { iconKey: "fascicolo", titleKey: "fascicoloTitle", bodyKey: "fascicoloBody" },
  { iconKey: "ai", titleKey: "aiTitle", bodyKey: "aiBody" },
  { iconKey: "sharing", titleKey: "sharingTitle", bodyKey: "sharingBody" },
  { iconKey: "openData", titleKey: "openDataTitle", bodyKey: "openDataBody" },
  { iconKey: "openSource", titleKey: "openSourceTitle", bodyKey: "openSourceBody" },
];

export default async function HomePage() {
  const t = await getTranslations("landing");

  return (
    <main
      style={{
        maxWidth: 1080,
        margin: "0 auto",
        padding: "2.5rem 1.25rem 3rem",
      }}
    >
      <section style={{ textAlign: "center", marginBottom: "2.75rem" }}>
        <h1 style={{ marginBottom: "0.6rem", fontSize: "2.2rem", lineHeight: 1.15 }}>
          {t("headline")}
        </h1>
        <p
          className="meta"
          style={{
            maxWidth: 640,
            margin: "0 auto",
            fontSize: "1.02rem",
            lineHeight: 1.5,
          }}
        >
          {t("tagline")}
        </p>
      </section>

      <section aria-labelledby="capabilities-title">
        <h2
          id="capabilities-title"
          style={{
            textAlign: "center",
            fontSize: "0.9rem",
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--bv-muted, #6b7280)",
            marginBottom: "1.5rem",
            fontWeight: 600,
          }}
        >
          {t("capabilitiesTitle")}
        </h2>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: "1rem",
          }}
        >
          {CAPABILITIES.map((cap) => (
            <article
              key={cap.iconKey}
              className="card"
              style={{
                display: "flex",
                flexDirection: "column",
                alignItems: "flex-start",
                padding: "1.2rem 1.1rem",
              }}
            >
              <Icon label={t(cap.titleKey)}>{ICONS[cap.iconKey]}</Icon>
              <h3 style={{ margin: "0 0 0.35rem", fontSize: "1.02rem" }}>{t(cap.titleKey)}</h3>
              <p className="meta" style={{ margin: 0, fontSize: "0.86rem", lineHeight: 1.45 }}>
                {t(cap.bodyKey)}
              </p>
            </article>
          ))}
        </div>
      </section>

      <p
        role="note"
        style={{
          marginTop: "2.5rem",
          padding: "0.75rem 1rem",
          textAlign: "center",
          fontSize: "0.8rem",
          lineHeight: 1.5,
          color: "var(--bv-fg-soft, #555)",
          border: "1px solid var(--bv-card-border, #e5e7eb)",
          borderRadius: 8,
          background: "var(--bv-warn-soft, rgba(234, 179, 8, 0.08))",
        }}
      >
        {t("medicalDisclaimer")}
      </p>

      <p
        className="meta"
        style={{
          marginTop: "1.25rem",
          textAlign: "center",
          fontSize: "0.82rem",
        }}
      >
        <a href={`${API_BASE_URL}/docs`}>{t("footerApiDocs")}</a>
        {" · "}
        <Link href="/transparency">{t("footerTransparency")}</Link>
        {" · "}
        <a href="https://github.com/angleto/bitvision">{t("footerGitHub")}</a>
        {" · "}
        {t("footerLicense")}
      </p>
    </main>
  );
}
