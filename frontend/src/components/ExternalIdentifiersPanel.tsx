"use client";

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import { ApiError } from "@/lib/api";
import { type ExternalIdentifier, externalIdentifiersApi } from "@/lib/api_records";

interface Props {
  patientId: string;
  /** When true the panel renders the add/remove buttons. Default true. */
  editable?: boolean;
}

const COMMON_SYSTEM_DEFS: { labelKey: string; system: string; type: string }[] = [
  {
    labelKey: "labelCodiceFiscale",
    system: "urn:oid:2.16.840.1.113883.2.9.4.3.2",
    type: "fiscal-code",
  },
  {
    labelKey: "labelMrn",
    system: "https://example.org/patient-mrn",
    type: "MR",
  },
  {
    labelKey: "labelDicomPid",
    system: "DICOM:Issuer:UNKNOWN",
    type: "MR",
  },
  {
    labelKey: "labelLabId",
    system: "https://example.org/lab-id",
    type: "MR",
  },
];

export default function ExternalIdentifiersPanel({ patientId, editable = true }: Props) {
  const t = useTranslations("externalIds");
  const commonSystems = useMemo(
    () =>
      COMMON_SYSTEM_DEFS.map((def) => ({
        label: t(def.labelKey),
        system: def.system,
        type: def.type,
      })),
    [t],
  );

  const [items, setItems] = useState<ExternalIdentifier[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [pending, setPending] = useState<ExternalIdentifier>({
    system: COMMON_SYSTEM_DEFS[0].system,
    value: "",
    type: COMMON_SYSTEM_DEFS[0].type,
    assigner: "",
  });

  useEffect(() => {
    let cancelled = false;
    externalIdentifiersApi
      .list(patientId)
      .then((rows) => {
        if (!cancelled) setItems(rows);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : t("errorLoad"));
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, t]);

  async function add() {
    if (!pending.value.trim()) {
      setError(t("errorEmptyValue"));
      return;
    }
    setError(null);
    try {
      const next = await externalIdentifiersApi.add(patientId, {
        system: pending.system,
        value: pending.value.trim(),
        type: pending.type,
        assigner: pending.assigner?.trim() || null,
      });
      setItems(next);
      setPending({ ...pending, value: "" });
      setAdding(false);
    } catch (e: unknown) {
      setError(
        e instanceof ApiError
          ? `${e.status}: ${e.message}`
          : e instanceof Error
            ? e.message
            : t("errorGeneric"),
      );
    }
  }

  async function remove(it: ExternalIdentifier) {
    setError(null);
    try {
      const next = await externalIdentifiersApi.remove(patientId, it.system, it.value);
      setItems(next);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("errorGeneric"));
    }
  }

  return (
    <section className="external-identifiers-panel" aria-label={t("ariaLabel")}>
      <header style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
        <h3 style={{ margin: 0 }}>{t("title")}</h3>
        <small style={{ color: "var(--muted-fg, #666)" }}>{t("subtitle")}</small>
      </header>

      {error && (
        <p role="alert" style={{ color: "var(--error-fg, #c00)" }}>
          {error}
        </p>
      )}

      {items === null ? (
        <p>{t("loading")}</p>
      ) : items.length === 0 ? (
        <p style={{ color: "var(--muted-fg, #666)" }}>{t("empty")}</p>
      ) : (
        <table className="external-identifiers-table" style={{ width: "100%" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>{t("colType")}</th>
              <th style={{ textAlign: "left" }}>{t("colValue")}</th>
              <th style={{ textAlign: "left" }}>{t("colAssigner")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={`${it.system}|${it.value}`}>
                <td>
                  <strong>{it.type}</strong>
                  <br />
                  <small style={{ color: "var(--muted-fg, #666)" }}>{it.system}</small>
                </td>
                <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{it.value}</td>
                <td>{it.assigner ?? "—"}</td>
                <td>
                  {editable && (
                    <button
                      type="button"
                      onClick={() => void remove(it)}
                      aria-label={t("removeAriaLabel", {
                        type: it.type,
                        value: it.value,
                      })}
                    >
                      {t("remove")}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {editable && !adding && (
        <button type="button" onClick={() => setAdding(true)}>
          {t("addIdentifier")}
        </button>
      )}

      {editable && adding && (
        <div className="external-identifiers-add" style={{ marginTop: "1rem" }}>
          <label>
            {t("fieldSystem")}
            <select
              value={pending.system}
              onChange={(e) => {
                const opt = commonSystems.find((s) => s.system === e.target.value);
                setPending({
                  ...pending,
                  system: e.target.value,
                  type: opt?.type ?? pending.type,
                });
              }}
            >
              {commonSystems.map((s) => (
                <option key={s.system} value={s.system}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("fieldValue")}
            <input
              type="text"
              value={pending.value}
              onChange={(e) => setPending({ ...pending, value: e.target.value })}
              placeholder={t("valuePlaceholder")}
            />
          </label>
          <label>
            {t("fieldAssigner")}
            <input
              type="text"
              value={pending.assigner ?? ""}
              onChange={(e) => setPending({ ...pending, assigner: e.target.value })}
              placeholder={t("assignerPlaceholder")}
            />
          </label>
          <div>
            <button type="button" onClick={() => void add()}>
              {t("save")}
            </button>
            <button
              type="button"
              onClick={() => {
                setAdding(false);
                setError(null);
              }}
              style={{ marginLeft: "0.5rem" }}
            >
              {t("cancel")}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
