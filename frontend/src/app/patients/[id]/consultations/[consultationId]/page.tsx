"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import BackToFolderLink from "@/components/BackToFolderLink";
import ConflictResolver from "@/components/ConflictResolver";
import { ConsultationBadges } from "@/components/ConsultationCard";
import ConsultationCitationList from "@/components/ConsultationCitationList";
import EvidenceContent from "@/components/EvidenceContent";
import {
  ApiError,
  type ConsultationDetail,
  type ProposalOut,
  consultationsApi,
  patientsApi,
  proposalsApi,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

type Tab = "summary" | "findings" | "recommendations" | "citations";

export default function ConsultationDetailPage() {
  const params = useParams<{ id: string; consultationId: string }>();
  const { user } = useAuth();
  const tA = useTranslations("actions");
  const tC = useTranslations("consultationDetail");
  const tCp = useTranslations("consultationDetailPage");

  const [data, setData] = useState<ConsultationDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("summary");
  const [isOwner, setIsOwner] = useState(false);

  const [showSign, setShowSign] = useState(false);
  const [showReject, setShowReject] = useState(false);
  const [signNote, setSignNote] = useState("");
  const [rejectReason, setRejectReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [proposal, setProposal] = useState<ProposalOut | null>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await consultationsApi.detail(params.consultationId);
      setData(d);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, [params.consultationId]);

  // Look up the proposal tied to this consultation, if any. F12.1 maps
  // a consultation 1:1 with a proposal once it transitions to submitted.
  useEffect(() => {
    let cancelled = false;
    if (!data) return;
    proposalsApi
      .list(params.id)
      .then((all) => {
        if (cancelled) return;
        const match = all.find((p) => p.consultation_id === params.consultationId);
        setProposal(match ?? null);
      })
      .catch(() => {
        if (!cancelled) setProposal(null);
      });
    return () => {
      cancelled = true;
    };
  }, [data, params.id, params.consultationId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    let cancelled = false;
    if (!user) {
      setIsOwner(false);
      return;
    }
    patientsApi
      .detail(params.id)
      .then((p) => {
        if (cancelled) return;
        setIsOwner(
          user.is_admin ||
            user.subject_id === p.managed_by_subject_id ||
            user.subject_id === p.self_user_subject_id,
        );
      })
      .catch(() => !cancelled && setIsOwner(false));
    return () => {
      cancelled = true;
    };
  }, [params.id, user]);

  async function handleSign() {
    setBusy(true);
    setActionErr(null);
    try {
      await consultationsApi.sign(params.consultationId, signNote || null);
      setShowSign(false);
      setSignNote("");
      await refresh();
    } catch (e) {
      setActionErr(e instanceof ApiError ? e.message : "sign failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleReject() {
    if (!rejectReason.trim()) {
      setActionErr("Indica un motivo");
      return;
    }
    setBusy(true);
    setActionErr(null);
    try {
      await consultationsApi.reject(params.consultationId, rejectReason);
      setShowReject(false);
      setRejectReason("");
      await refresh();
    } catch (e) {
      setActionErr(e instanceof ApiError ? e.message : "reject failed");
    } finally {
      setBusy(false);
    }
  }

  if (err)
    return (
      <main>
        <p className="error">{err}</p>
      </main>
    );
  if (!data)
    return (
      <main>
        <p className="meta">Loading...</p>
      </main>
    );

  const canAct = isOwner && data.status === "submitted";

  return (
    <main>
      <p className="meta">
        <BackToFolderLink
          patientId={params.id}
          patientName=""
          itemKind="consultation"
          itemId={params.consultationId}
          rootLabel={tC("consultationsLabel")}
        />
        {" · "}
        <Link href={`/patients/${params.id}/consultations`} style={{ color: "var(--bv-muted)" }}>
          {tC("consultsAi")}
        </Link>
      </p>
      <h1>{data.title}</h1>
      <div style={{ marginBottom: "0.5rem" }}>
        <ConsultationBadges c={data} />
      </div>
      <p className="meta" style={{ fontSize: "0.8rem" }}>
        Creato {new Date(data.created_at).toLocaleString()}
        {data.signed_at && ` · firmato ${new Date(data.signed_at).toLocaleString()}`}
        {data.rejected_at && ` · rifiutato ${new Date(data.rejected_at).toLocaleString()}`}
      </p>

      <div
        style={{
          display: "flex",
          gap: "0.2rem",
          borderBottom: "1px solid var(--color-border, #d1d5db)",
          marginTop: "1rem",
        }}
      >
        <TabButton label="Sommario" tab="summary" active={tab} onClick={setTab} />
        <TabButton label="Findings" tab="findings" active={tab} onClick={setTab} />
        <TabButton label="Raccomandazioni" tab="recommendations" active={tab} onClick={setTab} />
        <TabButton
          label={`Citazioni (${data.citations.length})`}
          tab="citations"
          active={tab}
          onClick={setTab}
        />
      </div>

      <div style={{ padding: "1rem 0" }}>
        {tab === "summary" && (
          <EvidenceContent
            patientId={params.id}
            body={data.summary_md ?? ""}
            ctx={`evidence:consultation:${data.id}`}
          />
        )}
        {tab === "findings" && (
          <EvidenceContent
            patientId={params.id}
            body={data.findings_md ?? ""}
            ctx={`evidence:consultation:${data.id}`}
          />
        )}
        {tab === "recommendations" && (
          <EvidenceContent
            patientId={params.id}
            body={data.recommendations_md ?? ""}
            ctx={`evidence:consultation:${data.id}`}
          />
        )}
        {tab === "citations" && <ConsultationCitationList citations={data.citations} />}
      </div>

      {canAct && (
        <div
          className="card"
          style={{
            marginTop: "1rem",
            display: "flex",
            gap: "0.5rem",
            justifyContent: "flex-end",
          }}
        >
          <button type="button" className="ghost" onClick={() => setShowReject(true)}>
            {tCp("reject")}
          </button>
          <button type="button" onClick={() => setShowSign(true)}>
            {tCp("sign")}
          </button>
        </div>
      )}

      {data.status === "signed" && data.sign_note && (
        <div className="card" style={{ marginTop: "1rem" }}>
          <strong>{tCp("signNoteLabel")}</strong>
          <p style={{ margin: "0.3rem 0 0" }}>{data.sign_note}</p>
        </div>
      )}

      {proposal && proposal.status === "open" && isOwner && (
        <div className="card" style={{ marginTop: "1rem" }}>
          <h3 style={{ marginTop: 0 }}>{tCp("proposalChanges")}</h3>
          <p className="meta" style={{ fontSize: "0.78rem", marginBottom: "0.6rem" }}>
            {tCp("proposalIntro")}
          </p>
          <ConflictResolver proposalId={proposal.id} onMerged={refresh} onWithdrawn={refresh} />
        </div>
      )}
      {data.status === "rejected" && data.reject_reason && (
        <div className="card" style={{ marginTop: "1rem" }}>
          <strong>{tC("rejectReasonHeader")}:</strong>
          <p style={{ margin: "0.3rem 0 0" }}>{data.reject_reason}</p>
        </div>
      )}

      {showSign && (
        <Dialog
          title={tC("signTitle")}
          onClose={() => {
            setShowSign(false);
            setActionErr(null);
          }}
        >
          {actionErr && <p className="error">{actionErr}</p>}
          <label style={{ display: "block" }}>
            <span className="meta">{tC("signNoteOptional")}</span>
            <textarea
              value={signNote}
              onChange={(e) => setSignNote(e.target.value)}
              rows={4}
              style={{ width: "100%" }}
            />
          </label>
          <div
            style={{
              marginTop: "0.75rem",
              display: "flex",
              gap: "0.5rem",
              justifyContent: "flex-end",
            }}
          >
            <button
              type="button"
              className="ghost"
              onClick={() => setShowSign(false)}
              disabled={busy}
            >
              {tA("cancel")}
            </button>
            <button type="button" onClick={handleSign} disabled={busy}>
              {busy ? tA("saveBusy") : tC("signSubmit")}
            </button>
          </div>
        </Dialog>
      )}

      {showReject && (
        <Dialog
          title={tC("rejectTitle")}
          onClose={() => {
            setShowReject(false);
            setActionErr(null);
          }}
        >
          {actionErr && <p className="error">{actionErr}</p>}
          <label style={{ display: "block" }}>
            <span className="meta">{tC("rejectReason")}</span>
            <textarea
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              rows={4}
              required
              style={{ width: "100%" }}
            />
          </label>
          <div
            style={{
              marginTop: "0.75rem",
              display: "flex",
              gap: "0.5rem",
              justifyContent: "flex-end",
            }}
          >
            <button
              type="button"
              className="ghost"
              onClick={() => setShowReject(false)}
              disabled={busy}
            >
              {tA("cancel")}
            </button>
            <button type="button" onClick={handleReject} disabled={busy}>
              {busy ? tA("saveBusy") : tC("rejectSubmit")}
            </button>
          </div>
        </Dialog>
      )}
    </main>
  );
}

function TabButton({
  label,
  tab,
  active,
  onClick,
}: {
  label: string;
  tab: Tab;
  active: Tab;
  onClick: (t: Tab) => void;
}) {
  const isActive = tab === active;
  return (
    <button
      type="button"
      onClick={() => onClick(tab)}
      style={{
        // The global ``button`` rule sets background:var(--bv-accent)
        // and color:#fff which is fine for primary CTAs but not for
        // a tab strip. Without explicit overrides the tab labels
        // render white-on-page-bg (illegible in light mode).
        padding: "0.5rem 0.9rem",
        border: "none",
        background: "transparent",
        color: isActive ? "var(--bv-accent)" : "var(--bv-fg)",
        borderBottom: isActive ? "2px solid var(--bv-accent)" : "2px solid transparent",
        fontWeight: isActive ? 600 : 400,
        cursor: "pointer",
        fontSize: "0.9rem",
      }}
    >
      {label}
    </button>
  );
}

function Dialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const tCp = useTranslations("consultationDetailPage");
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.4)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
      }}
    >
      <button
        type="button"
        aria-label={tCp("closeAriaLabel")}
        onClick={onClose}
        style={{
          position: "absolute",
          inset: 0,
          background: "transparent",
          border: "none",
          cursor: "default",
        }}
      />
      <div
        // biome-ignore lint/a11y/useSemanticElements: legacy fake-modal in this consultations page; nested inside a custom backdrop wrapper. Native <dialog> would conflict with the existing structure.
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="card"
        style={{
          position: "relative",
          minWidth: 320,
          maxWidth: 520,
          width: "90%",
          background: "var(--color-surface, #fff)",
        }}
      >
        <h2 style={{ marginTop: 0 }}>{title}</h2>
        {children}
      </div>
    </div>
  );
}
