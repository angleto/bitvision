import type { Metadata, Viewport } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";
import { type ReactNode, Suspense } from "react";

import { ModalProvider } from "@/components/ModalHost";
import ServiceWorkerRegistrar from "@/components/ServiceWorkerRegistrar";
import ShareGuestBanner from "@/components/ShareGuestBanner";
import SiteHeader from "@/components/SiteHeader";
import ThemeInit from "@/components/ThemeInit";
import { AuthProvider } from "@/lib/auth-context";

import "./globals.css";

export const metadata: Metadata = {
  title: "bitvision phoenix",
  description: "Open, trustworthy, consent-based medical imaging infrastructure.",
  // Next emits <link rel="manifest"> for app/manifest.ts automatically;
  // what it cannot infer is the iOS side. Safari ignores the manifest
  // when installing and reads these instead, so without them "Add to
  // Home Screen" yields a screenshot thumbnail that opens in a browser
  // tab rather than as an app.
  appleWebApp: {
    capable: true,
    title: "bitvision",
    // "default" keeps the status bar legible in both themes; the
    // translucent variant lets page content slide under it, which on a
    // record view means the header overlapping the patient name.
    statusBarStyle: "default",
  },
  other: {
    // Next 15 emits only the modern ``mobile-web-app-capable``. iOS
    // Safari has honoured that since 16.4; before that it reads the
    // ``apple-`` prefixed name, and without it "Add to Home Screen"
    // produces a bookmark that opens in a browser tab instead of an
    // app window. A phone old enough to matter here is exactly the
    // phone this is being installed on.
    "apple-mobile-web-app-capable": "yes",
  },
  icons: {
    apple: [{ url: "/icons/apple-touch-icon.png", sizes: "180x180" }],
    icon: [
      { url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  },
};

/**
 * Painted behind the OS chrome. Declared per colour scheme because the
 * manifest can only carry one value, and a white status bar above a
 * dark app is the sort of detail that makes an installed app feel like
 * a bookmark. Values are the ``--bv-bg-elevated`` tokens from
 * globals.css, repeated here because the browser needs them before any
 * stylesheet has loaded.
 */
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#11151e" },
  ],
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const locale = await getLocale();
  const messages = await getMessages();
  return (
    <html lang={locale}>
      <body>
        <NextIntlClientProvider locale={locale} messages={messages}>
          <ThemeInit />
          <ServiceWorkerRegistrar />
          <AuthProvider>
            <ModalProvider>
              {/* Suspense boundary covers SiteHeader's useSearchParams() so
                  Next can still statically prerender pages that don't use it. */}
              <Suspense fallback={<div style={{ height: 49 }} />}>
                <SiteHeader />
              </Suspense>
              {/* Always-visible "you're a guest — create an account to keep
                  this access" bar; self-hides for normal accounts. */}
              <ShareGuestBanner />
              {children}
            </ModalProvider>
          </AuthProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
