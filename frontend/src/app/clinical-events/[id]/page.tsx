"use client";

// Legacy non-namespaced route. The canonical form is
// ``/patients/:pid/clinical-events/:eid`` so the URL itself proves the
// event belongs to the active patient. Resolve ``patient_id`` and
// permanent-redirect, preserving the original query string so back-link
// breadcrumbs (``?from=...``) survive the hop.
//
// Client-side resolution because the API needs a JWT pulled from
// localStorage; a server component cannot authenticate on the user's
// behalf.

import { useTranslations } from "next-intl";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import ClinicalEventContent from "@/components/ClinicalEventContent";
import { ApiError } from "@/lib/api";
import { clinicalEventsApi } from "@/lib/api_records";

export default function ClinicalEventLegacyRedirectPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const t = useTranslations("clinicalEventDetail");
  const [resolved, setResolved] = useState<"redirecting" | "fallback" | "error">("redirecting");

  useEffect(() => {
    let cancelled = false;
    clinicalEventsApi
      .read(params.id)
      .then((event) => {
        if (cancelled) return;
        if (!event.patient_id) {
          setResolved("fallback");
          return;
        }
        const qs = searchParams.toString();
        const target = `/patients/${event.patient_id}/clinical-events/${params.id}${qs ? `?${qs}` : ""}`;
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
    return <ClinicalEventContent eventId={params.id} />;
  }
  if (resolved === "error") {
    return (
      <main>
        <p className="error">{t("errorLoad")}</p>
      </main>
    );
  }
  return (
    <main>
      <p className="meta">{t("redirecting")}</p>
    </main>
  );
}
