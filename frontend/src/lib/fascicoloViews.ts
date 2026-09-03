// Single source of truth for the Health Record tab set.
//
// The list used to be spelled out in five places: the ``View`` union and
// the ``VIEWS`` array in ``FascicoloViewToggle``, the ``if (fromUrl ===
// …)`` chain that re-syncs state from the URL, plus ``initialView`` and
// ``hasDeepLink`` in the patient page. They drifted: ``initialView``
// never learned about ``tasks``, ``calendar``, ``ask`` and ``shares``,
// so a link to any of those four rendered the Drive pane on first paint
// and only flipped once the sync effect ran — a visible flash plus a
// scroll jump on a slow phone. Those links are not hypothetical:
// ``SendStudyDialog`` points at ``?view=shares`` and
// ``/patients/{id}/tasks`` redirects to ``?view=tasks``.
//
// Keeping one array removes the class of bug rather than the instance.

/**
 * Tabs in render order. The order is the navigation order and is
 * deliberate:
 *
 * - ``events`` then ``tasks``: the operational checklist lives next to
 *   the clinical timeline. The merged view (events + tasks together) is
 *   reachable from either tab via the "Vista unificata" toggle (URL key
 *   ``merge=1``).
 * - ``ask`` sits after the primary clinical surfaces (drive / events /
 *   tasks / calendar) so the natural-language entry point stays findable
 *   without being buried under the audit and sharing tabs.
 * - ``provenance`` last: audit trail, lookup surface, almost always
 *   reached last.
 */
export const FASCICOLO_VIEWS = [
  "drive",
  "events",
  "tasks",
  "calendar",
  "ask",
  "documents",
  "evidence",
  "shares",
  "provenance",
] as const;

export type View = (typeof FASCICOLO_VIEWS)[number];

/** Tab shown when ``?view=`` is absent or unrecognised. */
export const DEFAULT_VIEW: View = "drive";

export function isView(value: unknown): value is View {
  return typeof value === "string" && (FASCICOLO_VIEWS as readonly string[]).includes(value);
}

/**
 * Parse a raw ``?view=`` value, falling back to the default tab.
 *
 * Unrecognised values fall back rather than throw: ``?view=`` is
 * user-editable and arrives from bookmarks that may predate a rename.
 */
export function parseView(value: string | null | undefined): View {
  return isView(value) ? value : DEFAULT_VIEW;
}

/**
 * True when ``?view=`` addresses a tab other than the default, i.e. the
 * URL deep-links into the Health Record and the page should scroll the
 * work surface into view on mount.
 *
 * An unrecognised value is NOT a deep link: it renders the default tab,
 * so scrolling past the header would strand the user somewhere they did
 * not ask to be.
 */
export function isDeepLinkView(value: string | null | undefined): boolean {
  return isView(value) && value !== DEFAULT_VIEW;
}

/**
 * i18n keys for a tab, under the ``fascicolo.v3`` namespace.
 *
 * Derived from the tab id rather than tabulated, so adding a tab to
 * ``FASCICOLO_VIEWS`` cannot leave a lookup table behind. ``caption`` is
 * absent for ``drive``: that pane renders the Drive layout directly, with
 * no explanatory line above it.
 */
export function viewKeys(view: View): { tab: string; hint: string; caption: string | null } {
  const suffix = view.charAt(0).toUpperCase() + view.slice(1);
  return {
    tab: `tab${suffix}`,
    hint: `hint${suffix}`,
    caption: view === DEFAULT_VIEW ? null : `caption${suffix}`,
  };
}
