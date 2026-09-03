import type { MetadataRoute } from "next";

/**
 * Web app manifest — what makes the app installable on a phone.
 *
 * Served by the App Router at `/manifest.webmanifest`, and linked from
 * the document head automatically: Next emits the `<link rel="manifest">`
 * for this route, so nothing in `layout.tsx` has to remember to.
 *
 * Chrome installs a page when it has a manifest carrying `name`,
 * `short_name`, `start_url`, a `display` of `standalone` (or
 * `fullscreen` / `minimal-ui`), at least a 192px and a 512px icon, and
 * the page is served over HTTPS with a service worker that has a fetch
 * handler. All six are satisfied here and in `public/sw.js`.
 *
 * iOS ignores the manifest for installation — Safari installs from
 * Share → Add to Home Screen and reads `apple-touch-icon` plus the
 * `apple-mobile-web-app-*` meta tags declared in `layout.tsx`. It does
 * honour `display: standalone` once installed, so the app opens without
 * browser chrome there too.
 *
 * `start_url` is `/patients` rather than `/`: on a phone this app is a
 * way to look something up or check an appointment, and the root is a
 * marketing page. A session-less launch lands on `/login` by the normal
 * redirect, so the choice costs nothing when signed out.
 *
 * The theme colour is the light-theme surface rather than the brand
 * orange. It paints the Android status bar and the iOS splash, and the
 * app is a health record read at arm's length — a saturated bar around
 * clinical content is noise, and it would clash outright in dark mode.
 * A single value is used because the manifest cannot express two; the
 * page itself remains fully theme-aware.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "bitvision phoenix",
    short_name: "bitvision",
    description:
      "Your health record: imaging, reports, appointments. Open, consent-based, and yours.",
    start_url: "/patients",
    scope: "/",
    display: "standalone",
    orientation: "any",
    // --bv-bg-elevated / --bv-bg from globals.css, light theme.
    theme_color: "#ffffff",
    background_color: "#f6f7f9",
    lang: "it",
    dir: "ltr",
    categories: ["medical", "health", "productivity"],
    icons: [
      {
        src: "/icons/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      // Maskable icons are cropped to whatever shape the launcher uses.
      // They carry an opaque background and a smaller mark, because a
      // transparent maskable icon renders as a black blob on Android.
      {
        src: "/icons/maskable-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "maskable",
      },
      {
        src: "/icons/maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
