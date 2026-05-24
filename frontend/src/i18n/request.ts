// next-intl request config: resolves the active locale from a cookie
// (no URL prefix, no routing changes) and loads the matching messages
// catalogue from frontend/messages/<locale>.json.
//
// Locale precedence:
//   1. ``BVP_LOCALE`` cookie set by the LanguageSwitcher.
//   2. ``Accept-Language`` request header parsed for ``it`` / ``en``.
//   3. Default: ``it`` (the platform is Italian-first; non-IT browsers
//      can still pick English via Accept-Language or the switcher).

import { getRequestConfig } from "next-intl/server";
import { cookies, headers } from "next/headers";

export const SUPPORTED_LOCALES = ["en", "it"] as const;
export type AppLocale = (typeof SUPPORTED_LOCALES)[number];
export const DEFAULT_LOCALE: AppLocale = "it";
export const LOCALE_COOKIE = "BVP_LOCALE";

function isSupported(value: string | null | undefined): value is AppLocale {
  return value === "en" || value === "it";
}

async function resolveLocale(): Promise<AppLocale> {
  const cookieStore = await cookies();
  const fromCookie = cookieStore.get(LOCALE_COOKIE)?.value;
  if (isSupported(fromCookie)) return fromCookie;

  const hdr = (await headers()).get("accept-language") ?? "";
  // First language tag wins; we only care about the primary subtag.
  const primary = hdr.split(",")[0]?.split("-")[0]?.toLowerCase();
  if (isSupported(primary)) return primary;
  return DEFAULT_LOCALE;
}

export default getRequestConfig(async () => {
  const locale = await resolveLocale();
  const messages = (await import(`../../messages/${locale}.json`)).default;
  return { locale, messages };
});
