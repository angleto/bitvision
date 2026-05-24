"use client";

// /transparency — public aggregate platform stats (F11.2).
// Calls GET /api/transparency (no auth) and renders a plain tabular view.
// No per-user or per-study data is ever displayed here.

import { useEffect, useState } from "react";

import { ApiError, type TransparencyOut, transparencyApi } from "@/lib/api";

const TIER_LABELS: Record<"t1" | "t2" | "t3" | "t4", string> = {
  t1: "T1 — private",
  t2: "T2 — shared (controlled)",
  t3: "T3 — training opt-in (anonymised)",
  t4: "T4 — public (CC)",
};

export default function TransparencyPage() {
  const [data, setData] = useState<TransparencyOut | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await transparencyApi.get();
        if (!cancelled) setData(body);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main>
      <h1>Transparency</h1>
      <p className="meta" style={{ marginBottom: "1.5rem" }}>
        Public aggregate stats about the platform. No per-user or per-study data is exposed here;
        the numbers update live from the API.
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

          <section className="card">
            <h3>Community</h3>
            <p>
              <strong>{data.users.total}</strong> registered user
              {data.users.total === 1 ? "" : "s"}.
            </p>
          </section>

          <section className="card">
            <h3>Sharing</h3>
            <table style={{ width: "100%", fontSize: "0.9rem" }}>
              <tbody>
                <tr>
                  <td>Active grants</td>
                  <td style={{ textAlign: "right" }}>{data.sharing.grants_active}</td>
                </tr>
                <tr>
                  <td>De-identified on read</td>
                  <td style={{ textAlign: "right" }}>{data.sharing.grants_deidentified}</td>
                </tr>
                <tr>
                  <td>Commercial (irrevocable)</td>
                  <td style={{ textAlign: "right" }}>{data.sharing.grants_commercial}</td>
                </tr>
              </tbody>
            </table>
          </section>

          <section className="card">
            <h3>LLM activity</h3>
            <table style={{ width: "100%", fontSize: "0.9rem" }}>
              <tbody>
                <tr>
                  <td>Consultations</td>
                  <td style={{ textAlign: "right" }}>{data.llm.consultations_total}</td>
                </tr>
                <tr>
                  <td>Summaries generated</td>
                  <td style={{ textAlign: "right" }}>{data.llm.summaries_total}</td>
                </tr>
              </tbody>
            </table>
          </section>

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
