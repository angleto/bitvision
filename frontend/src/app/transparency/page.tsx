"use client";

// /transparency — platform aggregate stats (F11.2).
//
// Two slices, gated by role:
//   - Public (anonymous OK): Studies + Governance, from GET
//     /api/transparency. No per-user or per-study data.
//   - Admin only: Community, Sharing, and LLM activity, from GET
//     /api/transparency/admin (require_admin on the backend). These
//     reveal community size and platform activity we don't publish.
//
// The gate is enforced server-side: a non-admin browser never receives
// the admin counts (the public endpoint doesn't even compute them).
// The client just picks which endpoint to call from the session's
// is_admin flag, so the admin cards only render for admins.

import { useEffect, useState } from "react";

import {
  ApiError,
  type TransparencyOut,
  type TransparencyPublicOut,
  transparencyApi,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const TIER_LABELS: Record<"t1" | "t2" | "t3" | "t4", string> = {
  t1: "T1 — private",
  t2: "T2 — shared (controlled)",
  t3: "T3 — training opt-in (anonymised)",
  t4: "T4 — public (CC)",
};

// Small inline marker so an admin can tell at a glance which cards are
// not part of the public page. Matches the page's plain-English style.
function AdminOnlyBadge() {
  return (
    <span
      className="meta"
      style={{ marginLeft: "0.5rem", fontSize: "0.75rem", fontWeight: 400 }}
      title="Visible to admins only — not shown on the public transparency page."
    >
      (admin only)
    </span>
  );
}

export default function TransparencyPage() {
  const { user, status } = useAuth();
  const [data, setData] = useState<TransparencyPublicOut | TransparencyOut | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const isAdmin = status === "ready" && !!user?.is_admin;

  useEffect(() => {
    // Wait until the auth state has resolved before fetching: that
    // decides which endpoint to hit, so we avoid fetching the public
    // slice and then re-fetching the admin superset on the same load.
    if (status === "loading") return;

    let cancelled = false;
    // Drop any previously-loaded payload on an auth/role transition so a
    // logout never briefly shows the prior (admin) numbers while the
    // refetch is in flight.
    setData(null);
    setErr(null);
    (async () => {
      try {
        let body: TransparencyPublicOut | TransparencyOut;
        if (isAdmin) {
          try {
            body = await transparencyApi.getAdmin();
          } catch {
            // An admin whose elevated call fails (privilege revoked,
            // agent-token session, or a transient error) must still get
            // the public slice rather than a blank page. The public
            // endpoint stays the floor for everyone.
            if (cancelled) return;
            body = await transparencyApi.get();
          }
        } else {
          body = await transparencyApi.get();
        }
        if (!cancelled) {
          setData(body);
          setErr(null);
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [status, isAdmin]);

  // Narrow to the admin superset only when we actually loaded it. The
  // `"users" in data` guard keeps the admin cards from rendering off a
  // public payload that lacks those fields.
  const adminData = isAdmin && data && "users" in data ? (data as TransparencyOut) : null;

  return (
    <main>
      <h1>Transparency</h1>
      <p className="meta" style={{ marginBottom: "1.5rem" }}>
        Public aggregate stats about the platform. No per-user or per-study data is exposed here;
        the numbers update live from the API.
        {isAdmin && " As an admin you also see operational counts marked (admin only)."}
      </p>

      {err && <p className="error">{err}</p>}
      {!data && !err && <p className="meta">Loading…</p>}

      {data && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
            gap: "1rem",
          }}
        >
          <section className="card">
            <h3>Studies</h3>
            <p>
              <strong>{data.studies.total}</strong> total, {data.studies.public} public.
            </p>
            <h4 style={{ marginTop: "1rem" }}>By contribution tier</h4>
            <table style={{ width: "100%", fontSize: "0.9rem" }}>
              <tbody>
                {(Object.keys(data.studies.by_tier) as Array<keyof typeof TIER_LABELS>).map(
                  (tier) => (
                    <tr key={tier}>
                      <td>{TIER_LABELS[tier]}</td>
                      <td style={{ textAlign: "right" }}>{data.studies.by_tier[tier]}</td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
            <h4 style={{ marginTop: "1rem" }}>Modality split (T3 + T4)</h4>
            {Object.keys(data.studies.by_modality).length === 0 ? (
              <p className="meta">No public / training studies yet.</p>
            ) : (
              <table style={{ width: "100%", fontSize: "0.9rem" }}>
                <tbody>
                  {Object.entries(data.studies.by_modality).map(([modality, n]) => (
                    <tr key={modality}>
                      <td>{modality}</td>
                      <td style={{ textAlign: "right" }}>{n}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {adminData && (
            <section className="card">
              <h3>
                Community
                <AdminOnlyBadge />
              </h3>
              <p>
                <strong>{adminData.users.total}</strong> registered user
                {adminData.users.total === 1 ? "" : "s"}.
              </p>
            </section>
          )}

          {adminData && (
            <section className="card">
              <h3>
                Sharing
                <AdminOnlyBadge />
              </h3>
              <table style={{ width: "100%", fontSize: "0.9rem" }}>
                <tbody>
                  <tr>
                    <td>Active grants</td>
                    <td style={{ textAlign: "right" }}>{adminData.sharing.grants_active}</td>
                  </tr>
                  <tr>
                    <td>De-identified on read</td>
                    <td style={{ textAlign: "right" }}>{adminData.sharing.grants_deidentified}</td>
                  </tr>
                  <tr>
                    <td>Commercial (irrevocable)</td>
                    <td style={{ textAlign: "right" }}>{adminData.sharing.grants_commercial}</td>
                  </tr>
                </tbody>
              </table>
            </section>
          )}

          {adminData && (
            <section className="card">
              <h3>
                LLM activity
                <AdminOnlyBadge />
              </h3>
              <table style={{ width: "100%", fontSize: "0.9rem" }}>
                <tbody>
                  <tr>
                    <td>Consultations</td>
                    <td style={{ textAlign: "right" }}>{adminData.llm.consultations_total}</td>
                  </tr>
                  <tr>
                    <td>Summaries generated</td>
                    <td style={{ textAlign: "right" }}>{adminData.llm.summaries_total}</td>
                  </tr>
                </tbody>
              </table>
            </section>
          )}

          <section className="card" style={{ gridColumn: "1 / -1" }}>
            <h3>Governance</h3>
            <ul style={{ margin: 0 }}>
              <li>
                License: <strong>{data.governance.license}</strong>
              </li>
            </ul>
          </section>
        </div>
      )}

      {data && (
        <p className="meta" style={{ marginTop: "1.5rem" }}>
          Generated at {data.generated_at} (API v{data.version}).
        </p>
      )}
    </main>
  );
}
