"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useState } from "react";

import { isViewerSupported } from "@/lib/browserSupport";

/**
 * Wrap any Cornerstone-mounting region so the viewer never tries to
 * boot on a browser missing WebGL2 / WebAssembly. The check runs
 * client-side after mount (the SSR pass renders nothing so the markup
 * matches between server and client), then either passes through to
 * `children` or shows a localized "browser non supportato" panel.
 *
 * Desktop is the primary target; this gate exists mostly to handle
 * mobile Safari < 15, in-app browsers (Instagram / Facebook webviews)
 * and very old Edge versions, where mounting Cornerstone produces a
 * black canvas with no error visible to the user.
 */
export default function BrowserSupportGate({ children }: { children: ReactNode }) {
  const t = useTranslations("viewerSupport");
  // Render nothing until we've probed (avoids a flash of either UI on
  // the SSR → hydrate boundary).
  const [supported, setSupported] = useState<boolean | null>(null);

  useEffect(() => {
    setSupported(isViewerSupported());
  }, []);

  if (supported === null) return null;
  if (supported) return <>{children}</>;

  return (
    <div
      role="alert"
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "0.75rem",
        height: "100%",
        minHeight: 240,
        padding: "1.5rem",
        background: "#0a0d14",
        color: "#e6ecf3",
        textAlign: "center",
      }}
    >
      <div
        aria-hidden
        style={{
          width: 48,
          height: 48,
          borderRadius: "50%",
          background: "#1a1d25",
          border: "1px solid #2a2f3b",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: "1.5rem",
        }}
      >
        ⚠
      </div>
      <h2 style={{ margin: 0, fontSize: "1.05rem" }}>{t("title")}</h2>
      <p style={{ margin: 0, maxWidth: 480, fontSize: "0.9rem", color: "#c5cdd9" }}>{t("body")}</p>
      <p style={{ margin: 0, maxWidth: 480, fontSize: "0.82rem", color: "#94a3b8" }}>{t("hint")}</p>
    </div>
  );
}
