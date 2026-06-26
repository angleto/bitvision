import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";
import { type ReactNode, Suspense } from "react";

import { ModalProvider } from "@/components/ModalHost";
import ShareGuestBanner from "@/components/ShareGuestBanner";
import SiteHeader from "@/components/SiteHeader";
import ThemeInit from "@/components/ThemeInit";
import { AuthProvider } from "@/lib/auth-context";

import "./globals.css";

export const metadata: Metadata = {
  title: "bitvision phoenix",
  description: "Open, trustworthy, consent-based medical imaging infrastructure.",
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const locale = await getLocale();
  const messages = await getMessages();
  return (
    <html lang={locale}>
      <body>
        <NextIntlClientProvider locale={locale} messages={messages}>
          <ThemeInit />
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
