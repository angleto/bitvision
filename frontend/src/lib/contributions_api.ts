// Public-contribution review queue (OpenData publish quarantine). Mirrors the
// backend /api/contributions/* surface. Accept/reject are If-Match-conditional
// (the engine bumps the submission etag on every transition); accept is
// admin-only + human-only server-side.

import { request } from "@/lib/api";

export interface ContributionInstance {
  instance_id: string | null;
  name: string | null;
  pixel_phi_risk: string | null;
}

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
};
