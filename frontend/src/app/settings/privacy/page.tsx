"use client";

// GDPR privacy settings — consent toggles, data export, erasure request.
// Matches the taxonomy in docs/security-gdpr.md §3.
// Auth gate is in ``settings/layout.tsx``; ``user`` is guaranteed set here.

import { useEffect, useState } from "react";

import { ApiError, type Consent, downloadJobResult, gdprApi } from "@/lib/api";
import { jobsStorage } from "@/lib/jobs";
import { useJob } from "@/lib/useJob";

const CONSENT_LABELS: Record<string, { title: string; desc: string; required?: boolean }> = {
  terms_of_service: {
    title: "Terms of Service",
    desc: "Required to use the platform.",
    required: true,
  },
  privacy_policy: {
    title: "Privacy Policy",
    desc: "Required to use the platform.",
    required: true,
  },
  marketing_email: {
    title: "Marketing email",
    desc: "Receive product announcements and newsletters.",
  },
  research_use: {
    title: "Research use",
    desc: "Allow my de-identified studies to be used for academic research.",
  },
  commercial_use: {
    title: "Commercial use",
    desc: "Allow my de-identified studies to be licensed commercially.",
  },
  ai_training: {
    title: "AI model training",
    desc: "Allow my de-identified studies to be used to train AI models.",
  },
  third_party_sharing: {
    title: "Third-party sharing",
    desc: "Allow the platform to share my data with partner organizations (never for advertising).",
  },
};

export default function PrivacySettingsPage() {
  const [consents, setConsents] = useState<Consent[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportJobId, setExportJobId] = useState<string | null>(null);
  const [exportDownloaded, setExportDownloaded] = useState(false);
  const { job: exportJob } = useJob(exportJobId);
  const [erasing, setErasing] = useState(false);
  const [erasureConfirm, setErasureConfirm] = useState(false);
  const [erasureReason, setErasureReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await gdprApi.listConsents();
        if (!cancelled) setConsents(rows);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggleConsent(kind: string, nextGranted: boolean) {
    setBusy(kind);
    setErr(null);
    try {
      const updated = await gdprApi.setConsent(kind, nextGranted);
      setConsents((prev) => (prev ?? []).map((c) => (c.kind === kind ? updated : c)));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "update failed");
    } finally {
      setBusy(null);
    }
  }

  async function onExport() {
    setExporting(true);
    setErr(null);
    setNotice(null);
    setExportDownloaded(false);
    try {
      const created = await gdprApi.requestExport();
      jobsStorage.add({ id: created.id, kind: created.kind });
      setExportJobId(created.id);
      setNotice("Export queued; the download will start once the bundle is ready.");
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        setErr("Too many active jobs. Wait for some to finish and retry.");
      } else {
        setErr(e instanceof ApiError ? e.message : "export failed");
      }
    } finally {
      setExporting(false);
    }
  }

  // Once the GDPR export job succeeds, hand the freshly-signed URL to
  // an anchor and remember we downloaded it so polling re-renders do
  // not start the download again. Goes through the signed-token
  // anchor path so big GDPR ZIPs stream straight to disk and the
  // bare-href approach (which 401'd in prod under the global auth
  // gate) stays gone.
  useEffect(() => {
    if (!exportJob || exportDownloaded) return;
    if (exportJob.status === "succeeded" && exportJob.result_download_url) {
      let cancelled = false;
      void downloadJobResult(exportJob.id).then(() => {
        if (cancelled) return;
        setExportDownloaded(true);
        setNotice("Export downloaded.");
      });
      return () => {
        cancelled = true;
      };
    }
    if (exportJob.status === "failed") {
      setErr(exportJob.error?.message ?? "export failed");
    } else if (exportJob.status === "cancelled") {
      setNotice("Export cancelled.");
    }
  }, [exportJob, exportDownloaded]);

  async function onRequestErasure() {
    if (!erasureConfirm) return;
    setErasing(true);
    setErr(null);
    try {
      const req = await gdprApi.requestErasure("self", erasureReason || null);
      setNotice(
        req.status === "completed"
          ? "Your account has been erased. You will be logged out."
          : `Erasure request submitted (id: ${req.id}, status: ${req.status}).`,
      );
      setErasureConfirm(false);
      setErasureReason("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "erasure failed");
    } finally {
      setErasing(false);
    }
  }

  if (consents === null && err === null) {
    return (
      <main>
        <p className="meta">Loading...</p>
      </main>
    );
  }

  return (
    <main>
      <h1>Privacy &amp; GDPR</h1>
      <p className="meta">
        Manage your consents, download a copy of your personal data (Art. 20), or request erasure of
        your account (Art. 17).
      </p>

      {err && <p className="error">{err}</p>}
      {notice && <p className="meta">{notice}</p>}

      <section style={{ marginTop: "2rem" }}>
        <h2>Consents</h2>
        {!consents && <p className="meta">Loading consents...</p>}
        {consents?.map((c) => {
          const meta = CONSENT_LABELS[c.kind] ?? {
            title: c.kind,
            desc: "",
          };
          return (
            <div
              key={c.kind}
              style={{
                border: "1px solid var(--border)",
                padding: "0.75rem 1rem",
                borderRadius: 6,
                marginBottom: "0.5rem",
                display: "flex",
                alignItems: "flex-start",
                gap: "1rem",
              }}
            >
              <div style={{ flex: 1 }}>
                <strong>{meta.title}</strong>
                {meta.required ? (
                  <span className="meta" style={{ marginLeft: "0.5rem" }}>
                    (required)
                  </span>
                ) : null}
                <div className="meta">{meta.desc}</div>
                {c.granted_at && (
                  <div className="meta" style={{ fontSize: "0.8em" }}>
                    Granted: {new Date(c.granted_at).toLocaleString()}
                  </div>
                )}
                {c.revoked_at && (
                  <div className="meta" style={{ fontSize: "0.8em" }}>
                    Revoked: {new Date(c.revoked_at).toLocaleString()}
                  </div>
                )}
              </div>
              <label style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                <input
                  type="checkbox"
                  checked={c.granted}
                  disabled={busy === c.kind || meta.required === true}
                  onChange={(e) => toggleConsent(c.kind, e.target.checked)}
                />
                {c.granted ? "Granted" : "Not granted"}
              </label>
            </div>
          );
        })}
      </section>

      <section style={{ marginTop: "2rem" }}>
        <h2>Data export</h2>
        <p className="meta">
          Download a ZIP archive of all personal data the platform holds about you (manifest only —
          raw DICOM pixels are available through the per-study download endpoint).
        </p>
        <button
          type="button"
          onClick={onExport}
          disabled={exporting || exportJob?.status === "queued" || exportJob?.status === "running"}
        >
          {exporting
            ? "Starting..."
            : exportJob?.status === "queued"
              ? "Queued..."
              : exportJob?.status === "running"
                ? `Building ZIP${exportJob.stage ? ` (${exportJob.stage})` : ""}...`
                : "Download my data (.zip)"}
        </button>
        {exportJob?.status === "succeeded" && exportJob.result_download_url && (
          <p className="meta" style={{ marginTop: "0.5rem" }}>
            <button
              type="button"
              onClick={() => {
                if (exportJob?.id) void downloadJobResult(exportJob.id);
              }}
              style={{
                background: "none",
                border: "none",
                padding: 0,
                color: "var(--bv-link, #1a73e8)",
                textDecoration: "underline",
                cursor: "pointer",
                font: "inherit",
              }}
            >
              Download again
            </button>
          </p>
        )}
      </section>

      <section style={{ marginTop: "2rem" }}>
        <h2>Right to erasure</h2>
        <p className="meta">
          Request permanent deletion of your account. Public studies you previously published stay
          online (the data was released under a public-domain-equivalent grant) but ownership is
          transferred to an anonymous subject. Audit log entries about your actions are retained in
          a redacted form as required by regulation.
        </p>

        {!erasureConfirm ? (
          <button
            type="button"
            onClick={() => setErasureConfirm(true)}
            style={{ background: "#a33" }}
          >
            Request account erasure...
          </button>
        ) : (
          <div
            style={{
              border: "1px solid #a33",
              padding: "1rem",
              borderRadius: 6,
              marginTop: "0.5rem",
            }}
          >
            <p>
              <strong>This action cannot be undone.</strong> Your email, name, and authentication
              credentials will be anonymised. Private studies with no active shares will be deleted.
            </p>
            <label>
              Reason (optional, for our records)
              <textarea
                rows={3}
                value={erasureReason}
                onChange={(e) => setErasureReason(e.target.value)}
                style={{ width: "100%", marginTop: "0.25rem" }}
              />
            </label>
            <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.75rem" }}>
              <button
                type="button"
                onClick={onRequestErasure}
                disabled={erasing}
                style={{ background: "#a33" }}
              >
                {erasing ? "Processing..." : "Confirm: erase my account"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setErasureConfirm(false);
                  setErasureReason("");
                }}
                disabled={erasing}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}
