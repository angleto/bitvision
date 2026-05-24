"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";

import { useModal } from "@/components/ModalHost";
import SendIcon from "@/components/icons/SendIcon";
import { ApiError, type Paginated, type Patient, type PatientScope, patientsApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

// Map each scope to the i18n keys for its label + tooltip. Resolved
// once at render time via ``useTranslations("patientsList")`` so the
// scope tab bar follows the active locale.
const SCOPES: ReadonlyArray<
  readonly [PatientScope, "Personal" | "Mine" | "Shared" | "Public" | "All"]
> = [
  ["personal", "Personal"],
  ["mine", "Mine"],
  ["shared", "Shared"],
  ["public", "Public"],
  ["all", "All"],
];

const DEFAULT_SCOPE: PatientScope = "personal";

function isPatientScope(value: string | null): value is PatientScope {
  return (
    value === "personal" ||
    value === "mine" ||
    value === "shared" ||
    value === "public" ||
    value === "all"
  );
}

export default function PatientsPage() {
  return (
    <Suspense
      fallback={
        <main>
          <p className="meta">Loading...</p>
        </main>
      }
    >
      <PatientsList />
    </Suspense>
  );
}

function PatientsList() {
  const params = useSearchParams();
  const router = useRouter();
  const tPatients = useTranslations("patientsList");
  const q = params.get("q") ?? "";
  const tag = params.get("tag") ?? "";
  const rawScope = params.get("scope");
  const scope: PatientScope = isPatientScope(rawScope) ? rawScope : DEFAULT_SCOPE;
  const { user } = useAuth();
  const modal = useModal();
  const [data, setData] = useState<Paginated<Patient> | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const resp = await patientsApi.list({
        q: q || undefined,
        scope,
        tag: tag || undefined,
      });
      setData(resp);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, [q, scope, tag]);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setErr(null);
    const run = async () => {
      try {
        const resp = await patientsApi.list({
          q: q || undefined,
          scope,
          tag: tag || undefined,
        });
        if (!cancelled) setData(resp);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [q, scope, tag]);

  const clearTag = useCallback(() => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (scope !== DEFAULT_SCOPE) params.set("scope", scope);
    const qs = params.toString();
    router.replace(qs ? `/patients?${qs}` : "/patients");
  }, [router, q, scope]);

  const handleDelete = useCallback(
    async (patient: Patient) => {
      const ok = await modal.confirm({
        title: tPatients("deleteTitle"),
        message: tPatients("deleteMessage", { name: patient.display_name }),
        confirmLabel: tPatients("deleteConfirm"),
        destructive: true,
      });
      if (!ok) return;
      try {
        await patientsApi.remove(patient.id);
        await reload();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "delete failed");
      }
    },
    [modal, reload, tPatients],
  );

  const setScope = useCallback(
    (next: PatientScope) => {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      // Default scope stays out of the URL so /patients without query
      // string keeps a clean canonical form.
      if (next !== DEFAULT_SCOPE) params.set("scope", next);
      if (tag) params.set("tag", tag);
      const qs = params.toString();
      router.replace(qs ? `/patients?${qs}` : "/patients");
    },
    [router, q, tag],
  );

  return (
    <main>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: "1rem",
        }}
      >
        <h1>
          Patients
          {q ? (
            <span className="meta" style={{ marginLeft: "0.5rem" }}>
              · search &quot;{q}&quot;
            </span>
          ) : null}
        </h1>
        <Link href="/patients/new">
          <button type="button">+ New patient</button>
        </Link>
      </div>

      <ScopeBar scope={scope} onChange={setScope} />

      {tag && (
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.5rem",
            marginBottom: "1rem",
            padding: "0.3rem 0.7rem",
            border: "1px solid var(--bv-card-border, #e5e7eb)",
            borderRadius: 999,
            background: "#fff7ef",
            fontSize: "0.85rem",
          }}
        >
          <span className="meta" style={{ fontSize: "0.75rem" }}>
            tag:
          </span>
          <strong>{tag}</strong>
          <button
            type="button"
            className="ghost"
            onClick={clearTag}
            title={tPatients("tagFilterRemove")}
            aria-label={tPatients("tagFilterRemove")}
            style={{
              padding: "0 0.35rem",
              border: "none",
              fontSize: "0.95rem",
              lineHeight: 1,
              color: "var(--bv-fg-soft, #475569)",
            }}
          >
            ×
          </button>
        </div>
      )}

      {err && <p className="error">{err}</p>}
      {!data && !err && <p className="meta">Loading...</p>}

      {data?.items.length === 0 && (
        <p className="meta">
          {scope === DEFAULT_SCOPE ? (
            <>
              No patients in this view.{" "}
              <button
                type="button"
                className="ghost"
                onClick={() => setScope("public")}
                style={{ padding: "0.1rem 0.5rem", fontSize: "0.85rem" }}
              >
                Browse public datasets
              </button>{" "}
              or <Link href="/patients/new">create a new Health Record</Link>.
            </>
          ) : (
            <>
              No patients found. <Link href="/patients">Reset filters</Link>.
            </>
          )}
        </p>
      )}

      {data?.items.map((p) => {
        const canDelete =
          !!user &&
          (user.is_admin ||
            user.subject_id === p.managed_by_subject_id ||
            user.subject_id === p.self_user_subject_id);
        return (
          <PatientCard
            key={p.id}
            patient={p}
            // Show the origin badge only when the current view mixes
            // multiple origins; otherwise the badge is just noise.
            showOrigin={scope === "personal" || scope === "all"}
            onDelete={canDelete ? () => handleDelete(p) : undefined}
          />
        );
      })}

      {data && data.total > data.items.length ? (
        <p className="meta">
          Showing {data.items.length} of {data.total}.
        </p>
      ) : null}
    </main>
  );
}

function ScopeBar({
  scope,
  onChange,
}: {
  scope: PatientScope;
  onChange: (next: PatientScope) => void;
}) {
  const tPatients = useTranslations("patientsList");
  return (
    <div
      role="tablist"
      aria-label="Patient scope"
      style={{
        display: "inline-flex",
        gap: 4,
        margin: "0.75rem 0 1rem",
        padding: 3,
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        borderRadius: 999,
        background: "var(--bv-card-bg, #fff)",
        flexWrap: "wrap",
      }}
    >
      {SCOPES.map(([value, suffix]) => {
        const active = value === scope;
        return (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={active}
            title={tPatients(`scope${suffix}Title`)}
            onClick={() => onChange(value)}
            style={{
              fontSize: "0.82rem",
              padding: "0.25rem 0.75rem",
              border: "1px solid transparent",
              borderRadius: 999,
              background: active ? "#e96b1f" : "transparent",
              color: active ? "#fff" : "inherit",
              cursor: "pointer",
            }}
          >
            {tPatients(`scope${suffix}Label`)}
          </button>
        );
      })}
    </div>
  );
}

function PatientCard({
  patient,
  showOrigin,
  onDelete,
}: {
  patient: Patient;
  showOrigin: boolean;
  onDelete?: () => void;
}) {
  const tPatients = useTranslations("patientsList");
  const router = useRouter();
  const showShare = patient.origin === "mine";
  const shareRight = onDelete ? 44 : 8;
  const cardPaddingRight = (showShare ? 96 : 0) + (onDelete ? 36 : 0);
  return (
    <div style={{ position: "relative" }}>
      <Link
        href={`/patients/${patient.id}`}
        className="card"
        style={{
          display: "block",
          color: "inherit",
          paddingRight: cardPaddingRight || undefined,
        }}
      >
        <h3>
          {patient.display_name}
          <span className="badges">
            {showOrigin && patient.origin && <OriginBadge origin={patient.origin} />}
            {patient.sex && <span className="badge">{patient.sex}</span>}
            {patient.blood_type && <span className="badge">{patient.blood_type}</span>}
          </span>
        </h3>
        <div className="meta">
          {patient.birth_date ?? "birth date unknown"}
          {patient.tax_id ? ` · CF ${patient.tax_id}` : ""}
          {patient.external_id ? ` · ID ${patient.external_id}` : ""}
        </div>
      </Link>
      {showShare && (
        <button
          type="button"
          className="ghost"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            router.push(`/patients/${patient.id}?dialog=share`);
          }}
          title={tPatients("sharePatient")}
          aria-label={tPatients("sharePatient")}
          style={{
            position: "absolute",
            top: 8,
            right: shareRight,
            height: 28,
            padding: "0 10px",
            borderRadius: 6,
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: "0.78rem",
            fontWeight: 500,
            color: "var(--bv-brand, #e96b1f)",
            border: "1px solid var(--bv-card-border, #e5e7eb)",
            background: "var(--bv-card-bg, #fff)",
            cursor: "pointer",
          }}
        >
          <SendIcon size={13} />
          <span>{tPatients("shareButtonLabel")}</span>
        </button>
      )}
      {onDelete && (
        <button
          type="button"
          className="ghost"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onDelete();
          }}
          title={tPatients("deletePatient")}
          aria-label={tPatients("deletePatient")}
          style={{
            position: "absolute",
            top: 8,
            right: 8,
            width: 28,
            height: 28,
            padding: 0,
            borderRadius: 6,
            color: "var(--bv-danger, #b42318)",
            fontSize: "1rem",
            lineHeight: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          ×
        </button>
      )}
    </div>
  );
}

function OriginBadge({ origin }: { origin: "mine" | "shared" | "public" }) {
  const cfg: Record<typeof origin, { label: string; bg: string; fg: string }> = {
    mine: { label: "mine", bg: "#e0f2fe", fg: "#075985" },
    shared: { label: "shared", bg: "#fef3c7", fg: "#854d0e" },
    public: { label: "public", bg: "#dcfce7", fg: "#166534" },
  };
  const { label, bg, fg } = cfg[origin];
  return (
    <span
      className="badge"
      style={{
        background: bg,
        color: fg,
        fontSize: "0.7rem",
        textTransform: "uppercase",
        letterSpacing: "0.04em",
      }}
    >
      {label}
    </span>
  );
}
