"use client";

// Client-side detection is deliberately thin (extension + MIME). The
// backend re-classifies based on magic bytes and is the source of truth
// — the UI heuristic only seeds the override dropdown.

import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useModal } from "@/components/ModalHost";
import NativeDialog from "@/components/NativeDialog";
import {
  ApiError,
  type BulkUploadSummary,
  type ContributionTier,
  type DetectedKind,
  type Patient,
  type SuggestedRoute,
  type TreeNode,
  bulkUploadApi,
  patientTreeApi,
  patientsApi,
} from "@/lib/api";
import { extractIsoFiles, isLikelyIsoFile } from "@/lib/iso9660";
import { JOB_TERMINAL_STATUSES, jobsApi, jobsStorage } from "@/lib/jobs";
import { entryLabel, useDocumentCatalog } from "@/lib/useDocumentCatalog";
import { useJob } from "@/lib/useJob";

interface WalkedFile {
  file: File;
  relativePath: string;
}

/**
 * Expand ISO9660 / ISO+Joliet images dropped onto the uploader. Each
 * ``.iso`` file is opened in-browser, its contents listed, and every
 * regular file rewritten to a ``WalkedFile`` rooted at the ISO stem.
 *
 * Runs sequentially across multiple ISOs but streams files out of a
 * single ISO via the parser's async generator, so the uploader's
 * Pass-1 classifier can start working on early files before the
 * tail of the ISO is read.
 *
 * Failures are surfaced via ``onError`` and the offending ISO is
 * dropped from the result; other files in the same drop survive.
 */
async function expandIsos(
  files: WalkedFile[],
  onError?: (msg: string) => void,
): Promise<WalkedFile[]> {
  const out: WalkedFile[] = [];
  for (const wf of files) {
    const lowerName = wf.file.name.toLowerCase();
    const looksIso = lowerName.endsWith(".iso");
    if (!looksIso) {
      out.push(wf);
      continue;
    }
    let confirmed = false;
    try {
      confirmed = await isLikelyIsoFile(wf.file);
    } catch {
      confirmed = false;
    }
    if (!confirmed) {
      // ``.iso`` extension but no CD001 magic — could be UDF-only,
      // hybrid, or have a malformed descriptor table the browser
      // can't walk. The backend has a pycdlib-based fallback that
      // handles those, so forward the raw ISO upstream and let the
      // server take a shot. The 5 GiB per-file cap is wide enough
      // for a hospital DVD; if it overflows the upload itself, the
      // server replies with a 413.
      out.push(wf);
      continue;
    }
    const stem = wf.file.name.replace(/\.iso$/i, "");
    let extractedAny = false;
    try {
      for await (const inner of extractIsoFiles(wf.file)) {
        extractedAny = true;
        out.push({
          file: inner.file,
          relativePath: `${stem}/${inner.path}`,
        });
      }
    } catch (e) {
      // Client-side parser threw mid-walk: most likely the inner
      // directory tree relies on Rock Ridge / UDF extensions we
      // don't implement. Send the raw blob to the server and let
      // it try; surface the reason as a non-fatal hint.
      onError?.(
        `ISO ${wf.file.name}: client extraction failed (${
          e instanceof Error ? e.message : String(e)
        }) — falling back to server-side extraction.`,
      );
      if (!extractedAny) out.push(wf);
    }
  }
  return out;
}

/**
 * readEntries() returns at most ~100 entries per call, so the loop keeps
 * pulling batches until it yields an empty one — a single call is not
 * enough for large folders.
 */
async function walkEntry(entry: FileSystemEntry, prefix: string): Promise<WalkedFile[]> {
  if (entry.isFile) {
    const fileEntry = entry as FileSystemFileEntry;
    const file = await new Promise<File>((resolve, reject) => {
      fileEntry.file(resolve, reject);
    });
    return [{ file, relativePath: prefix ? `${prefix}/${entry.name}` : entry.name }];
  }
  if (entry.isDirectory) {
    const dirEntry = entry as FileSystemDirectoryEntry;
    const reader = dirEntry.createReader();
    const nextPrefix = prefix ? `${prefix}/${entry.name}` : entry.name;
    const batches: WalkedFile[][] = [];
    while (true) {
      const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => {
        reader.readEntries(resolve, reject);
      });
      if (batch.length === 0) break;
      const walked = await Promise.all(batch.map((child) => walkEntry(child, nextPrefix)));
      batches.push(...walked);
    }
    return batches.flat();
  }
  return [];
}

// React doesn't declare these lowercase attrs — spread via an untyped bag
// so they land on the DOM (major browsers honor them for folder picking).
const nonStandardDirProps: Record<string, string> = {
  webkitdirectory: "",
  directory: "",
};

// Picker dropdown is now driven by ``GET /api/document-catalog`` (see
// ``useDocumentCatalog``). The server-side bulk-ingest path runs its
// own classifier on the filename and writes ``kind_id`` from there,
// so the value the user picks here is purely a label preview — but
// we want the preview labels to match the catalog the rest of the
// app uses, so the option list is sourced from the same vocabulary.

function kindFromName(name: string, mime: string): DetectedKind {
  // ``ext`` is the substring after the last "." — when the filename
  // has no dot at all (common for DICOMDIR files like ``44245860``),
  // ``split(".").pop()`` returns the whole name. Guarding against
  // that prevents arbitrary basenames from masquerading as
  // extensions.
  const lower = name.toLowerCase();
  const ext = lower.includes(".") ? (lower.split(".").pop() ?? "") : "";
  if (ext === "dcm" || ext === "dicom" || ext === "ima" || mime === "application/dicom") {
    return "dicom";
  }
  if (ext === "pdf" || mime === "application/pdf") return "pdf";
  if (
    ["jpg", "jpeg", "png", "webp", "gif", "tif", "tiff"].includes(ext) ||
    mime.startsWith("image/")
  ) {
    return "image";
  }
  if (ext === "zip" || mime === "application/zip" || mime === "application/x-zip-compressed") {
    return "archive";
  }
  // Text-ish companion files. Radiology CDs sometimes ship overlay /
  // hanging-protocol XML alongside the DICOMDIR; clinicians may also
  // attach plain notes (.txt / .md), CSV exports, structured-report
  // JSON. The server's U3 classifier maps all of these to ``text`` and
  // PatientDocument storage accepts them — surfacing them as a
  // dedicated client kind keeps the route default at ``document``
  // instead of silently skipping.
  if (
    ["xml", "txt", "md", "csv", "json", "html"].includes(ext) ||
    mime === "application/xml" ||
    mime === "text/xml" ||
    mime.startsWith("text/")
  ) {
    return "text";
  }
  return "unknown";
}

/** DICOM Part-10 magic-byte detection: a 128-byte preamble (typically
 * zeros) followed by ``DICM`` at offset 128. Used as a fallback for
 * files that lack an extension (CD/DICOMDIR archives often store
 * instances with bare numeric basenames like ``44245860``). Reads
 * only the header, not the whole file. */
async function isDicomByMagic(file: File): Promise<boolean> {
  if (file.size < 132) return false;
  try {
    const head = await file.slice(0, 132).arrayBuffer();
    const bytes = new Uint8Array(head);
    return (
      bytes[128] === 0x44 /* D */ &&
      bytes[129] === 0x49 /* I */ &&
      bytes[130] === 0x43 /* C */ &&
      bytes[131] === 0x4d /* M */
    );
  } catch {
    return false;
  }
}

// Mirrors the backend ``services/document_type_heuristic.py`` rules.
// The values returned MUST be valid ``document_kinds`` ids from the
// catalog seeded in migration 0072 — the server's classifier emits
// the same vocabulary, so picking a different one client-side would
// surface a label inconsistent with what the server actually commits.
// (Pre-fix this function returned FE-only ids like ``imaging_report``
// / ``discharge_letter`` that never existed in the DB.)
function guessDocumentType(filename: string): string {
  const n = filename.toLowerCase();
  if (/(consent|consenso|privacy)/.test(n)) return "consent";
  if (/(discharge|dimissione|dimiss)/.test(n)) return "discharge_summary";
  if (/(prescri|ricetta|terapia)/.test(n)) return "prescription";
  if (/(referral|impegnativa|richiesta|rinvia)/.test(n)) return "referral";
  if (/(\blab\b|esame.?labor|analisi|blood|sangue|emocrom|urinocolt)/.test(n)) return "lab_result";
  if (/(\ber\b|emergenz|pronto.?soccor|triage)/.test(n)) return "emergency_report";
  if (
    /(referto.?(radiolog|imag|tc|tac|rm|rx|ct|mri|pet|eco)|radiolog|imaging.?report|\b(tac|rm|rx|tc|ct|mri|pet|eco|ultraso)\b)/.test(
      n,
    )
  )
    return "radiology_report";
  if (
    /(visita.?(oncolog|cardiolog|neurolog|dermatolog|pneumolog|urolog|ginecolog|gastroenter|endocrinolog|reumatolog|psichiat|otorin|ortope|nefrolog|ematolog)|specialist.?(report|note|letter)|consultation.?note)/.test(
      n,
    )
  )
    return "specialist_visit_note";
  if (/(progress.?note|follow.?up|controllo.?(periodic|post)|decorso)/.test(n))
    return "progress_note";
  if (/(anamnes|history.?and.?physical|\bh&p\b|esame.?obiettiv)/.test(n)) return "history_physical";
  if (/(note|appunti|diario|clinical|visita)/.test(n)) return "personal_note";
  return "unclassified";
}

function routeFor(filename: string, detected: DetectedKind): SuggestedRoute {
  if (detected === "dicom") return { kind: "study" };
  if (detected === "archive") return { kind: "archive" };
  if (detected === "pdf" || detected === "image" || detected === "text") {
    return { kind: "document", document_type: guessDocumentType(filename) };
  }
  return { kind: "skip", reason: "unrecognized file type" };
}

function makeEntry(file: File, relativePath: string): Entry {
  const detected = kindFromName(file.name, file.type);
  return {
    id: `${relativePath}:${file.size}:${file.lastModified}:${Math.random().toString(36).slice(2, 8)}`,
    file,
    relativePath,
    detected,
    route: routeFor(file.name, detected),
  };
}

interface Entry {
  id: string;
  file: File;
  relativePath: string;
  detected: DetectedKind;
  /** Seeded by the heuristic, then overridable via the dropdown. */
  route: SuggestedRoute;
}

// Upload lifecycle:
//   idle       — nothing in flight; user picks files / patient.
//   uploading  — XHR is streaming bytes to /api/upload/bulk; progress
//                advances 0→100 via xhr.upload.onprogress.
//   staging    — XHR upload finished (browser-side bytes are out) but
//                we have not yet received the ``202 + JobOut``. The
//                backend is now staging multipart bytes to S3 inside
//                the request handler — for a multi-GiB ISO this can
//                hold the connection 5+ minutes. Without this state,
//                the user stares at "Uploading… 100%" with no signal
//                that the server is the one busy now and it is
//                NOT safe to close the tab yet (closing the tab here
//                aborts the in-flight POST and loses the upload).
//   polling    — the multipart upload returned a 202 + JobOut; the
//                server has staged the bytes to S3 and the worker is
//                running the actual ingest. The ``useJob`` hook
//                polls /api/jobs/{id} for stage + progress until
//                terminal. This phase is *resumable*: the job id
//                lives in localStorage (jobsStorage), so closing the
//                tab and coming back later still surfaces the job.
//   done       — worker reported succeeded; render the summary
//                lifted from ``job.result``.
//   error      — multipart 4xx/5xx, network drop, worker crash, or
//                user-requested cancel.
type Phase = "idle" | "uploading" | "staging" | "polling" | "done" | "error";

const BULK_UPLOAD_JOB_KIND = "bulk_upload";

// Coerce ``job.result`` (Record<string, unknown> from the generic
// JobOut type) into the typed summary the UI renders. Worker stamps
// the keys in services.bulk_ingest.summary_to_dict.
function jobResultToSummary(raw: Record<string, unknown> | null): BulkUploadSummary | null {
  if (!raw) return null;
  return {
    studies_created: (raw.studies_created as BulkUploadSummary["studies_created"]) ?? [],
    documents_created: (raw.documents_created as BulkUploadSummary["documents_created"]) ?? [],
    skipped: (raw.skipped as BulkUploadSummary["skipped"]) ?? [],
    errors: (raw.errors as BulkUploadSummary["errors"]) ?? [],
    files: (raw.files as BulkUploadSummary["files"]) ?? [],
    dicomdir_found: (raw.dicomdir_found as boolean) ?? false,
    zip_archives_found: (raw.zip_archives_found as number) ?? 0,
    total_files: (raw.total_files as number) ?? 0,
  };
}

interface Props {
  onComplete?: (summary: BulkUploadSummary) => void;
  patientId?: string;
  targetFolderId?: string;
}

export default function UniversalUploader({ onComplete, patientId, targetFolderId }: Props) {
  const t = useTranslations("upload");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dirInputRef = useRef<HTMLInputElement>(null);

  const [entries, setEntries] = useState<Entry[]>([]);
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0);
  const [dragOver, setDragOver] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [summary, setSummary] = useState<BulkUploadSummary | null>(null);
  const [showErrors, setShowErrors] = useState(false);
  // Active bulk_upload Job id when the multipart POST has returned a
  // 202 and the server-side worker is running. Drives the polling
  // hook (``useJob`` below) and gets persisted to ``jobsStorage`` so
  // a tab close + reopen resumes from where we left off.
  const [jobId, setJobId] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  // ISO handling toggles. Both default true so the conservative
  // behaviour matches operator expectations (full archive preserved,
  // unpacked content kept tidy in a per-CD sub-folder). Hidden when
  // the current selection contains no .iso files.
  const [keepIsoArchive, setKeepIsoArchive] = useState(true);
  const [extractIsoContents, setExtractIsoContents] = useState(true);
  const [wrapIsoInFolder, setWrapIsoInFolder] = useState(true);
  const job = useJob(jobId).job;
  // F6.1: default to the private tier. T3/T4 auto-create an
  // ai_training / commercial_use Consent row server-side.
  const [tier, setTier] = useState<ContributionTier>("t1");
  // True while we're walking ISO9660 directory tables and slicing the
  // image into per-file Blobs. The drop zone is disabled and a banner
  // tells the user not to navigate away — a 4 GB DVD takes seconds.
  const [extracting, setExtracting] = useState(false);
  // In-page patient + folder picker. Only shown when the parent did
  // not pre-supply a ``patientId`` — opening the uploader from inside
  // a patient fascicolo skips this entirely (the patient is the
  // current page) and falls through to the ZIP / drop UI.
  const [pickedPatient, setPickedPatient] = useState<Patient | null>(null);
  const [pickedFolderId, setPickedFolderId] = useState<string | null>(null);
  // Resolved values that flow into the actual ``bulk_upload`` POST:
  // pre-supplied prop wins (patient fascicolo flow), otherwise the
  // in-page picker's choice is used.
  const effectivePatientId = patientId ?? pickedPatient?.id;
  const effectiveFolderId = targetFolderId ?? pickedFolderId ?? undefined;
  // Number of files still going through the magic-byte pass. Surfaced
  // in the banner so the user knows the DICOM count is still moving
  // up — a 4 GB DVD with 3000+ slices takes a few seconds to scan.
  const [scanningRemaining, setScanningRemaining] = useState(0);
  // Sort state for the entries table. ``null`` means "input order",
  // which is what the user implicitly chose by dropping the files —
  // we don't want to reorder until they explicitly click a header.
  const [sort, setSort] = useState<{
    key: "name" | "path" | "type" | "route" | "size";
    dir: "asc" | "desc";
  } | null>(null);

  const addFiles = useCallback(async (input: WalkedFile[]) => {
    if (input.length === 0) return;

    // Pass 0: expand any ``.iso`` blobs into their member files
    // before the regular classifier sees them. Done first so the
    // existing magic-byte / DICOMDIR plumbing applies uniformly.
    const hasIso = input.some((f) => f.file.name.toLowerCase().endsWith(".iso"));
    let files = input;
    if (hasIso) {
      setExtracting(true);
      try {
        files = await expandIsos(input, (msg) => setErr(msg));
      } finally {
        setExtracting(false);
      }
      if (files.length === 0) return;
    }

    // Pass 1: synchronous classification by name/MIME. Files that
    // come back ``unknown`` go to pass 2 below for a magic-byte
    // re-test.
    const newlyAdded: { id: string; file: File }[] = [];
    setEntries((cur) => {
      const seen = new Set(cur.map((e) => `${e.relativePath}|${e.file.size}`));
      const next = [...cur];
      for (const f of files) {
        const key = `${f.relativePath}|${f.file.size}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const entry = makeEntry(f.file, f.relativePath);
        next.push(entry);
        if (entry.detected === "unknown") {
          newlyAdded.push({ id: entry.id, file: f.file });
        }
      }
      return next;
    });
    // Pass 2: peek the first 132 bytes of each unknown file to look
    // for the DICOM "DICM" preamble. Two issues with the naive
    // ``Promise.all(allFiles)`` shape:
    //   1. With a 4 GB DVD carrying 3000+ slices, dispatching 3000+
    //      concurrent FileReader slices makes the browser choke and
    //      the whole batch resolves in one bucket — the user stares
    //      at "0 DICOM" until everything is done.
    //   2. The result update fires once at the end, so the file list
    //      shows ``unknown`` rows for the entire scan window.
    // Bound concurrency to a small pool and flush upgrades in
    // batches so the count climbs as the scan progresses. The
    // ``scanningRemaining`` counter feeds the banner UI so the user
    // sees the work is actually happening.
    if (newlyAdded.length === 0) return;
    setScanningRemaining((n) => n + newlyAdded.length);
    const POOL_SIZE = 32;
    const FLUSH_BATCH = 64;
    void (async () => {
      const queue = [...newlyAdded];
      let flushBuf: string[] = [];
      const flush = () => {
        if (flushBuf.length === 0) return;
        const upgraded = new Set(flushBuf);
        flushBuf = [];
        setEntries((cur) =>
          cur.map((e) =>
            upgraded.has(e.id)
              ? { ...e, detected: "dicom", route: routeFor(e.file.name, "dicom") }
              : e,
          ),
        );
      };
      const workers = Array.from({ length: POOL_SIZE }, async () => {
        while (queue.length > 0) {
          const item = queue.shift();
          if (!item) break;
          try {
            const isDicom = await isDicomByMagic(item.file);
            if (isDicom) flushBuf.push(item.id);
          } finally {
            setScanningRemaining((n) => Math.max(0, n - 1));
          }
          if (flushBuf.length >= FLUSH_BATCH) flush();
        }
      });
      await Promise.all(workers);
      flush();
    })();
  }, []);

  const handleDrop = useCallback(
    async (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragOver(false);

      const items = e.dataTransfer.items;
      if (items && items.length > 0 && typeof items[0].webkitGetAsEntry === "function") {
        const roots: FileSystemEntry[] = [];
        for (let i = 0; i < items.length; i += 1) {
          const entry = items[i].webkitGetAsEntry();
          if (entry) roots.push(entry);
        }
        // Walk roots concurrently; swallow individual errors so one bad
        // subtree doesn't nuke the whole drop.
        const walked = await Promise.all(
          roots.map((root) => walkEntry(root, "").catch(() => [] as WalkedFile[])),
        );
        const collected = walked.flat();
        if (collected.length > 0) {
          addFiles(collected);
          return;
        }
      }

      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        const list = Array.from(e.dataTransfer.files).map((f) => ({
          file: f,
          relativePath: f.name,
        }));
        addFiles(list);
      }
    },
    [addFiles],
  );

  const handleInputFiles = useCallback(
    (fileList: FileList | null) => {
      if (!fileList || fileList.length === 0) return;
      const list = Array.from(fileList).map((f) => ({
        file: f,
        // webkitRelativePath is only set when the input has the
        // webkitdirectory attribute; blank otherwise.
        relativePath: f.webkitRelativePath || f.name,
      }));
      addFiles(list);
    },
    [addFiles],
  );

  const removeEntry = (id: string) => {
    setEntries((cur) => cur.filter((e) => e.id !== id));
  };

  const updateRoute = (id: string, route: SuggestedRoute) => {
    setEntries((cur) => cur.map((e) => (e.id === id ? { ...e, route } : e)));
  };

  const reset = () => {
    setEntries([]);
    setPhase("idle");
    setProgress(0);
    setErr(null);
    setSummary(null);
    setShowErrors(false);
    if (jobId) jobsStorage.remove(jobId);
    setJobId(null);
    setCancelling(false);
  };

  const upload = async () => {
    if (entries.length === 0) return;
    setPhase("uploading");
    setErr(null);
    setProgress(0);
    setSummary(null);

    const files = entries.map((e) => e.file);
    const relativePaths = entries.map((e) => e.relativePath);
    // Forward every row as an override. We can't cheaply tell "user
    // touched" from "still the default", and the server treats the field
    // as advisory.
    const overrides = entries.map((e) => ({ relative_path: e.relativePath, route: e.route }));

    try {
      const job = await bulkUploadApi.upload({
        files,
        relativePaths,
        patientId: effectivePatientId ?? null,
        targetFolderId: effectiveFolderId ?? null,
        tier,
        overrides,
        keepIsoArchive,
        wrapIsoInFolder,
        extractIsoContents,
        // Progress callback fires for every byte chunk pushed up. The
        // multipart upload finishes at 100%; the server then returns
        // 202+JobOut and we hand off to the polling phase below. For
        // the staging-phase pivot at 100% see the comment on
        // ``Phase.staging`` — large ISOs make the gap between 100%
        // and the 202 several minutes long.
        onProgress: (p) => {
          setProgress(p);
          if (p >= 100) {
            setPhase((cur) => (cur === "uploading" ? "staging" : cur));
          }
        },
      });
      // Hand off to the worker. Persist the id so a refresh between
      // here and the first useJob fetch still surfaces the work.
      jobsStorage.add({ id: job.id, kind: BULK_UPLOAD_JOB_KIND });
      setJobId(job.id);
      setPhase("polling");
    } catch (e) {
      if (e instanceof ApiError) {
        setErr(
          e.status === 404
            ? t("uploadFailed404")
            : e.message || t("uploadFailedHttp", { status: e.status }),
        );
      } else {
        setErr(t("uploadFailedUnknown"));
      }
      setPhase("error");
    }
  };

  // React to job state transitions from the polling hook. Terminal
  // statuses pivot the phase + populate ``summary``/``err``; the
  // ``useJob`` hook itself handles localStorage cleanup on terminal.
  useEffect(() => {
    if (!job) return;
    if (!JOB_TERMINAL_STATUSES.has(job.status)) return;
    if (job.status === "succeeded") {
      const sum = jobResultToSummary(job.result);
      if (sum) {
        setSummary(sum);
        onComplete?.(sum);
      }
      setPhase("done");
      setJobId(null);
    } else if (job.status === "cancelled") {
      setErr(t("uploadCancelled"));
      setPhase("error");
      setJobId(null);
    } else if (job.status === "failed") {
      const code = job.error?.code as string | undefined;
      const message = (job.error?.message as string | undefined) ?? code ?? "";
      setErr(message ? `${code ?? "error"}: ${message}` : t("uploadFailedUnknown"));
      setPhase("error");
      setJobId(null);
    }
  }, [job, onComplete, t]);

  // On mount, resume polling if a bulk_upload Job is still tracked in
  // localStorage from a previous session/tab.
  // biome-ignore lint/correctness/useExhaustiveDependencies: mount-only resume; ``jobId`` is checked once and only used to skip when already polling.
  useEffect(() => {
    if (jobId !== null) return;
    const tracked = jobsStorage.list().filter((j) => j.kind === BULK_UPLOAD_JOB_KIND);
    if (tracked.length === 0) return;
    setJobId(tracked[0].id);
    setPhase("polling");
  }, []);

  const cancelUpload = useCallback(async () => {
    if (!jobId || cancelling) return;
    setCancelling(true);
    try {
      await jobsApi.cancel(jobId);
      // The useJob hook will pick up status=cancelled on the next
      // tick and route into the terminal handler above.
    } catch (e) {
      // Cancellation race (job already terminal): silently fall
      // through; useJob's next poll resolves the state.
      if (!(e instanceof ApiError && e.status === 404)) {
        setErr(e instanceof Error ? e.message : t("uploadFailedUnknown"));
      }
    } finally {
      setCancelling(false);
    }
  }, [jobId, cancelling, t]);

  const totalBytes = useMemo(() => entries.reduce((acc, e) => acc + e.file.size, 0), [entries]);
  // ``sortedEntries`` is the user-facing view. We keep ``entries`` in
  // input order so a re-sort doesn't lose the original "drop order"
  // (cancelling sort returns to that). The comparator routes each
  // sort key to a string-or-number primitive and sorts deterministically.
  const sortedEntries = useMemo(() => {
    if (!sort) return entries;
    const sign = sort.dir === "asc" ? 1 : -1;
    const keyOf = (e: Entry): string | number => {
      switch (sort.key) {
        case "name":
          return e.file.name.toLowerCase();
        case "path":
          return e.relativePath.toLowerCase();
        case "type":
          return e.detected;
        case "route": {
          if (e.route.kind === "document") return `document:${e.route.document_type}`;
          return e.route.kind;
        }
        case "size":
          return e.file.size;
      }
    };
    return [...entries].sort((a, b) => {
      const ka = keyOf(a);
      const kb = keyOf(b);
      if (ka < kb) return -1 * sign;
      if (ka > kb) return 1 * sign;
      return 0;
    });
  }, [entries, sort]);
  const toggleSort = useCallback((key: "name" | "path" | "type" | "route" | "size") => {
    setSort((cur) => {
      if (!cur || cur.key !== key) return { key, dir: "asc" };
      if (cur.dir === "asc") return { key, dir: "desc" };
      return null; // third click clears the sort
    });
  }, []);
  // CD / DVD detection: the radiology disc layout is well-known
  // (DICOMDIR at the root, a DICOM/ subtree, optional companion
  // files: REFERTO.PDF / VIEWER / IHE_PDI / AUTORUN.INF / ...). The
  // backend already routes DICOMDIR-driven ingestion (see
  // services/dicom_ingest + bulk_upload._extract_dicomdir); we
  // surface the recognition in the UI so the user knows their CD is
  // about to be ingested as a CD, with the DICOMDIR ordering
  // preserved. Detection is a cheap name scan — no header reads.
  const cdShape = useMemo(() => {
    let dicomdir = false;
    let dicomFiles = 0;
    let companionPdfs = 0;
    let companionImages = 0;
    let other = 0;
    for (const e of entries) {
      const tail = (e.relativePath.split("/").pop() ?? "").toUpperCase();
      if (tail === "DICOMDIR") {
        dicomdir = true;
        continue;
      }
      if (e.detected === "dicom") {
        dicomFiles += 1;
      } else if (e.detected === "pdf") {
        companionPdfs += 1;
      } else if (e.detected === "image") {
        companionImages += 1;
      } else {
        other += 1;
      }
    }
    return { dicomdir, dicomFiles, companionPdfs, companionImages, other };
  }, [entries]);
  // The upload button needs files *and* a destination — either the
  // parent told us which patient (fascicolo flow) or the in-page
  // picker resolved one. Without a patient the bulk endpoint creates
  // orphan studies, which is rarely what the user actually wants.
  const canUpload =
    entries.length > 0 &&
    phase !== "uploading" &&
    phase !== "staging" &&
    phase !== "polling" &&
    Boolean(effectivePatientId);

  return (
    <div className="dicom-uploader">
      {!patientId && (
        <PatientFolderPicker
          patient={pickedPatient}
          folderId={pickedFolderId}
          onPickPatient={(p) => {
            setPickedPatient(p);
            setPickedFolderId(null);
          }}
          onPickFolder={setPickedFolderId}
        />
      )}
      <div
        className={`dicom-uploader__drop${dragOver ? " dicom-uploader__drop--active" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
      >
        <p className="dicom-uploader__title">{t("dropTitle")}</p>
        <p className="meta">{t("dropSubtitle")}</p>
        {extracting && (
          <p
            className="meta"
            style={{
              marginTop: "0.5rem",
              padding: "0.4rem 0.6rem",
              background: "#fff7ef",
              border: "1px solid #fcd9b3",
              borderRadius: 6,
              fontSize: "0.85rem",
            }}
          >
            {t("extractingIso")}
          </p>
        )}
        <div className="dicom-uploader__pickers">
          <button type="button" onClick={() => fileInputRef.current?.click()}>
            {t("pickFile")}
          </button>
          <button type="button" className="ghost" onClick={() => dirInputRef.current?.click()}>
            {t("pickFolder")}
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={(e) => {
            handleInputFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <input
          ref={dirInputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          {...(nonStandardDirProps as unknown as React.InputHTMLAttributes<HTMLInputElement>)}
          onChange={(e) => {
            handleInputFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {entries.length > 0 && cdShape.dicomdir && (
        <div
          className="card"
          style={{
            background: "#eef6ff",
            borderColor: "#bcd9f7",
            padding: "0.6rem 0.85rem",
            marginBottom: "0.5rem",
          }}
        >
          <strong>{t("cdRecognized")}</strong>{" "}
          <span className="meta">
            DICOMDIR + {t("cdSummaryDicom", { n: cdShape.dicomFiles })}
            {cdShape.companionPdfs > 0
              ? ` + ${t("cdSummaryPdf", { n: cdShape.companionPdfs })}`
              : ""}
            {cdShape.companionImages > 0
              ? ` + ${t("cdSummaryAccessory", { n: cdShape.companionImages })}`
              : ""}
            {cdShape.other > 0 ? ` + ${t("cdSummaryOther", { n: cdShape.other })}` : ""}.
            {scanningRemaining > 0 ? ` ${t("cdScanning", { n: scanningRemaining })}` : ""}{" "}
            {t("cdOrderHint")}
          </span>
        </div>
      )}
      {entries.length > 0 && (
        <div className="dicom-uploader__list">
          <div className="dicom-uploader__list-header">
            <strong>
              {t("filesSummary", {
                n: entries.length,
                mib: (totalBytes / 1_048_576).toFixed(1),
              })}
            </strong>
            {(phase === "idle" || phase === "error") && (
              <button type="button" className="ghost" onClick={reset}>
                {t("clear")}
              </button>
            )}
          </div>

          {(phase === "idle" || phase === "error") && (
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: "0.4rem",
                margin: "0.4rem 0",
                fontSize: "0.85rem",
              }}
            >
              <span className="meta">{t("filterTitle")}</span>
              <button
                type="button"
                className="ghost"
                style={{ fontSize: "0.78rem", padding: "0.15rem 0.55rem" }}
                onClick={() => setEntries((cur) => cur.filter((e) => e.detected === "dicom"))}
                disabled={entries.length === 0}
                title={t("filterKeepDicomTitleLong")}
              >
                {t("filterKeepDicom")}{" "}
                {t("filterCount", {
                  n: entries.filter((e) => e.detected === "dicom").length,
                })}
              </button>
              <button
                type="button"
                className="ghost"
                style={{ fontSize: "0.78rem", padding: "0.15rem 0.55rem" }}
                onClick={() =>
                  setEntries((cur) =>
                    cur.filter((e) => e.detected === "dicom" || e.detected === "pdf"),
                  )
                }
                disabled={entries.length === 0}
                title={t("filterKeepDicomPdfTitle")}
              >
                {t("filterKeepDicomPdf")}{" "}
                {t("filterCount", {
                  n: entries.filter((e) => e.detected === "dicom" || e.detected === "pdf").length,
                })}
              </button>
              <button
                type="button"
                className="ghost"
                style={{ fontSize: "0.78rem", padding: "0.15rem 0.55rem" }}
                onClick={() =>
                  setEntries((cur) =>
                    cur.filter(
                      (e) =>
                        e.detected === "dicom" ||
                        e.detected === "pdf" ||
                        e.detected === "image" ||
                        e.detected === "text",
                    ),
                  )
                }
                disabled={entries.length === 0}
                title={t("filterKeepDicomDocsTitle")}
              >
                {t("filterKeepDicomDocs")}{" "}
                {t("filterCount", {
                  n: entries.filter(
                    (e) =>
                      e.detected === "dicom" ||
                      e.detected === "pdf" ||
                      e.detected === "image" ||
                      e.detected === "text",
                  ).length,
                })}
              </button>
              <button
                type="button"
                className="ghost"
                style={{ fontSize: "0.78rem", padding: "0.15rem 0.55rem" }}
                onClick={() => setEntries((cur) => cur.filter((e) => e.detected !== "unknown"))}
                disabled={entries.length === 0}
                title="Rimuove solo i file di tipo non riconosciuto"
              >
                {t("filterRemoveUnknown")}{" "}
                {t("filterCount", {
                  n: entries.filter((e) => e.detected === "unknown").length,
                })}
              </button>
            </div>
          )}

          <div className="universal-uploader__table-wrap">
            <table className="universal-uploader__table">
              <thead>
                <tr>
                  <SortableTh
                    label={t("tableFile")}
                    sortKey="name"
                    sort={sort}
                    onToggle={toggleSort}
                  />
                  <SortableTh
                    label={t("tablePath")}
                    sortKey="path"
                    sort={sort}
                    onToggle={toggleSort}
                  />
                  <SortableTh
                    label={t("tableType")}
                    sortKey="type"
                    sort={sort}
                    onToggle={toggleSort}
                  />
                  <SortableTh
                    label={t("tableRoute")}
                    sortKey="route"
                    sort={sort}
                    onToggle={toggleSort}
                  />
                  <th aria-label="actions" />
                </tr>
              </thead>
              <tbody>
                {sortedEntries.slice(0, 200).map((entry) => (
                  <EntryRow
                    key={entry.id}
                    entry={entry}
                    disabled={phase === "uploading" || phase === "staging" || phase === "polling"}
                    onChangeRoute={(route) => updateRoute(entry.id, route)}
                    onRemove={() => removeEntry(entry.id)}
                  />
                ))}
                {entries.length > 200 && (
                  <tr>
                    <td colSpan={5} className="meta">
                      ...and {entries.length - 200} more file
                      {entries.length - 200 === 1 ? "" : "s"} (will still be uploaded)
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div
            className="dicom-uploader__controls"
            style={{
              // Sticky bottom bar: tier picker + Carica stay in view
              // even when the file list grows past the fold. The list
              // above scrolls inside the page; this control strip
              // anchors so the user never has to scroll down to act.
              position: "sticky",
              bottom: 0,
              zIndex: 2,
              background: "var(--bv-card-bg, #fff)",
              borderTop: "1px solid var(--bv-card-border, #e5e7eb)",
              padding: "0.75rem",
              marginTop: "0.5rem",
              boxShadow: "0 -6px 12px rgba(0,0,0,0.08)",
            }}
          >
            <label style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <span>{t("tierLabel")}</span>
              <select
                value={tier}
                onChange={(e) => setTier(e.target.value as ContributionTier)}
                disabled={phase === "uploading" || phase === "staging"}
                style={{ padding: "0.25rem 0.5rem" }}
              >
                <option value="t1">{t("tierT1")}</option>
                <option value="t2">{t("tierT2")}</option>
                <option value="t3">{t("tierT3")}</option>
                <option value="t4">{t("tierT4")}</option>
              </select>
            </label>
            <p
              className="meta"
              style={{ margin: "0.25rem 0 0.75rem 0" }}
              // biome-ignore lint/security/noDangerouslySetInnerHtml: source is the static i18n bundle, not user input.
              dangerouslySetInnerHTML={{
                __html: t.raw(`tier${tier.toUpperCase()}Hint`) as string,
              }}
            />
            {entries.some((e) => e.file.name.toLowerCase().endsWith(".iso")) && (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.25rem",
                  marginBottom: "0.75rem",
                  padding: "0.5rem 0.75rem",
                  background: "var(--bv-card-bg, #fff)",
                  border: "1px solid var(--bv-card-border, #e5e7eb)",
                  borderRadius: "var(--bv-r-sm, 4px)",
                  fontSize: "0.85rem",
                }}
              >
                <label style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                  <input
                    type="checkbox"
                    checked={keepIsoArchive}
                    onChange={(e) => setKeepIsoArchive(e.target.checked)}
                    disabled={phase === "uploading" || phase === "staging"}
                  />
                  <span>{t("isoKeepArchive")}</span>
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                  <input
                    type="checkbox"
                    checked={wrapIsoInFolder}
                    onChange={(e) => setWrapIsoInFolder(e.target.checked)}
                    disabled={phase === "uploading" || phase === "staging" || !extractIsoContents}
                  />
                  <span>{t("isoWrapInFolder")}</span>
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                  <input
                    type="checkbox"
                    checked={extractIsoContents}
                    onChange={(e) => setExtractIsoContents(e.target.checked)}
                    disabled={phase === "uploading" || phase === "staging"}
                  />
                  <span>{t("isoExtractContents")}</span>
                </label>
                {!extractIsoContents && !keepIsoArchive && (
                  <p
                    className="meta"
                    style={{
                      margin: "0.25rem 0 0 0",
                      fontSize: "0.78rem",
                      color: "var(--bv-warning, #b45309)",
                    }}
                  >
                    {t("isoNothingToDo")}
                  </p>
                )}
              </div>
            )}
            <button type="button" onClick={upload} disabled={!canUpload}>
              {phase === "uploading"
                ? t("uploadingButton", { progress })
                : phase === "staging"
                  ? t("processingButton")
                  : phase === "polling"
                    ? t("pollingButton")
                    : t("uploadButton", { n: entries.length, tier: tier.toUpperCase() })}
            </button>
            {!effectivePatientId && (
              <p
                className="meta"
                style={{
                  marginTop: "0.4rem",
                  fontSize: "0.82rem",
                  color: "var(--bv-warning, #b45309)",
                }}
              >
                {t("uploadDisabledNoPatient")}
              </p>
            )}
            {effectivePatientId && entries.length === 0 && (
              <p className="meta" style={{ marginTop: "0.4rem", fontSize: "0.82rem" }}>
                {t("dropSubtitle")}
              </p>
            )}
          </div>

          {phase === "uploading" && (
            <div className="dicom-uploader__progress">
              <div
                className="dicom-uploader__progress-bar"
                style={{ width: `${progress}%` }}
                aria-valuenow={progress}
                aria-valuemin={0}
                aria-valuemax={100}
                role="progressbar"
                tabIndex={-1}
              />
            </div>
          )}
          {phase === "staging" && (
            // Distinct from "polling": no Job exists yet, the multipart
            // POST is still open. We use a warmer styling than polling
            // to make clear this is the *one* phase the user must NOT
            // interrupt — closing the tab here aborts the upload and
            // throws away the bytes already sent.
            <div
              style={{
                marginTop: "0.5rem",
                padding: "0.6rem 0.75rem",
                background: "#fff7ed",
                border: "1px solid #fdba74",
                borderRadius: 6,
                display: "flex",
                flexDirection: "column",
                gap: "0.5rem",
              }}
              // biome-ignore lint/a11y/useSemanticElements: aria-live status region; <output> would change form-related semantics.

              role="status"
              aria-live="polite"
            >
              <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                <span
                  aria-hidden
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: "50%",
                    border: "2px solid #fdba74",
                    borderTopColor: "#c2410c",
                    animation: "bv-spin 0.8s linear infinite",
                    display: "inline-block",
                    flex: "0 0 auto",
                  }}
                />
                <span style={{ fontSize: "0.9rem", fontWeight: 500 }}>{t("processingButton")}</span>
              </div>
              <p className="meta" style={{ fontSize: "0.78rem", margin: 0 }}>
                {t("processingHint")}
              </p>
            </div>
          )}
          {phase === "polling" && (
            <div
              style={{
                marginTop: "0.5rem",
                padding: "0.6rem 0.75rem",
                background: "#eef6ff",
                border: "1px solid #bcd9f7",
                borderRadius: 6,
                display: "flex",
                flexDirection: "column",
                gap: "0.5rem",
              }}
              // biome-ignore lint/a11y/useSemanticElements: aria-live status region; <output> would change form-related semantics.

              role="status"
              aria-live="polite"
            >
              <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                <span
                  aria-hidden
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: "50%",
                    border: "2px solid #bcd9f7",
                    borderTopColor: "#1d4ed8",
                    animation: "bv-spin 0.8s linear infinite",
                    display: "inline-block",
                    flex: "0 0 auto",
                  }}
                />
                <span style={{ fontSize: "0.9rem", fontWeight: 500 }}>
                  {(() => {
                    if (!job) return t("pollingResumed");
                    if (job.status === "queued") return t("pollingStageQueued");
                    const stageKey = job.stage ?? "";
                    const stageLabel =
                      stageKey === "downloading"
                        ? t("pollingStageDownloading")
                        : stageKey === "ingest_dicom"
                          ? t("pollingStageIngestDicom")
                          : stageKey === "ingest_other"
                            ? t("pollingStageIngestOther")
                            : stageKey === "finalize"
                              ? t("pollingStageFinalize")
                              : t("pollingStageRunning");
                    if (job.progress_total && job.progress_total > 0) {
                      return t("pollingProgress", {
                        stage: stageLabel,
                        done: job.progress_done,
                        total: job.progress_total,
                      });
                    }
                    return t("pollingProgressNoTotal", { stage: stageLabel });
                  })()}
                </span>
              </div>
              {job && job.progress_total !== null && job.progress_total > 0 && (
                <div className="dicom-uploader__progress" style={{ height: 6 }}>
                  <div
                    className="dicom-uploader__progress-bar"
                    style={{
                      width: `${Math.round((job.progress_done / job.progress_total) * 100)}%`,
                    }}
                    role="progressbar"
                    tabIndex={-1}
                    aria-valuenow={job.progress_done}
                    aria-valuemin={0}
                    aria-valuemax={job.progress_total}
                  />
                </div>
              )}
              <p className="meta" style={{ fontSize: "0.78rem", margin: 0 }}>
                {t("pollingHint")}
              </p>
              <div style={{ display: "flex", gap: "0.5rem" }}>
                <button
                  type="button"
                  className="ghost"
                  onClick={cancelUpload}
                  disabled={cancelling || !jobId}
                  style={{ fontSize: "0.82rem" }}
                >
                  {cancelling ? t("cancellingButton") : t("cancelButton")}
                </button>
              </div>
              <style>{"@keyframes bv-spin { to { transform: rotate(360deg); } }"}</style>
            </div>
          )}
        </div>
      )}

      {err && <p className="error">{err}</p>}

      {summary && phase === "done" && (
        <div className="dicom-uploader__summary card">
          <h3>{t("uploadComplete")}</h3>
          <ul>
            <li>
              <strong>{t("totalFilesUploaded", { n: summary.total_files })}</strong>
            </li>
            <li>{t("studiesCreated", { n: summary.studies_created.length })}</li>
            <li>{t("documentsCreated", { n: summary.documents_created.length })}</li>
            {summary.skipped.length > 0 && (
              <li>{t("skippedSummary", { n: summary.skipped.length })}</li>
            )}
            {summary.errors.length > 0 && (
              <li className="error">
                {t("errorsSummary", { n: summary.errors.length })}{" "}
                <button type="button" className="ghost" onClick={() => setShowErrors((v) => !v)}>
                  {showErrors ? t("hideErrors") : t("showErrors")}
                </button>
                {showErrors && (
                  <ul>
                    {summary.errors.slice(0, 20).map((e) => (
                      <li key={`${e.filename}:${e.message}`}>
                        <code>{e.filename}</code>: {e.message}
                      </li>
                    ))}
                    {summary.errors.length > 20 && (
                      <li className="meta">{t("andMore", { n: summary.errors.length - 20 })}</li>
                    )}
                  </ul>
                )}
              </li>
            )}
          </ul>
          <div className="dicom-uploader__summary-actions">
            <button type="button" className="ghost" onClick={reset}>
              {t("uploadMore")}
            </button>
            {summary.studies_created.length > 0 && (
              <a href={`/studies/${summary.studies_created[0].id}`} className="button-like">
                {t("openStudy")} →
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Click-to-sort table header. Tristate: asc → desc → off.
 * Renders ↑ / ↓ next to the active key; an empty string for the
 * inactive ones so the column widths don't jump as the user toggles.
 */
function SortableTh({
  label,
  sortKey,
  sort,
  onToggle,
}: {
  label: string;
  sortKey: "name" | "path" | "type" | "route" | "size";
  sort: { key: string; dir: "asc" | "desc" } | null;
  onToggle: (k: "name" | "path" | "type" | "route" | "size") => void;
}) {
  const active = sort?.key === sortKey;
  const arrow = active ? (sort?.dir === "asc" ? " ↑" : " ↓") : "";
  return (
    <th
      style={{ userSelect: "none", padding: 0 }}
      aria-sort={active ? (sort?.dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        onClick={() => onToggle(sortKey)}
        title="Clicca per ordinare; clicca ancora per invertire o annullare"
        style={{
          width: "100%",
          background: "transparent",
          border: "none",
          padding: "inherit",
          font: "inherit",
          color: "inherit",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        {label}
        <span style={{ opacity: active ? 1 : 0.3 }}>{arrow || " ⇅"}</span>
      </button>
    </th>
  );
}

interface EntryRowProps {
  entry: Entry;
  disabled: boolean;
  onChangeRoute: (route: SuggestedRoute) => void;
  onRemove: () => void;
}

function kindBadge(detected: DetectedKind): { icon: string; label: string } {
  switch (detected) {
    case "dicom":
      return { icon: "[DCM]", label: "DICOM" };
    case "pdf":
      return { icon: "[PDF]", label: "PDF" };
    case "image":
      return { icon: "[IMG]", label: "Image" };
    case "archive":
      return { icon: "[ZIP]", label: "Archive" };
    case "text":
      return { icon: "[TXT]", label: "Text/XML" };
    default:
      return { icon: "[?]", label: "Unknown" };
  }
}

// Encode the route into the <select>'s single string value so we can
// round-trip structured data without a separate decoded-state layer.
function routeToValue(route: SuggestedRoute): string {
  switch (route.kind) {
    case "study":
      return "study";
    case "archive":
      return "archive";
    case "skip":
      return "skip";
    case "document":
      return `document:${route.document_type}`;
  }
}

function valueToRoute(value: string): SuggestedRoute {
  if (value === "study") return { kind: "study" };
  if (value === "archive") return { kind: "archive" };
  if (value === "skip") return { kind: "skip", reason: "user skipped" };
  if (value.startsWith("document:")) {
    return { kind: "document", document_type: value.slice("document:".length) };
  }
  return { kind: "skip", reason: "unknown routing" };
}

function EntryRow({ entry, disabled, onChangeRoute, onRemove }: EntryRowProps) {
  const locale = useLocale();
  const { catalog } = useDocumentCatalog();
  const activeKinds = (catalog?.kinds ?? []).filter((k) => k.is_active);
  const { icon, label } = kindBadge(entry.detected);
  // DICOM and ZIP routing is server-decided; only PDF/IMG/Unknown expose
  // a dropdown (document-type + a "skip" escape hatch).
  const readOnlyRouting = entry.detected === "dicom" || entry.detected === "archive";
  const pathLabel = entry.relativePath === entry.file.name ? "root" : entry.relativePath;

  return (
    <tr>
      <td title={entry.file.name}>
        <span className="universal-uploader__fname">{entry.file.name}</span>
      </td>
      <td className="meta" title={pathLabel}>
        {pathLabel}
      </td>
      <td>
        <span className="universal-uploader__kind">
          <span className="universal-uploader__kind-icon">{icon}</span> {label}
        </span>
      </td>
      <td>
        {readOnlyRouting ? (
          <span className="meta">
            {entry.route.kind === "study" ? "Study" : "Archive (server unpacks)"}
          </span>
        ) : (
          <select
            value={routeToValue(entry.route)}
            disabled={disabled}
            onChange={(e) => onChangeRoute(valueToRoute(e.target.value))}
          >
            {activeKinds.map((k) => (
              <option key={k.id} value={`document:${k.id}`}>
                Document · {entryLabel(k, locale)}
              </option>
            ))}
            <option value="skip">Skip (don't upload)</option>
          </select>
        )}
      </td>
      <td>
        {!disabled && (
          <button
            type="button"
            className="ghost dicom-uploader__remove"
            onClick={onRemove}
            aria-label={`Remove ${entry.file.name}`}
          >
            ×
          </button>
        )}
      </td>
    </tr>
  );
}

/**
 * Destination picker shown above the drop zone when the parent did
 * not pre-supply a patient id. Two stages:
 *
 * 1. Patient — debounced search against ``GET /api/patients?q=...``
 *    plus a "Crea nuovo paziente" link to the registration form.
 *    Selecting a row resolves the destination.
 * 2. Folder — once a patient is picked, list the folders at the root
 *    of that patient's tree (deeper sub-folders are reachable from
 *    the fascicolo itself; mixing nested folders into the upload UI
 *    would crowd it). The default is "(no folder)" which lands the
 *    new resources at the patient root.
 */
function PatientFolderPicker({
  patient,
  folderId,
  onPickPatient,
  onPickFolder,
}: {
  patient: Patient | null;
  folderId: string | null;
  onPickPatient: (p: Patient | null) => void;
  onPickFolder: (id: string | null) => void;
}) {
  const t = useTranslations("upload");
  const modal = useModal();
  const [q, setQ] = useState("");
  const [browseOpen, setBrowseOpen] = useState(false);
  const [folderBrowseOpen, setFolderBrowseOpen] = useState(false);
  const [foldersBumpKey, setFoldersBumpKey] = useState(0);
  const [results, setResults] = useState<Patient[]>([]);
  const [searching, setSearching] = useState(false);
  const [folders, setFolders] = useState<TreeNode[] | null>(null);
  const [foldersErr, setFoldersErr] = useState<string | null>(null);

  // Debounced search — fires after the user stops typing. ``scope=all``
  // so a medico can target any patient they have permission to write
  // to, not just their personal slice.
  useEffect(() => {
    if (patient) return; // search hidden once a patient is locked in
    if (q.trim().length < 2) {
      setResults([]);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const handle = setTimeout(async () => {
      try {
        const resp = await patientsApi.list({ q: q.trim(), scope: "all", limit: 8 });
        if (!cancelled) setResults(resp.items);
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 220);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [q, patient]);

  // Folder list — reload whenever the picked patient changes or a
  // new folder is created inline (``foldersBumpKey``).
  // biome-ignore lint/correctness/useExhaustiveDependencies: ``foldersBumpKey`` is the manual-refresh trigger after inline folder creation.
  useEffect(() => {
    if (!patient) {
      setFolders(null);
      return;
    }
    let cancelled = false;
    patientTreeApi
      .tree(patient.id, "/")
      .then((listing) => {
        if (!cancelled) {
          setFolders(listing.nodes.filter((n) => n.type === "folder"));
          setFoldersErr(null);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setFolders([]);
          setFoldersErr(e instanceof ApiError ? e.message : "load failed");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [patient, foldersBumpKey]);

  const handleNewFolder = useCallback(async () => {
    if (!patient) return;
    const name = await modal.prompt({
      title: t("newFolderPromptTitle"),
      label: t("newFolderPromptLabel"),
    });
    const trimmed = name?.trim();
    if (!trimmed) return;
    try {
      const created = await patientTreeApi.createFolder(patient.id, null, trimmed);
      setFoldersBumpKey((k) => k + 1);
      // Auto-pick the new folder so the upload destination is set
      // to the user's freshly created bucket.
      onPickFolder(created.id);
    } catch (e) {
      // Surface via the same error channel as the folder list load.
      // ``setFoldersErr`` is the closest visible error slot.
      setFoldersErr(e instanceof ApiError ? e.message : "create failed");
    }
  }, [patient, modal, onPickFolder, t]);

  return (
    <div className="card" style={{ padding: "0.75rem 1rem", marginBottom: "0.75rem" }}>
      <strong style={{ display: "block", marginBottom: "0.4rem" }}>{t("destinationTitle")}</strong>
      {!patient ? (
        <>
          <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
            <input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("patientSearchPlaceholder")}
              style={{ flex: 1 }}
            />
            <button
              type="button"
              className="ghost"
              onClick={() => setBrowseOpen(true)}
              title={t("browsePatientsTitle")}
            >
              {t("browsePatients")}
            </button>
          </div>
          {q.trim().length >= 2 && (
            <div style={{ marginTop: "0.4rem" }}>
              {searching && (
                <p className="meta" style={{ fontSize: "0.8rem" }}>
                  {t("patientSearching")}
                </p>
              )}
              {!searching && results.length === 0 && (
                <p className="meta" style={{ fontSize: "0.8rem" }}>
                  {t("patientNoMatch")}{" "}
                  <Link href={`/patients/new?return=${encodeURIComponent("/upload")}`}>
                    {t("patientCreateNew")}
                  </Link>
                </p>
              )}
              {results.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className="ghost"
                  onClick={() => onPickPatient(p)}
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    padding: "0.35rem 0.55rem",
                    marginTop: "0.2rem",
                    fontSize: "0.88rem",
                  }}
                >
                  <strong>{p.display_name}</strong>
                  {p.birth_date ? (
                    <span className="meta" style={{ marginLeft: "0.5rem" }}>
                      {p.birth_date}
                    </span>
                  ) : null}
                  {p.tax_id ? (
                    <span className="meta" style={{ marginLeft: "0.5rem" }}>
                      CF {p.tax_id}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          )}
          {q.trim().length < 2 && (
            <p className="meta" style={{ fontSize: "0.8rem", marginTop: "0.4rem" }}>
              {t("patientShortQuery")}{" "}
              <Link href={`/patients/new?return=${encodeURIComponent("/upload")}`}>
                {t("patientCreateNew")}
              </Link>
            </p>
          )}
        </>
      ) : (
        <>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
              flexWrap: "wrap",
            }}
          >
            <span>
              {t("patientPickedLabel")} <strong>{patient.display_name}</strong>
            </span>
            <button
              type="button"
              className="ghost"
              onClick={() => onPickPatient(null)}
              style={{ fontSize: "0.78rem" }}
            >
              {t("patientChange")}
            </button>
          </div>
          <div style={{ marginTop: "0.5rem" }}>
            <label style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
              <span className="meta">{t("folderLabel")}</span>
              <select
                value={folderId ?? ""}
                onChange={(e) => onPickFolder(e.target.value || null)}
                style={{ flex: 1 }}
              >
                <option value="">{t("folderRoot")}</option>
                {folders?.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="ghost"
                onClick={() => setFolderBrowseOpen(true)}
                title={t("browseFoldersTitle")}
              >
                {t("browseFolders")}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={handleNewFolder}
                title={t("newFolderPromptTitle")}
              >
                {t("newFolderInline")}
              </button>
            </label>
            {foldersErr && (
              <p className="error" style={{ fontSize: "0.8rem", marginTop: "0.3rem" }}>
                {foldersErr}
              </p>
            )}
          </div>
        </>
      )}
      {browseOpen && (
        <PatientBrowseModal
          onPick={(p) => {
            onPickPatient(p);
            setBrowseOpen(false);
          }}
          onClose={() => setBrowseOpen(false)}
        />
      )}
      {folderBrowseOpen && patient && (
        <FolderBrowseModal
          patientId={patient.id}
          onPick={(id) => {
            onPickFolder(id);
            setFolderBrowseOpen(false);
          }}
          onClose={() => setFolderBrowseOpen(false)}
        />
      )}
    </div>
  );
}

/**
 * Finder-style patient browser. Backdrop + card layout matches
 * ``ModalHost`` so the visual contract is consistent across the app.
 * The list is "all" scope — a clinician shouldn't be limited to
 * "personal" when picking an upload destination.
 */
function PatientBrowseModal({
  onPick,
  onClose,
}: {
  onPick: (p: Patient) => void;
  onClose: () => void;
}) {
  const t = useTranslations("upload");
  const [items, setItems] = useState<Patient[] | null>(null);
  const [q, setQ] = useState("");

  useEffect(() => {
    let cancelled = false;
    const handle = setTimeout(
      async () => {
        try {
          const resp = await patientsApi.list({
            q: q.trim() || undefined,
            scope: "all",
            limit: 50,
          });
          if (!cancelled) setItems(resp.items);
        } catch {
          if (!cancelled) setItems([]);
        }
      },
      q ? 200 : 0,
    );
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [q]);

  return (
    <NativeDialog open onClose={onClose} ariaLabel={t("browsePatientsTitle")} className="bv-dialog">
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, inherit)",
          border: "1px solid var(--bv-card-border, #d0d5dd)",
          borderRadius: 10,
          boxShadow: "0 18px 42px rgba(0,0,0,0.35)",
          padding: "1rem 1.25rem",
          width: "min(680px, 95%)",
          maxHeight: "80vh",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>{t("browsePatientsTitle")}</h2>
        <input
          type="search"
          // biome-ignore lint/a11y/noAutofocus: modal opened by the user to search; the field is the modal's reason to exist.
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("patientSearchPlaceholder")}
          style={{ width: "100%", marginBottom: "0.5rem" }}
        />
        <div style={{ flex: 1, overflowY: "auto" }}>
          {items === null && <p className="meta">…</p>}
          {items !== null && items.length === 0 && <p className="meta">{t("patientNoMatch")}</p>}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
              gap: "0.5rem",
              marginTop: "0.4rem",
            }}
          >
            {items?.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => onPick(p)}
                className="card"
                style={{
                  textAlign: "left",
                  cursor: "pointer",
                  padding: "0.6rem 0.75rem",
                  background: "var(--bv-card-bg, #fff)",
                  color: "inherit",
                  border: "1px solid var(--bv-card-border, #e5e7eb)",
                }}
              >
                <strong style={{ display: "block" }}>{p.display_name}</strong>
                <span className="meta" style={{ fontSize: "0.78rem" }}>
                  {p.birth_date ?? "—"}
                  {p.tax_id ? ` · ${p.tax_id}` : ""}
                  {p.external_id ? ` · ${p.external_id}` : ""}
                </span>
              </button>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "0.75rem" }}>
          <button type="button" className="ghost" onClick={onClose}>
            {t("browsePatientsClose")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}

/**
 * Tree-walk folder browser scoped to one patient. Click a folder row
 * to descend into it; breadcrumb at the top to walk back; "Use this
 * folder" button to commit the current location as the upload
 * destination (or the patient root when at the breadcrumb root).
 */
function FolderBrowseModal({
  patientId,
  onPick,
  onClose,
}: {
  patientId: string;
  onPick: (folderId: string | null) => void;
  onClose: () => void;
}) {
  const t = useTranslations("upload");
  const [path, setPath] = useState("/");
  const [breadcrumb, setBreadcrumb] = useState<{ label: string; path: string }[]>([]);
  const [folders, setFolders] = useState<TreeNode[]>([]);
  const [currentFolderId, setCurrentFolderId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    patientTreeApi
      .tree(patientId, path)
      .then((listing) => {
        if (cancelled) return;
        setFolders(listing.nodes.filter((n) => n.type === "folder"));
        setBreadcrumb(listing.breadcrumb.map((b) => ({ label: b.name, path: b.path })));
        setCurrentFolderId(listing.folder_id ?? null);
      })
      .catch(() => {
        if (!cancelled) setFolders([]);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, path]);

  return (
    <NativeDialog open onClose={onClose} ariaLabel={t("browseFoldersTitle")} className="bv-dialog">
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, inherit)",
          border: "1px solid var(--bv-card-border, #d0d5dd)",
          borderRadius: 10,
          boxShadow: "0 18px 42px rgba(0,0,0,0.35)",
          padding: "1rem 1.25rem",
          width: "min(540px, 95%)",
          maxHeight: "80vh",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>{t("browseFoldersTitle")}</h2>
        <nav
          aria-label="breadcrumb"
          style={{
            fontSize: "0.85rem",
            marginBottom: "0.5rem",
            display: "flex",
            flexWrap: "wrap",
            gap: "0.25rem",
            alignItems: "center",
          }}
        >
          <button
            type="button"
            className="ghost"
            onClick={() => setPath("/")}
            style={{ padding: "0.1rem 0.4rem" }}
          >
            {t("breadcrumbRoot")}
          </button>
          {breadcrumb.map((b) => (
            <span
              key={b.path}
              style={{ display: "inline-flex", gap: "0.25rem", alignItems: "center" }}
            >
              <span aria-hidden>/</span>
              <button
                type="button"
                className="ghost"
                onClick={() => setPath(b.path)}
                style={{ padding: "0.1rem 0.4rem" }}
              >
                {b.label}
              </button>
            </span>
          ))}
        </nav>
        <div style={{ flex: 1, overflowY: "auto" }}>
          {path !== "/" && (
            <button
              type="button"
              onClick={() => {
                const segs = path.split("/").filter(Boolean);
                const parent = segs.length <= 1 ? "/" : `/${segs.slice(0, -1).join("/")}`;
                setPath(parent);
              }}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "0.4rem 0.6rem",
                marginBottom: "0.2rem",
                background: "transparent",
                border: "1px dashed var(--bv-card-border, #e5e7eb)",
                borderRadius: 6,
                cursor: "pointer",
                font: "inherit",
                color: "inherit",
                fontStyle: "italic",
              }}
              title={t("parentFolderTitle")}
            >
              {t("parentFolderLabel")}
            </button>
          )}
          {folders.length === 0 && (
            <p className="meta" style={{ fontSize: "0.85rem" }}>
              {t("noSubfolders")}
            </p>
          )}
          {folders.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setPath(f.path ?? path + (path.endsWith("/") ? "" : "/") + f.name)}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "0.4rem 0.6rem",
                marginBottom: "0.2rem",
                background: "transparent",
                border: "1px solid var(--bv-card-border, #e5e7eb)",
                borderRadius: 6,
                cursor: "pointer",
                font: "inherit",
                color: "inherit",
              }}
            >
              📁 {f.name}
            </button>
          ))}
        </div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            marginTop: "0.75rem",
            gap: "0.5rem",
          }}
        >
          <button type="button" className="ghost" onClick={onClose}>
            {t("browsePatientsClose")}
          </button>
          <button type="button" onClick={() => onPick(currentFolderId)}>
            {t("useThisFolder")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}
