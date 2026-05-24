"use client";

/**
 * PatientAskPanel — natural-language Q&A over one patient's record.
 *
 * Surface that the user types a question into ("qual'è l'ultima
 * PET?", "riassumi l'istologico"); the backend orchestrator runs a
 * tier-aware tool-use loop and streams back an answer with inline
 * citations. Citations are rendered as clickable chips that open the
 * cited document / event / chunk in a side dialog.
 *
 * Tier policy:
 * - The tier badge ("free" / "standard" / "premium") is announced as
 *   the first SSE event so the user knows which model is running
 *   before any text shows up.
 * - When the wallet gate auto-downgrades (e.g. premium → standard
 *   because balance was short), a non-blocking warning appears with a
 *   link to ``/settings/billing`` for top-up.
 * - On HTTP 402 the backend returns a JSON envelope with
 *   ``balance_cents`` + ``estimated_max_cost_cents`` + ``top_up_url``;
 *   we render that as a modal (NativeDialog) instead of a chat reply.
 *
 * History is kept client-side only (sessionStorage) per the round-2
 * decision; nothing about a Q&A turn is persisted server-side beyond
 * the credit-ledger debit and the provenance audit row.
 */

import { useLocale, useTranslations } from "next-intl";
import {
  Fragment,
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";

import EvidenceContent from "@/components/EvidenceContent";
import NativeDialog from "@/components/NativeDialog";
import ReportContentDetail from "@/components/ReportContentDetail";
import {
  type AiModelsBundle,
  type AiTierStatus,
  ApiError,
  type AvailableAiModel,
  type ClinicalNote,
  type PatientDocument,
  type QnaAnswerOut,
  type QnaCitation,
  type QnaInsufficientCredits,
  type QnaTier,
  aiModelsApi,
  aiTierApi,
  patientsApi,
  qnaAskStream,
} from "@/lib/api";
import {
  type ClinicalEvent,
  type ReportContent,
  clinicalEventsApi,
  reportContentsApi,
} from "@/lib/api_records";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ConversationTurn {
  /** Stable id used as React key + sessionStorage entry. */
  id: string;
  question: string;
  /** Markdown answer rendered with react-markdown. */
  answerMd: string;
  citations: QnaCitation[];
  tier: QnaTier;
  downgraded: boolean;
  modelId: string | null;
  iterations: number;
  usedTools: string[];
  /** True while the SSE stream is still open. */
  streaming: boolean;
  /** Backend ``request_id`` used as idempotency key for the wallet
   *  debit; surfaced to the user for support tickets. */
  requestId: string | null;
  error: string | null;
}

interface Props {
  patientId: string;
  /**
   * Optional class name applied to the root container so the host
   * page can drop the panel into its own grid without leaking layout
   * concerns into this component.
   */
  className?: string;
}

const STORAGE_KEY_PREFIX = "bvp.patientAsk.history:";

const SUGGESTED_PROMPTS_IT = [
  "Qual'è l'ultima PET?",
  "Esami del sangue dell'ultimo anno",
  "Trend creatinina",
  "Riassumi l'ultimo istologico",
  "Ci sono accenni a metastasi nei referti?",
];

const SUGGESTED_PROMPTS_EN = [
  "What is the last PET scan?",
  "Lab tests in the last year",
  "Creatinine trend",
  "Summarise the last pathology report",
  "Any mention of metastasis in the reports?",
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function PatientAskPanel({ patientId, className }: Props) {
  const t = useTranslations("patientAsk");
  const locale = useLocale();
  const lang: "it" | "en" = locale === "en" ? "en" : "it";

  const [history, setHistory] = useState<ConversationTurn[]>([]);
  const [pendingQuery, setPendingQuery] = useState("");
  const [streamingTurnId, setStreamingTurnId] = useState<string | null>(null);
  const [insufficient, setInsufficient] = useState<QnaInsufficientCredits | null>(null);
  const [activeCitation, setActiveCitation] = useState<QnaCitation | null>(null);
  const [tierStatus, setTierStatus] = useState<AiTierStatus | null>(null);
  const [modelsBundle, setModelsBundle] = useState<AiModelsBundle | null>(null);
  const [modelOverride, setModelOverride] = useState<string>("");
  const abortRef = useRef<AbortController | null>(null);
  const answerScrollRef = useRef<HTMLDivElement | null>(null);

  // Pull the live tier on mount so the contextual banner shows the
  // user's effective mode immediately (before any /ask is dispatched).
  // Silent on failure: the banner still renders an explanatory string
  // for the workspace default.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, m] = await Promise.all([aiTierApi.status(), aiModelsApi.list()]);
        if (cancelled) return;
        setTierStatus(s);
        setModelsBundle(m);
      } catch {
        // ignore — banner falls back to a generic free-mode message
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Restore client-side history on mount; empty when sessionStorage is
  // cleared or the user lands on a different patient. Per round-2
  // decision: history is intentionally NOT server-persisted in v1.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const raw = window.sessionStorage.getItem(STORAGE_KEY_PREFIX + patientId);
      if (raw) {
        const parsed = JSON.parse(raw) as ConversationTurn[];
        // Drop any turn that was still ``streaming`` when the page
        // unloaded — the SSE connection is gone and we cannot
        // resume it (M5 v1 does not support replay).
        const restored = parsed.map((turn) => ({
          ...turn,
          streaming: false,
        }));
        setHistory(restored);
      }
    } catch {
      // Corrupt JSON → start fresh; not worth showing the user.
    }
  }, [patientId]);

  // Persist history on every change.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.sessionStorage.setItem(STORAGE_KEY_PREFIX + patientId, JSON.stringify(history));
    } catch {
      // Quota exceeded or private mode — ignore; in-memory state is
      // the authoritative source for the running session.
    }
  }, [history, patientId]);

  // History is rendered newest-first (the latest answer sits at the
  // top of the scrollable area). On every change we snap the
  // viewport to ``scrollTop = 0`` so the new content stays visible
  // without manual scroll. ``history`` is the dependency because we
  // want this to fire on every text_delta tick during streaming, not
  // just on mount of a new turn.
  // biome-ignore lint/correctness/useExhaustiveDependencies: history is intentional — we need to re-scroll on every text_delta, not just when streamingTurnId flips.
  useEffect(() => {
    if (!streamingTurnId) return;
    const el = answerScrollRef.current;
    if (el) el.scrollTop = 0;
  }, [streamingTurnId, history]);

  const suggestedPrompts = useMemo(
    () => (lang === "en" ? SUGGESTED_PROMPTS_EN : SUGGESTED_PROMPTS_IT),
    [lang],
  );

  const updateTurn = useCallback((id: string, patch: Partial<ConversationTurn>) => {
    setHistory((prev) => prev.map((turn) => (turn.id === id ? { ...turn, ...patch } : turn)));
  }, []);

  const submit = useCallback(
    async (rawQuery: string) => {
      const query = rawQuery.trim();
      if (!query || streamingTurnId) return;
      setInsufficient(null);

      const turnId =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `turn-${Date.now()}-${Math.random()}`;
      const newTurn: ConversationTurn = {
        id: turnId,
        question: query,
        answerMd: "",
        citations: [],
        tier: "free",
        downgraded: false,
        modelId: null,
        iterations: 0,
        usedTools: [],
        streaming: true,
        requestId: null,
        error: null,
      };
      setHistory((prev) => [...prev, newTurn]);
      setPendingQuery("");
      setStreamingTurnId(turnId);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const { response, events } = await qnaAskStream(
          patientId,
          { query, lang, model_override: modelOverride || null },
          controller.signal,
        );

        if (response.status === 402) {
          const body = (await response.json()) as QnaInsufficientCredits;
          setInsufficient(body);
          updateTurn(turnId, {
            streaming: false,
            error: t("error402"),
          });
          setStreamingTurnId(null);
          return;
        }

        if (!response.ok) {
          updateTurn(turnId, {
            streaming: false,
            error: t("errorGeneric", { status: response.status }),
          });
          setStreamingTurnId(null);
          return;
        }

        for await (const ev of events) {
          if (ev.event === "tier") {
            updateTurn(turnId, {
              tier: ev.data.tier,
              downgraded: ev.data.downgraded,
              requestId: ev.data.request_id,
            });
          } else if (ev.event === "citation") {
            setHistory((prev) =>
              prev.map((turn) =>
                turn.id === turnId ? { ...turn, citations: [...turn.citations, ev.data] } : turn,
              ),
            );
          } else if (ev.event === "text_delta") {
            setHistory((prev) =>
              prev.map((turn) =>
                turn.id === turnId ? { ...turn, answerMd: turn.answerMd + ev.data.delta } : turn,
              ),
            );
          } else if (ev.event === "done") {
            updateTurn(turnId, {
              streaming: false,
              modelId: ev.data.model_id,
              iterations: ev.data.iterations,
              usedTools: ev.data.used_tools,
            });
          } else if (ev.event === "error") {
            updateTurn(turnId, {
              streaming: false,
              error: ev.data.message,
            });
          }
        }
      } catch (e) {
        // Aborted by the user — clean up silently.
        if (controller.signal.aborted) {
          updateTurn(turnId, { streaming: false });
        } else {
          updateTurn(turnId, {
            streaming: false,
            error: e instanceof ApiError ? e.message : t("errorNetwork"),
          });
        }
      } finally {
        setStreamingTurnId(null);
        abortRef.current = null;
      }
    },
    [lang, patientId, streamingTurnId, modelOverride, t, updateTurn],
  );

  const onAbort = useCallback(() => {
    if (abortRef.current) abortRef.current.abort();
  }, []);

  const onClearHistory = useCallback(() => {
    if (streamingTurnId) return;
    setHistory([]);
    if (typeof window !== "undefined") {
      window.sessionStorage.removeItem(STORAGE_KEY_PREFIX + patientId);
    }
  }, [streamingTurnId, patientId]);

  const onSubmitForm = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      submit(pendingQuery);
    },
    [pendingQuery, submit],
  );

  // Cmd+Enter / Ctrl+Enter fires submit while the textarea has focus.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        submit(pendingQuery);
      }
    },
    [pendingQuery, submit],
  );

  const isStreaming = streamingTurnId !== null;

  // Pick the most authoritative tier signal: a finished turn beats
  // the on-mount status query because the wallet gate may have
  // auto-downgraded before the orchestrator ran.
  const lastTurn = history.length > 0 ? history[history.length - 1] : null;
  const effectiveTier: QnaTier = lastTurn
    ? lastTurn.tier
    : (tierStatus?.user_override ?? tierStatus?.workspace_default ?? "free");
  const downgraded = lastTurn?.downgraded ?? false;

  return (
    <div className={`patient-ask-panel${className ? ` ${className}` : ""}`}>
      <TierBanner tier={effectiveTier} downgraded={downgraded} />

      <form
        onSubmit={onSubmitForm}
        style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: "0.75rem",
            flexWrap: "wrap",
          }}
        >
          <label htmlFor="patient-ask-input" className="meta">
            {t("inputLabel")}
          </label>
          {modelsBundle && modelsBundle.available.length > 0 && (
            <ModelPicker
              bundle={modelsBundle}
              value={modelOverride}
              onChange={setModelOverride}
              disabled={isStreaming}
            />
          )}
        </div>
        <textarea
          id="patient-ask-input"
          value={pendingQuery}
          onChange={(e) => setPendingQuery(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={t("inputPlaceholder")}
          rows={3}
          disabled={isStreaming}
          aria-busy={isStreaming}
        />
        <div
          style={{
            display: "flex",
            gap: "0.5rem",
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <span className="meta">{t("suggestedPrompts")}</span>
          {suggestedPrompts.map((prompt) => (
            <button
              key={prompt}
              type="button"
              className="ghost"
              onClick={() => submit(prompt)}
              disabled={isStreaming}
              style={{ fontSize: "0.85rem", padding: "0.2rem 0.6rem" }}
            >
              {prompt}
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <button type="submit" disabled={!pendingQuery.trim() || isStreaming}>
            {isStreaming ? t("sending") : t("send")}
          </button>
          {isStreaming && (
            <button type="button" className="ghost" onClick={onAbort}>
              {t("abort")}
            </button>
          )}
          <span className="meta" style={{ marginLeft: "auto" }}>
            {t("submitHint")}
          </span>
          {history.length > 0 && !isStreaming && (
            <button type="button" className="ghost" onClick={onClearHistory}>
              {t("newConversation")}
            </button>
          )}
        </div>
      </form>

      <div
        ref={answerScrollRef}
        aria-live="polite"
        aria-busy={isStreaming}
        style={{
          marginTop: "1rem",
          maxHeight: "60vh",
          overflowY: "auto",
        }}
      >
        {history.length === 0 ? (
          <p className="meta" style={{ marginTop: "1rem" }}>
            {t("emptyState")}
          </p>
        ) : (
          <>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "0.5rem",
                paddingBottom: "0.5rem",
                borderBottom: "1px solid var(--bv-card-border)",
              }}
            >
              <span className="meta" style={{ fontSize: "0.8rem" }}>
                {t("historyHeader", { n: history.length })}
              </span>
              <button
                type="button"
                className="ghost"
                onClick={onClearHistory}
                disabled={isStreaming}
                style={{ fontSize: "0.8rem" }}
                title={t("clearTitle")}
              >
                ✕ {t("clear")}
              </button>
            </div>
            {[...history].reverse().map((turn) => (
              <ConversationTurnView key={turn.id} turn={turn} onCitationClick={setActiveCitation} />
            ))}
          </>
        )}
      </div>

      <NativeDialog
        open={insufficient !== null}
        onClose={() => setInsufficient(null)}
        ariaLabel={t("insufficientTitle")}
      >
        {insufficient && (
          <div
            className="card"
            style={{ maxWidth: "32rem", margin: "2rem auto", padding: "1.5rem" }}
          >
            <h3 style={{ marginTop: 0 }}>{t("insufficientTitle")}</h3>
            <p>
              {t("insufficientBody", {
                balance: (insufficient.balance_cents / 100).toFixed(2),
                estimate: (insufficient.estimated_max_cost_cents / 100).toFixed(2),
              })}
            </p>
            <div style={{ display: "flex", gap: "0.5rem", marginTop: "1rem" }}>
              <a className="button" href={insufficient.top_up_url}>
                {t("topUp")}
              </a>
              <button type="button" className="ghost" onClick={() => setInsufficient(null)}>
                {t("dismiss")}
              </button>
            </div>
          </div>
        )}
      </NativeDialog>

      {activeCitation && (
        <CitationSidePanel
          citation={activeCitation}
          patientId={patientId}
          onClose={() => setActiveCitation(null)}
          ariaLabel={t("citationDialogTitle")}
        />
      )}
    </div>
  );
}

function CitationSidePanel({
  citation,
  patientId,
  onClose,
  ariaLabel,
}: {
  citation: QnaCitation;
  patientId: string;
  onClose: () => void;
  ariaLabel: string;
}) {
  // Right-anchored side panel rather than a top-of-viewport dialog.
  // Keeps the answer text visible on the left so the user can read
  // the cited excerpt next to the sentence that referenced it.
  // Closes on Escape and on click outside the panel (overlay click).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <button
        type="button"
        aria-label={ariaLabel}
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0,0,0,0.18)",
          zIndex: 99,
          border: 0,
          padding: 0,
          cursor: "pointer",
        }}
      />
      <aside
        // biome-ignore lint/a11y/useSemanticElements: <dialog> has too many native behaviours (form submit, modal stack) we don't want here
        role="dialog"
        aria-label={ariaLabel}
        aria-modal="true"
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100vh",
          width: "min(560px, 95vw)",
          background: "var(--bv-card-bg)",
          color: "var(--bv-fg)",
          borderLeft: "1px solid var(--bv-card-border)",
          boxShadow: "-8px 0 24px rgba(0,0,0,0.18)",
          zIndex: 100,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <CitationPreview citation={citation} patientId={patientId} onClose={onClose} />
      </aside>
    </>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/**
 * One-line contextual band rendered above the textarea.
 *
 * Sole job: tell the user, in plain language, which mode is going to
 * answer the next question and what it costs. When the wallet gate
 * auto-downgrades, the band swaps to the explanation copy so the
 * user understands why they got the cheaper output without opening
 * a dialog. Always carries a link to ``/settings/ai`` so changing
 * the mode is one click away.
 *
 * Three states map to three i18n strings:
 *   * free, no downgrade  → "Modalità Free attiva — …"
 *   * standard / premium  → "Modalità Standard — costo medio …"
 *   * downgraded          → "Modalità X attiva (auto-downgrade) — …"
 */
function TierBanner({
  tier,
  downgraded,
}: {
  tier: QnaTier;
  downgraded: boolean;
}) {
  const t = useTranslations("patientAsk.banner");

  const message = (() => {
    if (downgraded && tier === "free") return t("downgradedToFree");
    if (downgraded && tier === "standard") return t("downgradedToStandard");
    if (tier === "free") return t("free");
    if (tier === "standard") return t("standardActive");
    return t("premiumActive");
  })();

  const ctaLabel = tier === "free" ? t("ctaUpgrade") : t("ctaTopUp");
  const ctaHref = tier === "free" ? "/settings/ai" : "/settings/wallet";

  // Border colour reflects the mode tier so the eye picks it up at a
  // glance without reading; copy still says it explicitly.
  const accent =
    tier === "premium"
      ? "var(--bv-warning, #d97706)"
      : tier === "standard"
        ? "var(--bv-success, #16a34a)"
        : downgraded
          ? "var(--bv-danger, #dc2626)"
          : "var(--bv-info, #6b7280)";

  return (
    <output
      aria-live="polite"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "1rem",
        flexWrap: "wrap",
        padding: "0.5rem 0.75rem",
        marginBottom: "0.75rem",
        borderRadius: "0.4rem",
        borderLeft: `3px solid ${accent}`,
        background: "var(--bv-card-bg, #f8f9fa)",
        fontSize: "0.85rem",
        lineHeight: 1.4,
      }}
    >
      <span>{message}</span>
      <a className="ghost" href={ctaHref} style={{ fontSize: "0.85rem", whiteSpace: "nowrap" }}>
        {ctaLabel} →
      </a>
    </output>
  );
}

function ConversationTurnView({
  turn,
  onCitationClick,
}: {
  turn: ConversationTurn;
  onCitationClick: (c: QnaCitation) => void;
}) {
  const t = useTranslations("patientAsk");
  return (
    <article style={{ marginTop: "1.25rem" }}>
      <p style={{ fontWeight: 600, margin: "0 0 0.4rem" }}>
        <span aria-hidden="true">›</span> {turn.question}
      </p>

      <div
        style={{
          display: "flex",
          gap: "0.5rem",
          flexWrap: "wrap",
          alignItems: "center",
          fontSize: "0.85rem",
          margin: "0 0 0.5rem",
        }}
      >
        <TierBadge tier={turn.tier} downgraded={turn.downgraded} />
        {turn.modelId && (
          <span className="meta" title={t("modelTooltip")}>
            {turn.modelId}
          </span>
        )}
        {turn.iterations > 0 && (
          <span className="meta">{t("iterations", { count: turn.iterations })}</span>
        )}
        {turn.streaming && <span className="meta">⏳ {t("streaming")}</span>}
      </div>

      {turn.error && (
        <p className="error" role="alert">
          {turn.error}
        </p>
      )}

      {turn.answerMd && (
        <div className="qna-answer-md">
          <ReactMarkdown
            components={{
              p: ({ children }) => (
                <p>{processCitationNodes(children, turn.citations, onCitationClick)}</p>
              ),
              li: ({ children }) => (
                <li>{processCitationNodes(children, turn.citations, onCitationClick)}</li>
              ),
              strong: ({ children }) => (
                <strong>{processCitationNodes(children, turn.citations, onCitationClick)}</strong>
              ),
              em: ({ children }) => (
                <em>{processCitationNodes(children, turn.citations, onCitationClick)}</em>
              ),
            }}
          >
            {turn.answerMd}
          </ReactMarkdown>
        </div>
      )}

      {/* Citation chips are rendered inline by ``processCitationNodes``
          inside the answer markdown; the previous bottom-of-turn list
          duplicated each chip and visibly read like "the model said
          this twice". When the answer carries no inline marker (e.g.
          orchestrator dropped them all) we still surface the list so
          the user has a way back to the source. */}
      {turn.citations.length > 0 &&
        !/\[(?:doc|event|note|summary|report|chunk):/.test(turn.answerMd) && (
          <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", marginTop: "0.5rem" }}>
            <span className="meta" style={{ fontSize: "0.78rem", alignSelf: "center" }}>
              {t("sourcesLabel")}:
            </span>
            {turn.citations.map((c) => (
              <button
                key={`${c.kind}:${c.ref_id}`}
                type="button"
                className="ghost"
                onClick={() => onCitationClick(c)}
                title={citationTooltip(c)}
                aria-label={citationTooltip(c)}
                style={{ fontSize: "0.8rem", padding: "0.2rem 0.5rem" }}
              >
                {citationLabel(c)}
              </button>
            ))}
          </div>
        )}

      {turn.usedTools.length > 0 && (
        <p className="meta" style={{ fontSize: "0.75rem", marginTop: "0.4rem" }}>
          {t("toolsUsed")}: {turn.usedTools.join(", ")}
        </p>
      )}
    </article>
  );
}

function TierBadge({
  tier,
  downgraded,
}: {
  tier: QnaTier;
  downgraded: boolean;
}) {
  const t = useTranslations("patientAsk");
  const colorByTier: Record<QnaTier, string> = {
    free: "var(--bv-info, #6b7280)",
    standard: "var(--bv-success, #16a34a)",
    premium: "var(--bv-warning, #d97706)",
  };
  return (
    <span
      style={{
        display: "inline-flex",
        gap: "0.25rem",
        alignItems: "center",
        background: colorByTier[tier],
        color: "white",
        padding: "0.1rem 0.5rem",
        borderRadius: "999px",
        fontSize: "0.75rem",
        fontWeight: 600,
      }}
      title={downgraded ? t("downgradedTooltip") : undefined}
    >
      {t(`tier_${tier}`)}
      {downgraded && <span aria-label={t("downgradedTooltip")}>↓</span>}
    </span>
  );
}

// Max chars for the chip label text. Past this we ellipsis so the chip
// stays inline; the full title goes on the ``title=`` tooltip.
const CITATION_LABEL_MAX = 50;

const CITATION_KIND_EMOJI: Record<string, string> = {
  document: "📄",
  event: "📅",
  clinical_note: "📝",
  summary: "✦",
  report_content: "📋",
  chunk: "🔍",
};

function citationLabel(c: QnaCitation): string {
  const emoji = CITATION_KIND_EMOJI[c.kind] ?? "🔗";
  // Prefer the human title populated server-side (Patch 1). When the
  // backend lookup missed (deleted row, missing kind handler) we fall
  // back to the UUID short so the chip is still clickable and the
  // technical-details panel still shows the raw ref. This is the
  // medical-UX line the user explicitly asked for: a doctor / patient
  // should never read ``report_content:cbd2aa0f`` in the answer body.
  if (!c.title) {
    return `${emoji} ${c.kind}:${c.ref_id.slice(0, 8)}`;
  }
  const trimmed =
    c.title.length > CITATION_LABEL_MAX ? `${c.title.slice(0, CITATION_LABEL_MAX - 1)}…` : c.title;
  return `${emoji} ${trimmed}`;
}

/** Full human description used for ``title=`` tooltip + ``aria-label``.
 *  Mirrors the backend serialisation: title + date when present, raw
 *  UUID fallback otherwise. */
function citationTooltip(c: QnaCitation): string {
  if (!c.title) return `${c.kind}:${c.ref_id}`;
  return c.date ? `${c.title} · ${c.date}` : c.title;
}

/**
 * Replace inline ``[doc:UUID]`` markers in raw markdown text with
 * lightweight citation pills that open the source dialog. Plain text
 * outside the markers passes through unchanged.
 */
function renderInlineCitations(
  text: string,
  citations: QnaCitation[],
  onCitationClick: (c: QnaCitation) => void,
): React.ReactNode {
  // Mirrors the backend ``_CITATION_RE`` (qna.py): both short
  // (``doc``, ``note``, ``report``) and long-form (``document``,
  // ``clinical_note``, ``report_content``) prefixes are accepted —
  // models drift between the two even when the prompt asks for the
  // short form. UUID is followed by an optional snippet payload
  // before the closing ``]``; we accept any non-bracket text there
  // and discard it (the cleaned quote arrives on ``citation.quote``
  // via the SSE event payload, sanitised server-side).
  const re =
    /\[(doc|document|event|note|clinical_note|summary|report|report_content|chunk):([0-9a-fA-F-]{36})(?:\s+[^\[\]]+?)?\]/g;
  const out: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  const kindMap: Record<string, QnaCitation["kind"]> = {
    doc: "document",
    document: "document",
    event: "event",
    note: "clinical_note",
    clinical_note: "clinical_note",
    summary: "summary",
    report: "report_content",
    report_content: "report_content",
    chunk: "chunk",
  };
  m = re.exec(text);
  while (m !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const kind = kindMap[m[1]] ?? "chunk";
    // Backend canonicalises UUIDs to lowercase (Python ``uuid.UUID``
    // construction lowercases hex on parse). The model can emit
    // mixed-case markers, so we lowercase before lookup. Without this
    // step a chip click would never find its citation entry and the
    // dialog would render the bare {kind, ref_id} fallback.
    const refId = m[2].toLowerCase();
    const cit = citations.find((c) => c.kind === kind && c.ref_id.toLowerCase() === refId) ?? {
      kind,
      ref_id: refId,
    };
    out.push(
      <button
        key={`inline-${kind}-${refId}-${m.index}`}
        type="button"
        className="ghost"
        onClick={() => onCitationClick(cit)}
        title={citationTooltip(cit)}
        aria-label={citationTooltip(cit)}
        style={{
          padding: "0 0.3rem",
          fontSize: "0.85em",
          margin: "0 0.15em",
          display: "inline-flex",
          alignItems: "center",
          gap: "0.2em",
        }}
      >
        {citationLabel(cit)}
      </button>,
    );
    last = m.index + m[0].length;
    m = re.exec(text);
  }
  if (last < text.length) out.push(text.slice(last));
  return out.length > 0 ? out : text;
}

/**
 * Walks the React node tree and applies ``renderInlineCitations`` to
 * every text leaf. Required because react-markdown's ``children`` for
 * ``<p>`` / ``<li>`` is an array that mixes plain strings with element
 * nodes (e.g. ``<strong>1.</strong>`` followed by " Visita..."); doing
 * ``String(children)`` collapsed elements into ``[object Object]``.
 */
function processCitationNodes(
  children: React.ReactNode,
  citations: QnaCitation[],
  onCitationClick: (c: QnaCitation) => void,
): React.ReactNode {
  if (typeof children === "string") {
    return renderInlineCitations(children, citations, onCitationClick);
  }
  if (typeof children === "number" || typeof children === "boolean") {
    return children;
  }
  if (children == null) return children;
  if (Array.isArray(children)) {
    return children.map((child, i) => (
      <Fragment
        // biome-ignore lint/suspicious/noArrayIndexKey: react-markdown emits stable child arrays for the same input markdown; the index is the only identifier available for siblings.
        key={i}
      >
        {processCitationNodes(child, citations, onCitationClick)}
      </Fragment>
    ));
  }
  if (isValidElement<{ children?: React.ReactNode }>(children)) {
    const props = children.props ?? {};
    if (props.children !== undefined) {
      return cloneElement(
        children,
        undefined,
        processCitationNodes(props.children, citations, onCitationClick),
      );
    }
  }
  return children;
}

/** Banner shown above the previewed content when the model emitted a
 *  ``"snippet"`` quote inside the citation marker. Helps the reader
 *  see immediately why the source was cited without scanning the
 *  full body. */
function QuoteBanner({ quote }: { quote: string }) {
  const t = useTranslations("patientAsk");
  return (
    <blockquote
      style={{
        margin: 0,
        padding: "0.6rem 0.85rem",
        background: "var(--bv-warning-soft, rgba(217,119,6,0.08))",
        borderLeft: "3px solid var(--bv-warning, #d97706)",
        borderRadius: "0.25rem",
        fontSize: "0.9rem",
        fontStyle: "italic",
        lineHeight: 1.4,
      }}
    >
      <div
        className="meta"
        style={{
          fontStyle: "normal",
          fontSize: "0.72rem",
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          marginBottom: "0.2rem",
        }}
      >
        {t("citationQuoteLabel")}
      </div>
      “{quote}”
    </blockquote>
  );
}

/** Banner shown when the cited report_content is ``status=stale``.
 *  Patch 2 normally collapses the chain server-side, but the FE keeps
 *  this banner as defence in depth — if a stale row ever lands here,
 *  the doctor sees the warning and the link to the canonical row. */
function StaleBanner({
  patientId,
  supersededBy,
}: {
  patientId: string;
  supersededBy: string | null;
}) {
  const t = useTranslations("patientAsk");
  if (!supersededBy) {
    return (
      <p
        className="meta"
        style={{
          margin: 0,
          padding: "0.5rem 0.75rem",
          background: "var(--bv-warning-soft, rgba(217,119,6,0.08))",
          borderLeft: "3px solid var(--bv-warning, #d97706)",
          fontSize: "0.85rem",
        }}
      >
        ⚠ {t("citationStaleNoSuccessor")}
      </p>
    );
  }
  return (
    <p
      className="meta"
      style={{
        margin: 0,
        padding: "0.5rem 0.75rem",
        background: "var(--bv-warning-soft, rgba(217,119,6,0.08))",
        borderLeft: "3px solid var(--bv-warning, #d97706)",
        fontSize: "0.85rem",
      }}
    >
      ⚠ {t("citationStaleWithSuccessor")}{" "}
      <a href={`/patients/${patientId}/clinical-events/?rc=${supersededBy}`}>
        {t("citationStaleOpenCanonical")}
      </a>
    </p>
  );
}

/**
 * Right-anchored citation preview: discriminator on ``citation.kind``
 * that fetches the referenced row and renders it inline so the user
 * doesn't have to leave the chat to read the cited evidence.
 *
 * Per kind:
 *   * ``report_content`` — fetch via ``reportContentsApi.read`` and
 *     reuse :class:`ReportContentDetail` in read-only mode.
 *   * ``event`` — fetch the event + its report_contents list and
 *     render a compact summary with chips for each report.
 *   * ``document`` — fetch metadata (inline text when present) and
 *     show title, kind, date, an excerpt.
 *   * ``clinical_note`` — locate the note by id in the patient's
 *     note list (no per-id endpoint at the time of writing) and
 *     render the body through :class:`EvidenceContent`.
 *   * ``summary`` / ``chunk`` — placeholder card with the "Apri
 *     sorgente" link. The deep-link wiring for these kinds is
 *     followup work; the preview pane already beats the previous
 *     blank-page behaviour.
 *
 * Header always shows kind label + ``title`` + ``date`` from the
 * citation payload (populated server-side by ``_enrich_citations``).
 * Quote banner shows when the model emitted a ``"snippet"`` inside
 * the marker. Technical details (raw kind + ref_id) stay collapsed
 * at the bottom for debugging.
 */
function CitationPreview({
  citation,
  patientId,
  onClose,
}: {
  citation: QnaCitation;
  patientId: string;
  onClose: () => void;
}) {
  const t = useTranslations("patientAsk");
  const kindLabel: Record<string, string> = {
    document: t("citationKindDocument"),
    event: t("citationKindEvent"),
    clinical_note: t("citationKindNote"),
    summary: t("citationKindSummary"),
    report_content: t("citationKindReport"),
    chunk: t("citationKindChunk"),
  };

  return (
    <div
      style={{
        padding: "1.25rem 1.5rem",
        display: "flex",
        flexDirection: "column",
        gap: "1rem",
        height: "100%",
      }}
    >
      <header
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: "1rem",
          paddingBottom: "0.6rem",
          borderBottom: "1px solid var(--bv-card-border)",
        }}
      >
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="meta" style={{ fontSize: "0.78rem", textTransform: "uppercase" }}>
            {kindLabel[citation.kind] ?? citation.kind}
            {citation.date ? ` · ${citation.date}` : ""}
          </div>
          <h3
            style={{
              margin: "0.15rem 0 0",
              fontSize: "1.1rem",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {citation.title ?? `${citation.kind}:${citation.ref_id.slice(0, 8)}`}
          </h3>
        </div>
        <button
          type="button"
          className="ghost"
          onClick={onClose}
          aria-label={t("dismiss")}
          style={{ fontSize: "1.2rem", lineHeight: 1, padding: "0.2rem 0.5rem" }}
        >
          ✕
        </button>
      </header>

      {citation.quote && <QuoteBanner quote={citation.quote} />}

      <CitationContent citation={citation} patientId={patientId} />

      <details
        style={{
          marginTop: "auto",
          paddingTop: "0.75rem",
          borderTop: "1px solid var(--bv-card-border)",
          fontSize: "0.82rem",
        }}
      >
        <summary
          className="meta"
          style={{ cursor: "pointer", listStyle: "none", padding: "0.25rem 0" }}
        >
          ▸ {t("citationTechnicalDetails")}
        </summary>
        <dl
          style={{
            margin: "0.5rem 0 0",
            display: "grid",
            gridTemplateColumns: "max-content 1fr",
            gap: "0.25rem 0.75rem",
          }}
        >
          <dt className="meta">{t("citationKind")}</dt>
          <dd style={{ margin: 0, fontFamily: "var(--bv-mono, monospace)" }}>{citation.kind}</dd>
          <dt className="meta">{t("citationId")}</dt>
          <dd
            style={{
              margin: 0,
              fontFamily: "var(--bv-mono, monospace)",
              wordBreak: "break-all",
              userSelect: "all",
            }}
          >
            {citation.ref_id}
          </dd>
        </dl>
      </details>
    </div>
  );
}

/** Discriminator switch on ``citation.kind``. Fetches the cited record
 *  and renders kind-specific content. Loading / error states are
 *  generic so every branch falls into the same shape. */
function CitationContent({
  citation,
  patientId,
}: {
  citation: QnaCitation;
  patientId: string;
}) {
  switch (citation.kind) {
    case "report_content":
      return <ReportContentPreview citation={citation} patientId={patientId} />;
    case "event":
      return <EventPreview citation={citation} patientId={patientId} />;
    case "document":
      return <DocumentPreview citation={citation} patientId={patientId} />;
    case "clinical_note":
      return <ClinicalNotePreview citation={citation} patientId={patientId} />;
    default:
      return <PreviewFallback citation={citation} patientId={patientId} />;
  }
}

function PreviewLoading() {
  const t = useTranslations("patientAsk");
  return (
    <p className="meta" style={{ fontSize: "0.9rem" }}>
      {t("citationLoading")}
    </p>
  );
}

function PreviewError({ message }: { message: string }) {
  return (
    <p className="error" style={{ fontSize: "0.9rem", margin: 0 }} role="alert">
      {message}
    </p>
  );
}

/** Render the "Apri sorgente" deep-link button. Shown both at the
 *  top of every preview (right under the header / quote banner, so
 *  the user can open the source page without scrolling through a
 *  long ``narrative_md``) and at the bottom (so a reader who scrolls
 *  through the content lands on the action without having to scroll
 *  back up). Same href + label both places — the button is a row,
 *  not a section divider. */
function OpenSourceButton({ href }: { href: string }) {
  const t = useTranslations("patientAsk");
  return (
    <div>
      <a className="button" href={href}>
        {t("openSource")}
      </a>
    </div>
  );
}

/** Generic fallback for kinds without a dedicated preview yet
 *  (``summary``, ``chunk``). Shows a placeholder + "Apri sorgente"
 *  pointing at the patient page so the user is not stranded. */
function PreviewFallback({
  citation,
  patientId,
}: {
  citation: QnaCitation;
  patientId: string;
}) {
  const t = useTranslations("patientAsk");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      <p className="meta" style={{ margin: 0, fontSize: "0.9rem" }}>
        {t("citationPreviewUnavailable")}
      </p>
      <div>
        <a className="button" href={`/patients/${patientId}`}>
          {t("openSource")}
        </a>
      </div>
    </div>
  );
}

function ReportContentPreview({
  citation,
  patientId,
}: {
  citation: QnaCitation;
  patientId: string;
}) {
  const t = useTranslations("patientAsk");
  const [rc, setRc] = useState<ReportContent | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRc(null);
    setErr(null);
    (async () => {
      try {
        const data = await reportContentsApi.read(citation.ref_id);
        if (!cancelled) setRc(data);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : t("citationLoadFailed"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [citation.ref_id, t]);

  if (err) return <PreviewError message={err} />;
  if (!rc) return <PreviewLoading />;

  const deepLink = `/patients/${patientId}/clinical-events/${rc.clinical_event_id}#rc-${rc.id}`;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem", overflow: "auto" }}>
      {rc.status === "stale" && (
        <StaleBanner patientId={patientId} supersededBy={rc.superseded_by_id ?? null} />
      )}
      <OpenSourceButton href={deepLink} />
      <ReportContentDetail rc={rc} patientId={patientId} eventId={rc.clinical_event_id} />
      <OpenSourceButton href={deepLink} />
    </div>
  );
}

function EventPreview({
  citation,
  patientId,
}: {
  citation: QnaCitation;
  patientId: string;
}) {
  const t = useTranslations("patientAsk");
  const [event, setEvent] = useState<ClinicalEvent | null>(null);
  const [reports, setReports] = useState<ReportContent[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setEvent(null);
    setReports([]);
    setErr(null);
    (async () => {
      try {
        const [ev, rcs] = await Promise.all([
          clinicalEventsApi.read(citation.ref_id),
          reportContentsApi.listForEvent(citation.ref_id),
        ]);
        if (!cancelled) {
          setEvent(ev);
          // Hide stale rows from the preview list — they're already
          // collapsed server-side by Patch 2; this is defence in
          // depth for any chip whose response still contained one.
          setReports(rcs.filter((r) => r.status !== "stale"));
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : t("citationLoadFailed"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [citation.ref_id, t]);

  if (err) return <PreviewError message={err} />;
  if (!event) return <PreviewLoading />;

  const deepLink = `/patients/${patientId}/clinical-events/${event.id}`;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      <OpenSourceButton href={deepLink} />
      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "max-content 1fr",
          gap: "0.25rem 0.75rem",
          margin: 0,
          fontSize: "0.9rem",
        }}
      >
        <dt className="meta">{t("eventKindLabel")}</dt>
        <dd style={{ margin: 0 }}>{event.kind}</dd>
        {event.event_date && (
          <>
            <dt className="meta">{t("eventDateLabel")}</dt>
            <dd style={{ margin: 0 }}>{event.event_date}</dd>
          </>
        )}
        {event.body_part && (
          <>
            <dt className="meta">{t("eventBodyPartLabel")}</dt>
            <dd style={{ margin: 0 }}>{event.body_part}</dd>
          </>
        )}
      </dl>
      {event.narrative && (
        <div style={{ fontSize: "0.9rem", lineHeight: 1.5 }}>
          <EvidenceContent
            patientId={patientId}
            body={event.narrative}
            ctx={`citation:event:${event.id}`}
          />
        </div>
      )}
      {reports.length > 0 && (
        <section>
          <h4 style={{ margin: "0.5rem 0 0.4rem", fontSize: "0.9rem" }}>
            {t("eventReportsHeading", { n: reports.length })}
          </h4>
          <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.88rem" }}>
            {reports.map((r) => (
              <li key={r.id}>
                <a href={`/patients/${patientId}/clinical-events/${event.id}#rc-${r.id}`}>
                  {r.title ?? r.authority}
                </a>
                <span className="meta" style={{ marginLeft: "0.4rem", fontSize: "0.78rem" }}>
                  · {r.status}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <OpenSourceButton href={deepLink} />
    </div>
  );
}

function DocumentPreview({
  citation,
  patientId,
}: {
  citation: QnaCitation;
  patientId: string;
}) {
  const t = useTranslations("patientAsk");
  const [doc, setDoc] = useState<PatientDocument | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDoc(null);
    setErr(null);
    (async () => {
      try {
        const data = await patientsApi.getDocument(patientId, citation.ref_id);
        if (!cancelled) setDoc(data);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : t("citationLoadFailed"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [citation.ref_id, patientId, t]);

  if (err) return <PreviewError message={err} />;
  if (!doc) return <PreviewLoading />;

  const extract = doc.text ?? "";
  const truncated = extract.length > 1200 ? `${extract.slice(0, 1200)}…` : extract;
  const deepLink = `/patients/${patientId}/documents/${doc.id}`;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem", overflow: "auto" }}>
      <OpenSourceButton href={deepLink} />
      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "max-content 1fr",
          gap: "0.25rem 0.75rem",
          margin: 0,
          fontSize: "0.9rem",
        }}
      >
        {doc.kind_id && (
          <>
            <dt className="meta">{t("documentKindLabel")}</dt>
            <dd style={{ margin: 0 }}>{doc.kind_id}</dd>
          </>
        )}
        {doc.document_date && (
          <>
            <dt className="meta">{t("documentDateLabel")}</dt>
            <dd style={{ margin: 0 }}>{doc.document_date}</dd>
          </>
        )}
      </dl>
      {truncated ? (
        <pre
          style={{
            margin: 0,
            padding: "0.6rem 0.75rem",
            background: "var(--bv-card-bg-alt, #f9fafb)",
            border: "1px solid var(--bv-card-border)",
            borderRadius: "0.25rem",
            fontSize: "0.85rem",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            maxHeight: "320px",
            overflow: "auto",
          }}
        >
          {truncated}
        </pre>
      ) : (
        <p className="meta" style={{ fontSize: "0.85rem", margin: 0 }}>
          {t("documentNoExtract")}
        </p>
      )}
      <OpenSourceButton href={deepLink} />
    </div>
  );
}

function ClinicalNotePreview({
  citation,
  patientId,
}: {
  citation: QnaCitation;
  patientId: string;
}) {
  const t = useTranslations("patientAsk");
  const [note, setNote] = useState<ClinicalNote | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setNote(null);
    setErr(null);
    (async () => {
      try {
        // No per-id endpoint yet — fetch the patient's note list and
        // pluck the requested one. The list is bounded by the
        // patient's evidence size (typically <100 rows) so the
        // overhead is acceptable for a single preview render.
        const notes = await patientsApi.listNotes(patientId);
        const found = notes.find((n) => n.id === citation.ref_id);
        if (!cancelled) {
          if (found) {
            setNote(found);
          } else {
            setErr(t("citationLoadFailed"));
          }
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : t("citationLoadFailed"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [citation.ref_id, patientId, t]);

  if (err) return <PreviewError message={err} />;
  if (!note) return <PreviewLoading />;

  const deepLink = `/patients/${patientId}?view=evidence#note-${note.id}`;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem", overflow: "auto" }}>
      <OpenSourceButton href={deepLink} />
      <div style={{ fontSize: "0.92rem", lineHeight: 1.55 }}>
        <EvidenceContent patientId={patientId} body={note.body} ctx={`citation:note:${note.id}`} />
      </div>
      <OpenSourceButton href={deepLink} />
    </div>
  );
}

function ModelPicker({
  bundle,
  value,
  onChange,
  disabled,
}: {
  bundle: AiModelsBundle;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
}) {
  const t = useTranslations("patientAsk.modelPicker");
  const byTier = useMemo(() => {
    const m = new Map<"free" | "standard" | "premium", AvailableAiModel[]>([
      ["free", []],
      ["standard", []],
      ["premium", []],
    ]);
    for (const x of bundle.available) m.get(x.tier_hint)?.push(x);
    return m;
  }, [bundle]);
  const defaultLabel = useMemo(() => {
    const def = bundle.available.find((m) => m.model_id === bundle.current_default_model_id);
    return def?.display_name ?? bundle.current_default_model_id;
  }, [bundle]);
  return (
    <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.85rem" }}>
      <span className="meta">{t("label")}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        title={t("title", { tier: bundle.current_tier, default: defaultLabel })}
        style={{ minWidth: 220, padding: "0.25rem 0.4rem", fontSize: "0.85rem" }}
      >
        <option value="">★ {t("default", { name: defaultLabel })}</option>
        {(["standard", "premium", "free"] as const).map((tier) => {
          const items = byTier.get(tier) ?? [];
          if (items.length === 0) return null;
          return (
            <optgroup key={tier} label={t(`tierLabel.${tier}` as never)}>
              {items.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.display_name}
                  {m.is_in_house ? " · in-house" : ""}
                </option>
              ))}
            </optgroup>
          );
        })}
      </select>
    </label>
  );
}
