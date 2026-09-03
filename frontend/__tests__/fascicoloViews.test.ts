import {
  DEFAULT_VIEW,
  FASCICOLO_VIEWS,
  isDeepLinkView,
  isView,
  parseView,
  viewKeys,
} from "@/lib/fascicoloViews";
import { describe, expect, it } from "vitest";

import messagesEn from "../messages/en.json";
import messagesIt from "../messages/it.json";

describe("parseView", () => {
  // Regression: the patient page used to accept only 5 of the 9 tabs, so
  // ``?view=tasks`` rendered Drive on first paint and flipped once the
  // sync effect ran. Every tab the toggle can render must round-trip.
  it("accepts every tab the Health Record renders", () => {
    for (const v of FASCICOLO_VIEWS) {
      expect(parseView(v)).toBe(v);
    }
    expect(FASCICOLO_VIEWS).toContain("tasks");
    expect(FASCICOLO_VIEWS).toContain("calendar");
    expect(FASCICOLO_VIEWS).toContain("ask");
    expect(FASCICOLO_VIEWS).toContain("shares");
  });

  it("falls back to the default tab on unknown, empty and absent values", () => {
    expect(parseView("nope")).toBe(DEFAULT_VIEW);
    expect(parseView("")).toBe(DEFAULT_VIEW);
    expect(parseView(null)).toBe(DEFAULT_VIEW);
    expect(parseView(undefined)).toBe(DEFAULT_VIEW);
  });

  it("guards on unknown input types", () => {
    expect(isView(undefined)).toBe(false);
    expect(isView(3)).toBe(false);
    expect(isView("Drive")).toBe(false); // case-sensitive by design
  });
});

describe("isDeepLinkView", () => {
  it("is true for every non-default tab", () => {
    for (const v of FASCICOLO_VIEWS.filter((v) => v !== DEFAULT_VIEW)) {
      expect(isDeepLinkView(v)).toBe(true);
    }
  });

  // An unrecognised value renders the default tab, so scrolling past the
  // header would strand the user somewhere they did not ask to be.
  it("is false for the default tab and for unrecognised values", () => {
    expect(isDeepLinkView(DEFAULT_VIEW)).toBe(false);
    expect(isDeepLinkView("nope")).toBe(false);
    expect(isDeepLinkView(null)).toBe(false);
  });
});

describe("viewKeys", () => {
  it("resolves against the real catalogues, in both locales", () => {
    for (const locale of [messagesEn, messagesIt]) {
      // ``fascicolo.v3`` is a flat string map; the JSON import types it
      // with literal keys, which a computed lookup cannot satisfy.
      const v3 = locale.fascicolo.v3 as unknown as Record<string, string | undefined>;
      for (const v of FASCICOLO_VIEWS) {
        const keys = viewKeys(v);
        expect(v3[keys.tab], `${v}: ${keys.tab}`).toBeTruthy();
        expect(v3[keys.hint], `${v}: ${keys.hint}`).toBeTruthy();
        if (keys.caption !== null) {
          expect(v3[keys.caption], `${v}: ${keys.caption}`).toBeTruthy();
        }
      }
    }
  });

  // Drive renders the Drive layout directly, with no caption line above.
  it("has no caption for the default tab", () => {
    expect(viewKeys(DEFAULT_VIEW).caption).toBeNull();
    expect(viewKeys("events").caption).toBe("captionEvents");
  });

  it("has no duplicate ids", () => {
    expect(new Set(FASCICOLO_VIEWS).size).toBe(FASCICOLO_VIEWS.length);
  });
});
