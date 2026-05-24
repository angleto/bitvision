import { describe, expect, it } from "vitest";

import { safeInternalPath } from "@/lib/safe-redirect";

describe("safeInternalPath", () => {
  const fallback = "/studies";

  it("returns the fallback for null / undefined / empty input", () => {
    expect(safeInternalPath(null, fallback)).toBe(fallback);
    expect(safeInternalPath(undefined, fallback)).toBe(fallback);
    expect(safeInternalPath("", fallback)).toBe(fallback);
  });

  it("accepts plain same-origin paths", () => {
    expect(safeInternalPath("/patients", fallback)).toBe("/patients");
    expect(safeInternalPath("/patients/abc?tab=labs", fallback)).toBe("/patients/abc?tab=labs");
    expect(safeInternalPath("/patients/abc#row-3", fallback)).toBe("/patients/abc#row-3");
  });

  it("rejects absolute URLs", () => {
    expect(safeInternalPath("https://attacker.example/path", fallback)).toBe(fallback);
    expect(safeInternalPath("http://evil.example", fallback)).toBe(fallback);
    expect(safeInternalPath("javascript:alert(1)", fallback)).toBe(fallback);
    expect(safeInternalPath("data:text/html,evil", fallback)).toBe(fallback);
  });

  it("rejects protocol-relative URLs", () => {
    // Browsers resolve //evil.example under the page's own scheme and
    // route off-origin. This is the most common open-redirect payload.
    expect(safeInternalPath("//evil.example", fallback)).toBe(fallback);
    expect(safeInternalPath("//evil.example/path", fallback)).toBe(fallback);
  });

  it("rejects backslash escapes", () => {
    expect(safeInternalPath("/\\evil.example", fallback)).toBe(fallback);
  });

  it("rejects NUL byte payloads", () => {
    expect(safeInternalPath("/foo\x00bar", fallback)).toBe(fallback);
  });

  it("rejects values that do not start with a single slash", () => {
    expect(safeInternalPath("foo", fallback)).toBe(fallback);
    expect(safeInternalPath("./relative", fallback)).toBe(fallback);
    expect(safeInternalPath("../relative", fallback)).toBe(fallback);
  });
});
