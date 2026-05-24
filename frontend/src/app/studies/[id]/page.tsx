"use client";

// Legacy non-namespaced route. The canonical form is
// ``/patients/:pid/studies/:sid`` so the URL itself is a guarantee that
// the study belongs to the active patient (cross-patient inexpressible
// by construction). This file resolves the study's ``patient_id`` and
// permanent-redirects to the namespaced URL, preserving the original
// query string so back-link breadcrumbs (``?from=...``, ``?ctx=...``,
// ``?reportId=...``) survive the hop.
//
// We resolve client-side because the API requires a JWT pulled from
// localStorage; a server component cannot authenticate on the user's
// behalf. The interstitial render is intentionally minimal: a labelled
// spinner is enough — most redirects complete in a single round-trip
// and the user never sees this page in steady state.
//
// On 404 we fall through to ``StudyDetailContent`` so the legacy URL
// still renders something useful: callers that arrive with a stale
// study id (typo, deleted resource) get the canonical "study not found"
// surface instead of an empty redirect spinner.

import { useTranslations } from "next-intl";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import StudyDetailContent from "@/components/StudyDetailContent";
import { ApiError, studiesApi } from "@/lib/api";

export default function StudyLegacyRedirectPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const t = useTranslations("study");
  const [resolved, setResolved] = useState<"redirecting" | "fallback" | "error">("redirecting");

  useEffect(() => {
    let cancelled = false;
    studiesApi
      .detail(params.id)
      .then((study) => {
        if (cancelled) return;
        if (!study.patient_id) {
          // Orphan study (no patient association). Render in place
          // with the legacy back-link rather than redirecting nowhere.
          setResolved("fallback");
          return;
        }
        const qs = searchParams.toString();
        const target = `/patients/${study.patient_id}/studies/${params.id}${qs ? `?${qs}` : ""}`;
        router.replace(target);
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          setResolved("fallback");
          return;
        }
        setResolved("error");
      });
    return () => {
      cancelled = true;
    };
  }, [params.id, router, searchParams]);

  if (resolved === "fallback") {
    return <StudyDetailContent studyId={params.id} />;
  }
  if (resolved === "error") {
    return (
      <main>
        <p className="error">{t("loadFailed")}</p>
      </main>
    );
  }
  return (
    <main>
      <p className="meta">{t("redirecting")}</p>
    </main>
  );
}
