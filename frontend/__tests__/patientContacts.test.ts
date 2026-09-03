import { describe, expect, it } from "vitest";

import {
  type EditableContact,
  buildContactPayload,
  findDuplicateEmail,
  normaliseContactEmail,
} from "@/lib/patientContacts";

function row(partial: Partial<EditableContact> & { label: string }): EditableContact {
  return {
    _uiKey: partial.id ?? partial.label,
    id: null,
    relationship: null,
    email: null,
    phone: null,
    ...partial,
  } as EditableContact;
}

describe("buildContactPayload", () => {
  // The regression this whole module exists for. The form used to build
  // {label, relationship, email, phone} and drop `id`; the backend
  // matches on `id`, so every save inserted a second row for every
  // contact while the delegated originals were held back from deletion.
  // One production fascicolo went from five contacts to eight, and
  // deleting a duplicate through the same form produced two more.
  it("carries the backend id of every persisted contact", () => {
    const payload = buildContactPayload([
      row({ id: "cb51bfca-1a5c-47b7-9870-5a4172041da3", label: "Angelo Leto" }),
      row({ id: "4dc11333-07a1-4278-ab7e-52fa3648e2b7", label: "Alfonso Leto" }),
    ]);
    expect(payload.map((c) => c.id)).toEqual([
      "cb51bfca-1a5c-47b7-9870-5a4172041da3",
      "4dc11333-07a1-4278-ab7e-52fa3648e2b7",
    ]);
  });

  it("sends null for a row the user just added", () => {
    const payload = buildContactPayload([row({ label: "Nuovo contatto" })]);
    expect(payload).toHaveLength(1);
    expect(payload[0].id).toBeNull();
  });

  it("never leaks the local React key", () => {
    const payload = buildContactPayload([row({ id: "abc", label: "X" })]);
    expect(payload[0]).not.toHaveProperty("_uiKey");
  });

  it("drops rows the user added and left unnamed", () => {
    const payload = buildContactPayload([
      row({ label: "Reale" }),
      row({ label: "   " }),
      row({ label: "" }),
    ]);
    expect(payload.map((c) => c.label)).toEqual(["Reale"]);
  });

  it("normalises blank fields to null and trims the rest", () => {
    const payload = buildContactPayload([
      row({
        label: "  Angelo Leto  ",
        relationship: "  figlio ",
        email: "   ",
        phone: "",
      }),
    ]);
    expect(payload[0]).toEqual({
      id: null,
      label: "Angelo Leto",
      relationship: "figlio",
      email: null,
      phone: null,
    });
  });
});

describe("normaliseContactEmail", () => {
  it("matches what the server stores: trimmed, lowercased, empty as null", () => {
    expect(normaliseContactEmail("  Angelo@Leto.Blue ")).toBe("angelo@leto.blue");
    expect(normaliseContactEmail("   ")).toBeNull();
    expect(normaliseContactEmail(null)).toBeNull();
    expect(normaliseContactEmail(undefined)).toBeNull();
  });
});

describe("findDuplicateEmail", () => {
  it("reports the address two rows share, ignoring case and padding", () => {
    expect(
      findDuplicateEmail([
        row({ label: "Angelo", email: "angelo@leto.blue" }),
        row({ label: "Angelo bis", email: " ANGELO@Leto.Blue " }),
      ]),
    ).toBe("angelo@leto.blue");
  });

  it("allows any number of contacts without an address", () => {
    expect(
      findDuplicateEmail([
        row({ label: "Nonna", phone: "+390000000" }),
        row({ label: "Vicino", phone: "+390000001" }),
      ]),
    ).toBeNull();
  });

  it("ignores unnamed rows, which are dropped before the request anyway", () => {
    expect(
      findDuplicateEmail([
        row({ label: "Angelo", email: "angelo@leto.blue" }),
        row({ label: "  ", email: "angelo@leto.blue" }),
      ]),
    ).toBeNull();
  });

  it("passes a clean list", () => {
    expect(
      findDuplicateEmail([
        row({ label: "Angelo", email: "angelo@leto.blue" }),
        row({ label: "Alfonso", email: "archivioalfonsoleto@gmail.com" }),
      ]),
    ).toBeNull();
  });
});
