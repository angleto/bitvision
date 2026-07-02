// Public-contribution review queue (OpenData publish quarantine). Mirrors the
// backend /api/contributions/* surface. Accept/reject are If-Match-conditional
// (the engine bumps the submission etag on every transition); accept is
// admin-only + human-only server-side.

import { API_BASE_URL, request } from "@/lib/api";

export interface ContributionInstance {
  instance_id: string | null;
  name: string | null;
  pixel_phi_risk: string | null;
}

// Ground-truth PHI box (M6c). Matches the backend GtBox schema exactly:
// intrinsic pixel XYWH, top-left origin. `category` is a PhiCategory string.
export interface GtBox {
  x: number;
  y: number;
  w: number;
  h: number;
  text: string;
  category: string;
}

export interface GtBoxesResult {
  instance_id: string;
  boxes: GtBox[];
  etag: string;
}

export interface DetectedBoxesResult {
  instance_id: string;
  width: number;
  height: number;
  risk_level: string;
  residual_suspect: boolean;
  boxes: Array<{ x: number; y: number; w: number; h: number; text: string; conf: number }>;
}

export interface GtScoreResult {
  instance_id: string;
  recall: number;
  covered: number;
  total: number;
  missed: string[];
  risk_level: string;
}

// The PHI categories the backend accepts (services.pixel_deid_eval). Kept in
// sync with api/contributions.PHI_CATEGORIES.
export const PHI_CATEGORIES = [
  "name",
  "codice_fiscale",
  "date",
  "address",
  "phone",
  "email",
  "mrn",
  "other",
  "unknown",
] as const;
export type PhiCategory = (typeof PHI_CATEGORIES)[number];

export interface ContributionSubmission {
  id: string;
  status: string;
  auto_verdict: string | null;
  auto_checks: Record<string, unknown> | null;
  target_tier: string;
  source_study_id: string | null;
  contributor_subject_id: string | null;
  instance_count: number;
  instances: ContributionInstance[];
  created_at: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  etag: string;
}

export interface ContributionDecision {
  submission: ContributionSubmission;
  dry_run: boolean;
}

export const contributionsApi = {
  queue: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.offset != null) q.set("offset", String(params.offset));
    const query = q.toString();
    return request<ContributionSubmission[]>(`/api/contributions/queue${query ? `?${query}` : ""}`);
  },
  get: (id: string) => request<ContributionSubmission>(`/api/contributions/${id}`),
  accept: (id: string, etag: string, reason: string) =>
    request<ContributionDecision>(`/api/contributions/${id}/accept`, {
      method: "POST",
      json: { reason },
      headers: { "If-Match": etag },
    }),
  reject: (id: string, etag: string, reason: string) =>
    request<ContributionDecision>(`/api/contributions/${id}/reject`, {
      method: "POST",
      json: { reason },
      headers: { "If-Match": etag },
    }),

  // --- M6c box-labeling: render + detected boxes + GT store + recall score ---
  // The rendered instance is served as a PNG; it exposes burned-in PHI to the
  // authorised reviewer (no-store, admin-gated). `variant=original` is the
  // labeling surface; `variant=redacted` shows the automatic redaction.
  renderUrl: (submissionId: string, instanceId: string, variant: "original" | "redacted") =>
    `${API_BASE_URL}/api/contributions/${submissionId}/instances/${encodeURIComponent(
      instanceId,
    )}/render.png?variant=${variant}`,
  detectedBoxes: (submissionId: string, instanceId: string) =>
    request<DetectedBoxesResult>(
      `/api/contributions/${submissionId}/instances/${encodeURIComponent(instanceId)}/detected-boxes`,
    ),
  getGtBoxes: (submissionId: string, instanceId: string) =>
    request<GtBoxesResult>(
      `/api/contributions/${submissionId}/instances/${encodeURIComponent(instanceId)}/gt-boxes`,
    ),
  saveGtBoxes: (submissionId: string, instanceId: string, boxes: GtBox[], etag: string) =>
    request<GtBoxesResult>(
      `/api/contributions/${submissionId}/instances/${encodeURIComponent(instanceId)}/gt-boxes`,
      { method: "PUT", json: { boxes }, headers: { "If-Match": etag } },
    ),
  gtScore: (submissionId: string, instanceId: string) =>
    request<GtScoreResult>(
      `/api/contributions/${submissionId}/instances/${encodeURIComponent(instanceId)}/gt-score`,
    ),
};
