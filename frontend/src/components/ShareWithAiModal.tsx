"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import {
  type AiAssistant,
  ApiError,
  type AssistantSharedPatient,
  aiAssistantsApi,
} from "@/lib/api";

interface Props {
  patientId: string;
  open: boolean;
  onClose: () => void;
}

/**
 * Modal triggered from the Health Record. Lets the user toggle which
 * of their AI assistants this patient is shared with. Each row is a
 * checkbox: ticking it shares, un-ticking it un-shares — both calls go
 * through the assistant-scoped endpoints, so the share list stays the
 * source of truth.
 */
export default function ShareWithAiModal({ patientId, open, onClose }: Props) {
  const t = useTranslations("aiShare");
  const [assistants, setAssistants] = useState<AiAssistant[] | null>(null);
  // Per-assistant: whether this patient is shared with it.
  const [shared, setShared] = useState<Record<string, boolean>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const list = await aiAssistantsApi.list();
      setAssistants(list);
      // Hydrate the "shared" bitmap by listing each assistant's patients
      // and checking membership. The N is small (assistants per user
      // typically < 5) so this is fine.
      const map: Record<string, boolean> = {};
      for (const a of list) {
        try {
          const ps: AssistantSharedPatient[] = await aiAssistantsApi.listPatients(a.id);
          map[a.id] = ps.some((p) => p.patient_id === patientId);
        } catch {
          map[a.id] = false;
        }
      }
      setShared(map);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [patientId, t]);

  useEffect(() => {
    if (open) {
      setErr(null);
      setAssistants(null);
      reload();
    }
  }, [open, reload]);

  const toggle = useCallback(
    async (a: AiAssistant) => {
      setBusyId(a.id);
      setErr(null);
      try {
        if (shared[a.id]) {
          await aiAssistantsApi.unsharePatient(a.id, patientId);
        } else {
          await aiAssistantsApi.sharePatient(a.id, patientId);
        }
        setShared((s) => ({ ...s, [a.id]: !s[a.id] }));
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("actionFailed"));
      } finally {
        setBusyId(null);
      }
    },
    [patientId, shared, t],
  );

  if (!open) return null;

  // Backdrop + card layout matches ``ModalHost`` so the radiologist
  // gets the same visual contract whether they're hitting a confirm
  // / prompt dialog or this richer share modal — same blur, same
  // rounded card, same z-index, same close-on-backdrop semantics.
  return (
    <NativeDialog
      open={open}
      onClose={onClose}
      ariaLabel={t("patientShareModalTitle")}
      className="bv-dialog"
    >
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, inherit)",
          border: "1px solid var(--bv-card-border, #d0d5dd)",
          borderRadius: 10,
          boxShadow: "0 18px 42px rgba(0,0,0,0.35)",
          padding: "1.1rem 1.25rem",
          minWidth: 320,
          maxWidth: 540,
          width: "min(100%, 540px)",
        }}
      >
        <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>{t("patientShareModalTitle")}</h2>
        <p className="meta" style={{ fontSize: "0.85rem" }}>
          {t("patientShareModalIntro")}
        </p>
        {err && <p className="error">{err}</p>}
        {assistants === null && <p className="meta">Loading…</p>}
        {assistants !== null && assistants.length === 0 && (
          <div className="meta" style={{ marginTop: "0.5rem", fontSize: "0.9rem" }}>
            {t("patientShareNoAssistants")}{" "}
            <Link href="/settings/ai-assistants">{t("patientShareCreateLink")}</Link>.
          </div>
        )}
        {assistants?.map((a) => (
          <label
            key={a.id}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
              padding: "0.4rem 0",
              borderBottom: "1px solid var(--bv-card-border, #f0f1f4)",
            }}
          >
            <input
              type="checkbox"
              checked={!!shared[a.id]}
              onChange={() => toggle(a)}
              disabled={busyId === a.id}
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div>{a.label}</div>
              <div className="meta" style={{ fontSize: "0.75rem" }}>
                {[a.provider, a.model_id].filter(Boolean).join(" · ") || "—"}
              </div>
            </div>
            {!a.is_active && (
              <span className="badge" style={{ background: "#94a3b8", color: "#fff" }}>
                {t("statusRevoked")}
              </span>
            )}
          </label>
        ))}
        <div style={{ marginTop: "0.75rem", display: "flex", justifyContent: "flex-end" }}>
          <button type="button" onClick={onClose}>
            {t("patientShareDone")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}
