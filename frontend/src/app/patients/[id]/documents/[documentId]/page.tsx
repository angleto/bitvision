"use client";

import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import ContextualBackLink from "@/components/ContextualBackLink";
import DocumentPreview from "@/components/DocumentPreview";
import Markdown from "@/components/Markdown";
import NotesPanel from "@/components/NotesPanel";
import {
  ApiError,
  type DocumentKindEntry,
  type Patient,
  type PatientDocument,
  getStoredToken,
  patientsApi,
} from "@/lib/api";
import { entryLabel, useDocumentCatalog } from "@/lib/useDocumentCatalog";

/**
 * Patient document detail page. The route is
 * ``/patients/<patient_id>/documents/<doc_id>``: this is what
 * ``ContentPane`` redirects to when a document leaf is opened from the
 * fascicolo. Documents come in two shapes:
 *
 *   1. Inline text (``text`` non-null, no S3 file) — typical of free-form
 *      clinical notes / discharge letters / prescriptions seeded by the
 *      backend or pasted by a doctor.
 *   2. Uploaded file (``file_s3_key`` non-null) — PDF, image, etc.
 *
 * We render the inline text directly with ``Markdown``; otherwise we
 * defer to ``DocumentPreview`` which handles PDF / image / unknown via
 * the ``/content`` endpoint.
 */
export default function PatientDocumentPage() {
  const params = useParams<{ id: string; documentId: string }>();
  const tFasc = useTranslations("fascicolo");
  const locale = useLocale();
  const { catalog } = useDocumentCatalog();
  const patientId = params.id;
  const documentId = params.documentId;

  const [doc, setDoc] = useState<PatientDocument | null>(null);
  const [patient, setPatient] = useState<Patient | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!documentId) return;
    let cancelled = false;
    (async () => {
      try {
        const [d, p] = await Promise.all([
          patientsApi.getDocument(patientId, documentId),
          patientsApi.detail(patientId),
        ]);
        if (cancelled) return;
        setDoc(d);
        setPatient(p);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [patientId, documentId]);

  if (err)
    return (
      <main>
        <p className="meta">
          <Link href={`/patients/${patientId}`}>&larr; {tFasc("treeRootLabel")}</Link>
        </p>
        <p className="error">{err}</p>
      </main>
    );
  if (!doc || !patient)
    return (
      <main>
        <p className="meta">
          <Link href={`/patients/${patientId}`}>&larr; {tFasc("treeRootLabel")}</Link>
        </p>
        <p className="meta">Loading...</p>
      </main>
    );

  // Catalog is the single source of truth: the label for ``kind_id``
  // comes from ``display_name[locale]`` for the matching row (active
  // or not, so a legacy / soft-deleted kind still renders as text and
  // not as the bare id).
  const docKind = catalog?.kinds.find((k) => k.id === doc.document_type) ?? null;
  const typeLabel = docKind ? entryLabel(docKind, locale) : doc.document_type;
  const when = doc.document_date ?? doc.created_at.slice(0, 10);
  const hasFile = !!doc.file_s3_key;

  return (
    <main>
      <p className="meta">
        <ContextualBackLink
          patientId={patientId}
          patientName={patient.display_name}
          itemKind="document"
          itemId={documentId}
        />
      </p>
      <header style={{ marginBottom: "1rem" }}>
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            justifyContent: "space-between",
            gap: "0.5rem",
            flexWrap: "wrap",
          }}
        >
          <h1 style={{ marginBottom: "0.25rem" }}>{doc.title}</h1>
          <button
            type="button"
            className="ghost"
            onClick={() => setEditing((v) => !v)}
            style={{ fontSize: "0.85rem" }}
            title={tFasc("documentDetail.editMetadataTooltip")}
          >
            {editing
              ? tFasc("documentDetail.editMetadataClose")
              : tFasc("documentDetail.editMetadataOpen")}
          </button>
        </div>
        <p className="meta">
          <strong>{typeLabel}</strong>
          {" · "}
          {when}
          {doc.document_date && doc.document_date !== doc.created_at.slice(0, 10) && (
            <span style={{ marginLeft: "0.5rem", opacity: 0.7 }}>
              (caricato in piattaforma {doc.created_at.slice(0, 10)})
            </span>
          )}
        </p>
      </header>

      {editing && (
        <DocumentMetadataForm
          patientId={patientId}
          doc={doc}
          catalogKinds={catalog?.kinds ?? []}
          onCancel={() => setEditing(false)}
          onSaved={(next) => {
            setDoc(next);
            setEditing(false);
          }}
        />
      )}

      {/* Two-column layout: document content on the left, sticky notes
          panel on the right. The doctor doesn't have to scroll past
          the document to write — the textarea is always in view at
          the right edge of the viewport. Collapses to single column
          under 1000px so notes stack below on tablets / phones. */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) minmax(320px, 380px)",
          gap: "1.25rem",
          alignItems: "start",
        }}
        className="bv-doc-grid"
      >
        <div style={{ minWidth: 0 }}>
          {doc.text && (
            <article className="card" style={{ padding: "1rem 1.25rem", marginBottom: "1rem" }}>
              <Markdown text={doc.text} />
            </article>
          )}

          {hasFile && (
            <div
              style={{
                height: "min(900px, 78vh)",
                display: "flex",
                marginBottom: "1rem",
              }}
            >
              <DocumentPreview
                documentId={doc.id}
                patientId={patientId}
                documentType={doc.document_type}
                contentType={doc.file_content_type}
                filename={inferFilename(doc)}
              />
            </div>
          )}

          {doc.files && doc.files.length > 0 && <DocumentGallery patientId={patientId} doc={doc} />}

          {!doc.text && !hasFile && (!doc.files || doc.files.length === 0) && (
            <p className="meta">{tFasc("documentDetail.noContent")}</p>
          )}
        </div>

        <aside
          style={{
            position: "sticky",
            top: "calc(var(--header-h, 56px) + 1rem)",
            alignSelf: "start",
            maxHeight: "calc(100vh - var(--header-h, 56px) - 2rem)",
            overflowY: "auto",
          }}
        >
          <NotesPanel patientId={patientId} targetKind="document" targetId={doc.id} />
        </aside>
      </div>

      <style jsx global>{`
        @media (max-width: 1000px) {
          .bv-doc-grid {
            grid-template-columns: minmax(0, 1fr) !important;
          }
          .bv-doc-grid > aside {
            position: static !important;
            max-height: none !important;
          }
        }
      `}</style>
    </main>
  );
}

/**
 * Multi-file gallery for documents that carry N scans / pages.
 *
 * Two layouts depending on what the files look like:
 *
 *   - All-image gallery (typical: 5 photos of a paper report) →
 *     responsive grid of thumbnails. Click opens the full-size image
 *     in a new tab via the redirect endpoint.
 *   - Mixed / non-image (e.g. one PDF + extra images) → vertical list
 *     with type badge, filename, size, "Apri" link.
 *
 * Each file fetches its bytes through the auth-bearer-protected
 * ``/files/{file_id}/content`` endpoint, which then 307s to a
 * presigned S3 URL — the browser follows the redirect transparently
 * for ``<img src>`` / ``<a href>`` once the auth header has been
 * presented to the API; for direct image rendering we fetch as blob
 * and inject ``object URLs`` to avoid the auth-on-image issue.
 */
function DocumentGallery({
  patientId,
  doc,
}: {
  patientId: string;
  doc: PatientDocument;
}) {
  const t = useTranslations("documentDetailPage");
  const allImage =
    doc.files.length > 0 &&
    doc.files.every((f) => (f.file_content_type ?? "").toLowerCase().startsWith("image/"));

  return (
    <section
      style={{
        marginBottom: "1rem",
        marginTop: doc.text || doc.file_s3_key ? "1.25rem" : 0,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: "0.5rem",
          marginBottom: "0.5rem",
        }}
      >
        <h3 style={{ margin: 0 }}>
          {allImage ? t("scans") : t("attachments")} ({doc.files.length})
        </h3>
        <span className="meta" style={{ fontSize: "0.78rem" }}>
          {t("clickPageHint")}
        </span>
      </div>
      {allImage ? (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
            gap: "0.6rem",
          }}
        >
          {doc.files.map((f) => (
            <GalleryThumbnail key={f.id} patientId={patientId} docId={doc.id} file={f} />
          ))}
        </div>
      ) : (
        <ul className="card" style={{ listStyle: "none", padding: "0.5rem 0", margin: 0 }}>
          {doc.files.map((f) => (
            <li
              key={f.id}
              style={{
                display: "flex",
                gap: "0.5rem",
                alignItems: "center",
                padding: "0.4rem 1rem",
                borderTop: "1px solid var(--bv-divider)",
              }}
            >
              <span className="badge" style={{ minWidth: 50, textAlign: "center" }}>
                #{f.sequence + 1}
              </span>
              <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                {f.original_filename ?? `file-${f.sequence}`}
              </span>
              <span className="meta" style={{ fontSize: "0.78rem" }}>
                {f.file_content_type ?? "?"}
              </span>
              <span className="meta" style={{ fontSize: "0.78rem" }}>
                {f.size_bytes != null ? formatBytes(f.size_bytes) : ""}
              </span>
              <FileLink patientId={patientId} docId={doc.id} fileId={f.id}>
                {t("open")}
              </FileLink>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function GalleryThumbnail({
  patientId,
  docId,
  file,
}: {
  patientId: string;
  docId: string;
  file: PatientDocument["files"][number];
}) {
  const t = useTranslations("documentDetailPage");
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const ac = new AbortController();
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    fetch(patientsApi.documentFileContentUrl(patientId, docId, file.id), {
      credentials: "include",
      headers,
      signal: ac.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((b) => {
        if (cancelled) return;
        setSrc(URL.createObjectURL(b));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [patientId, docId, file.id]);

  return (
    <FileLink patientId={patientId} docId={docId} fileId={file.id}>
      <div
        style={{
          position: "relative",
          width: "100%",
          aspectRatio: "1",
          background: "#0a0d14",
          borderRadius: 6,
          overflow: "hidden",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {src ? (
          <img
            src={src}
            alt={file.original_filename ?? t("pageAlt", { n: file.sequence + 1 })}
            draggable={false}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          <span style={{ color: "#475569", fontSize: "0.78rem" }}>{t("loading")}</span>
        )}
        <span
          style={{
            position: "absolute",
            top: 4,
            left: 4,
            background: "rgba(0,0,0,0.7)",
            color: "#e6ecf3",
            fontSize: "0.65rem",
            padding: "1px 5px",
            borderRadius: 3,
            fontFamily: "ui-monospace, monospace",
          }}
        >
          #{file.sequence + 1}
        </span>
      </div>
    </FileLink>
  );
}

function FileLink({
  patientId,
  docId,
  fileId,
  children,
}: {
  patientId: string;
  docId: string;
  fileId: string;
  children: React.ReactNode;
}) {
  // Open in a new tab via the redirect endpoint. The browser sends no
  // auth header for a fresh tab, but the redirect target is a presigned
  // S3 URL with auth baked in — works as long as the user has a
  // session cookie on the same origin OR the API tolerates anonymous
  // calls within the redirect flow (it does for the same-tab navigation
  // because the cookie travels). For Bearer-only auth this falls back
  // to the bytes-as-blob path used by GalleryThumbnail.
  const href = patientsApi.documentFileContentUrl(patientId, docId, fileId);
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      style={{ textDecoration: "none", color: "inherit", display: "block" }}
    >
      {children}
    </a>
  );
}

function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  return `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/**
 * Build a filename for the preview / download fallback. Honors the
 * stored content-type so PDFs / images keep the right extension; falls
 * back to ``.bin`` for unknown types so DocumentPreview can still serve
 * a download link.
 */
function inferFilename(doc: PatientDocument): string {
  const safe = (doc.title || `document-${doc.id}`).replace(/[^A-Za-z0-9._-]+/g, "_");
  const ct = (doc.file_content_type || "").toLowerCase();
  const ext =
    ct === "application/pdf"
      ? ".pdf"
      : ct === "image/jpeg"
        ? ".jpg"
        : ct === "image/png"
          ? ".png"
          : ct === "text/plain"
            ? ".txt"
            : ct === "text/markdown"
              ? ".md"
              : ".bin";
  // Don't double-suffix when the title already carries the right
  // extension (``foo.pdf`` + ``.pdf`` would render ``foo.pdf.pdf``).
  // Also accept ``.jpeg`` for ``.jpg``.
  const lower = safe.toLowerCase();
  const aliases = ext === ".jpg" ? [".jpg", ".jpeg"] : [ext];
  if (aliases.some((a) => lower.endsWith(a))) return safe;
  return `${safe}${ext}`;
}

/**
 * Editable metadata form for a patient document.
 *
 * Most important field clinically: ``document_date``. When the user
 * scans a paper report from 2024 and uploads it today, the platform's
 * ``created_at`` is "today" but the clinically meaningful date is
 * 2024 — that's what shows up in the timeline / sort orders. The
 * form lets the user adjust it without re-uploading.
 */
function DocumentMetadataForm({
  patientId,
  doc,
  catalogKinds,
  onCancel,
  onSaved,
}: {
  patientId: string;
  doc: PatientDocument;
  /**
   * Active catalog rows for the kind dropdown. May be empty while the
   * catalog request is in flight; when empty we fall back to a
   * read-only display of the current ``kind_id`` so the user is never
   * presented with a wrong default that they could submit by accident.
   */
  catalogKinds: DocumentKindEntry[];
  onCancel: () => void;
  onSaved: (next: PatientDocument) => void;
}) {
  const tFasc = useTranslations("fascicolo");
  const locale = useLocale();
  const [title, setTitle] = useState(doc.title);
  const [docType, setDocType] = useState(doc.document_type);
  const [docDate, setDocDate] = useState(doc.document_date ?? "");
  const [textBody, setTextBody] = useState(doc.text ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // The dropdown options are the active catalog rows plus, when the
  // current document references a soft-deleted (or otherwise inactive)
  // kind, that legacy id as a disabled-looking entry so the user's
  // existing value is always visible and selectable. Without this, a
  // controlled ``<select value="...">`` whose value matches no option
  // silently falls back to the first option in the browser — exactly
  // the regression we are fixing here.
  const activeKinds = catalogKinds.filter((k) => k.is_active);
  const currentInActive = activeKinds.some((k) => k.id === docType);
  const legacyEntry = !currentInActive
    ? (catalogKinds.find((k) => k.id === docType) ?? null)
    : null;
  const dropdownOptions: DocumentKindEntry[] = legacyEntry
    ? [legacyEntry, ...activeKinds]
    : activeKinds;

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const next = await patientsApi.updateDocument(patientId, doc.id, {
        title: title.trim() || doc.title,
        document_type: docType,
        document_date: docDate || null,
        text: textBody || null,
      });
      onSaved(next);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" style={{ padding: "1rem 1.25rem", marginBottom: "1rem" }}>
      <h3 style={{ marginTop: 0 }}>{tFasc("documentDetail.editFormTitle")}</h3>
      {err && <p className="error">{err}</p>}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "0.6rem",
        }}
      >
        <label style={{ gridColumn: "1 / -1" }}>
          <span className="meta">{tFasc("documentDetail.fieldTitle")}</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{tFasc("documentDetail.fieldType")}</span>
          <select
            value={docType}
            onChange={(e) => setDocType(e.target.value)}
            disabled={dropdownOptions.length === 0}
            style={{ width: "100%", padding: "0.4rem" }}
          >
            {dropdownOptions.map((k) => (
              <option key={k.id} value={k.id}>
                {entryLabel(k, locale)}
                {!k.is_active ? ` (${tFasc("documentDetail.legacyKindSuffix")})` : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="meta">{tFasc("documentDetail.fieldDate")}</span>
          <input
            type="date"
            value={docDate}
            onChange={(e) => setDocDate(e.target.value)}
            style={{ width: "100%" }}
          />
        </label>
        <label style={{ gridColumn: "1 / -1" }}>
          <span className="meta">{tFasc("documentDetail.fieldText")}</span>
          <textarea
            value={textBody}
            onChange={(e) => setTextBody(e.target.value)}
            rows={4}
            style={{ width: "100%" }}
          />
        </label>
      </div>
      <div
        style={{
          marginTop: "0.75rem",
          display: "flex",
          justifyContent: "flex-end",
          gap: "0.4rem",
        }}
      >
        <button type="button" className="ghost" onClick={onCancel} disabled={busy}>
          {tFasc("documentDetail.cancel")}
        </button>
        <button type="button" onClick={save} disabled={busy || !title.trim()}>
          {busy ? tFasc("documentDetail.saveBusy") : tFasc("documentDetail.save")}
        </button>
      </div>
    </section>
  );
}
