// Persists the in-flight resumable upload session so a tab close / crash /
// reload can resume it. Mirrors the jobsStorage pattern in ./jobs.ts but holds
// a SINGLE record (this UI uploads one batch at a time, not last-N).
//
// We persist only the session id + the file manifest (filename / relativePath
// / declaredSize / fileIndex) — NOT the per-file received_offset, which is
// authoritative server-side and survives a client crash. On resume we GET the
// session for the live offsets and re-match the user's re-selected File
// handles (lost across a reload) to their slots by (filename, relativePath,
// declaredSize); only the un-acked tail re-uploads.

import type { ContributionTier } from "./api";

const KEY = "bvp.uploadSession.v1";

export interface PersistedSessionFile {
  fileIndex: number;
  filename: string;
  relativePath: string;
  declaredSize: number;
}

export interface PersistedUploadSession {
  sessionId: string;
  chunkSize: number;
  declaredTotalBytes: number;
  patientId: string | null;
  folderId: string | null;
  tier: ContributionTier;
  files: PersistedSessionFile[];
  createdAt: string;
}

export const uploadSessionStorage = {
  get(): PersistedUploadSession | null {
    if (typeof window === "undefined") return null;
    try {
      const raw = window.localStorage.getItem(KEY);
      if (!raw) return null;
      const rec = JSON.parse(raw) as PersistedUploadSession;
      if (!rec || typeof rec.sessionId !== "string" || !Array.isArray(rec.files)) return null;
      return rec;
    } catch {
      return null;
    }
  },
  set(rec: PersistedUploadSession): void {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(KEY, JSON.stringify(rec));
    } catch {
      // Quota exceeded / storage disabled (private mode). The upload still
      // works in this tab; only cross-reload resume is lost. Match jobsStorage
      // which also swallows write failures.
    }
  },
  clear(): void {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.removeItem(KEY);
    } catch {
      /* ignore */
    }
  },
};
