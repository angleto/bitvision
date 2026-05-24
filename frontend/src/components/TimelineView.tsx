"use client";

// Chronological timeline of a patient's fascicolo: pages through
// `/api/patients/{id}/timeline`, groups by month, and surfaces a
// per-item navigation target the parent can intercept via `onNavigate`
// (falls back to direct navigation when no handler is provided).

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, type TimelineItem, patientsApi } from "@/lib/api";

const PAGE_SIZE = 100;

export type TimelineNavigationTarget =
  | { kind: "study"; id: string }
  | { kind: "series"; id: string }
  | { kind: "report"; studyId: string; reportId: string }
  | { kind: "annotation"; studyId: string | null; annotationId: string }
  | { kind: "document"; id: string }
  | { kind: "consultation"; patientId: string; id: string };

export interface TimelineViewProps {
  patientId: string;
  /** When provided, item clicks call this handler instead of navigating. */
  onNavigate?: (target: TimelineNavigationTarget) => void;
}

export default function TimelineView({ patientId, onNavigate }: TimelineViewProps) {
  const tTv = useTranslations("timelineView");
  const [items, setItems] = useState<TimelineItem[]>([]);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const loadMore = useCallback(async () => {
    if (loading || done) return;
    setLoading(true);
    setErr(null);
    try {
      const page = await patientsApi.timeline(patientId, {
        limit: PAGE_SIZE,
        offset,
      });
      setItems((prev) => [...prev, ...page]);
      setOffset((prev) => prev + page.length);
      if (page.length < PAGE_SIZE) setDone(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "timeline load failed");
    } finally {
      setLoading(false);
    }
  }, [patientId, offset, loading, done]);

  // Reset paging state when the patient changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  useEffect(() => {
    setItems([]);
    setOffset(0);
    setDone(false);
    setErr(null);
  }, [patientId]);

  useEffect(() => {
    if (items.length === 0 && !done && !loading) void loadMore();
  }, [items.length, done, loading, loadMore]);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    // 400px rootMargin pre-fetches one screen ahead of the viewport.
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) void loadMore();
      },
      { rootMargin: "400px" },
    );
    io.observe(node);
    return () => io.disconnect();
  }, [loadMore]);

  const grouped = useMemo(() => groupByMonth(items), [items]);

  return (
    <div style={{ maxWidth: 800, margin: "0 auto", padding: "0 1rem" }}>
      <h2>{tTv("title")}</h2>
      {err && <p className="error">{err}</p>}
      {items.length === 0 && !loading && done && <p className="meta">{tTv("empty")}</p>}
      {grouped.map(({ label, entries }) => (
        <section key={label} style={{ marginBottom: "1.5rem" }}>
          <h3
            style={{
              position: "sticky",
              top: 0,
              margin: 0,
              padding: "0.5rem 0",
              background: "var(--color-bg, #fff)",
              fontSize: "1rem",
              fontWeight: 600,
              borderBottom: "1px solid var(--color-border, #e5e7eb)",
              zIndex: 1,
            }}
          >
            {label}
          </h3>
          {entries.map((item, i) => (
            <TimelineRow
              key={`${item.type}-${(item.data.id as string | undefined) ?? i}-${item.date}`}
              item={item}
              patientId={patientId}
              onNavigate={onNavigate}
            />
          ))}
        </section>
      ))}
      <div ref={sentinelRef} style={{ height: 1 }} />
      {loading && <TimelineLoading />}
    </div>
  );
}

function TimelineLoading() {
  const tT = useTranslations("timeline");
  return <p className="meta">{tT("loading")}</p>;
}

function TimelineRow({
  item,
  patientId,
  onNavigate,
}: {
  item: TimelineItem;
  patientId: string;
  onNavigate?: (t: TimelineNavigationTarget) => void;
}) {
  const tT = useTranslations("timeline");
  const { icon, title, preview, target } = describeItem(item, patientId, {
    studyDefault: tT("studyDefault"),
    reportFallback: tT("reportFallback"),
    reportVersion: (n: number) => tT("reportVersion", { n }),
  });

  function handleClick() {
    if (!target) return;
    if (onNavigate) {
      onNavigate(target);
      return;
    }
    if (typeof window !== "undefined") {
      window.location.href = fallbackUrl(target, patientId);
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={!target}
      className="card"
      style={{
        display: "flex",
        gap: "0.75rem",
        width: "100%",
        textAlign: "left",
        padding: "0.6rem 0.75rem",
        marginBottom: "0.4rem",
        border: "1px solid var(--color-border, #e5e7eb)",
        borderRadius: 6,
        background: "transparent",
        cursor: target ? "pointer" : "default",
      }}
    >
      <span
        aria-hidden
        style={{
          fontSize: "1.2rem",
          lineHeight: "1.4rem",
          flexShrink: 0,
          width: "1.5rem",
          textAlign: "center",
        }}
      >
        {icon}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            gap: "0.5rem",
          }}
        >
          <strong
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {title}
          </strong>
          <span className="meta" style={{ fontSize: "0.8rem", flexShrink: 0 }}>
            {formatDate(item.date)}
          </span>
        </div>
        {preview && (
          <p
            className="meta"
            style={{
              margin: "0.15rem 0 0",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {preview}
          </p>
        )}
      </div>
    </button>
  );
}

function groupByMonth(items: TimelineItem[]): { label: string; entries: TimelineItem[] }[] {
  const buckets = new Map<string, TimelineItem[]>();
  for (const item of items) {
    const key = monthKey(item.date);
    const bucket = buckets.get(key);
    if (bucket) bucket.push(item);
    else buckets.set(key, [item]);
  }
  // Map insertion order preserves the backend's date-desc ordering.
  return Array.from(buckets.entries()).map(([label, entries]) => ({
    label,
    entries,
  }));
}

function monthKey(raw: string): string {
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return "—";
  // Use Intl so the month label follows the active locale.
  return new Intl.DateTimeFormat(undefined, {
    month: "long",
    year: "numeric",
  }).format(d);
}

function formatDate(raw: string): string {
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
}

interface TimelineLabels {
  studyDefault: string;
  reportFallback: string;
  reportVersion: (n: number) => string;
}

function describeItem(
  item: TimelineItem,
  patientId: string,
  labels: TimelineLabels,
): {
  icon: string;
  title: string;
  preview: string | null;
  target: TimelineNavigationTarget | null;
} {
  const data = item.data ?? {};
  const id = data.id as string | undefined;
  switch (item.type) {
    case "study": {
      const title = (data.study_description as string | null) || labels.studyDefault;
      const modalities = Array.isArray(data.modalities)
        ? (data.modalities as string[]).join(", ")
        : null;
      return {
        icon: "[S]",
        title,
        preview: modalities,
        target: id ? { kind: "study", id } : null,
      };
    }
    case "report": {
      const version = data.version as number | undefined;
      const title = version ? labels.reportVersion(version) : labels.reportFallback;
      const preview = firstLine(data.text as string | null | undefined);
      const studyId = data.study_id as string | undefined;
      return {
        icon: "[R]",
        title,
        preview,
        target: id && studyId ? { kind: "report", studyId, reportId: id } : null,
      };
    }
    case "annotation": {
      const kind = (data.kind as string | null) ?? "annotation";
      const source = (data.source as string | null) ?? "";
      const title = `Annotazione ${kind}${source ? ` (${source})` : ""}`;
      const payload = (data.payload as Record<string, unknown> | undefined) ?? {};
      const preview = firstLine(
        (payload.text as string | null | undefined) ??
          (payload.description as string | null | undefined),
      );
      const studyId = (data.target_id as string | null | undefined) ?? null;
      return {
        icon: "[A]",
        title,
        preview,
        target: id ? { kind: "annotation", studyId, annotationId: id } : null,
      };
    }
    case "document": {
      const title = (data.title as string | null) || "Documento";
      const docType = data.document_type as string | null | undefined;
      const preview = firstLine(data.text as string | null | undefined) ?? docType ?? null;
      return {
        icon: "[D]",
        title,
        preview,
        target: id ? { kind: "document", id } : null,
      };
    }
    default: {
      // Handles "consultation" and any future type the backend adds.
      const title = (data.title as string | null) || item.type;
      return {
        icon: "[C]",
        title,
        preview: firstLine(data.summary_md as string | null | undefined),
        target: id ? { kind: "consultation", patientId, id } : null,
      };
    }
  }
}

function firstLine(s: string | null | undefined): string | null {
  if (!s) return null;
  const trimmed = s.trim();
  if (!trimmed) return null;
  const idx = trimmed.search(/\r?\n/);
  return idx === -1 ? trimmed : trimmed.slice(0, idx);
}

function fallbackUrl(t: TimelineNavigationTarget, patientId: string): string {
  switch (t.kind) {
    case "study":
      return `/patients/${patientId}/studies/${t.id}`;
    case "series":
      return `/viewer/series/${t.id}`;
    case "report":
      return `/patients/${patientId}/studies/${t.studyId}`;
    case "annotation":
      return t.studyId ? `/patients/${patientId}/studies/${t.studyId}` : "#";
    case "document":
      // No dedicated document page — keep the user on the current page.
      return "#";
    case "consultation":
      return `/patients/${t.patientId}/consultations/${t.id}`;
  }
}
