"use client";

// Legacy standalone "Timeline eventi clinici" page. The same view
// now lives as a tab inside the patient Health Record
// (``FascicoloViewToggle``, ``view=events``); the standalone variant
// duplicated UI chrome and was hard-coded in Italian. Kept as a
// redirect so that existing bookmarks and inbound links continue to
// land on the canonical view. Forwards ``?kind=`` so deep-linked
// filters survive the consolidation.

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

export default function ClinicalEventsRedirect() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const search = useSearchParams();

  useEffect(() => {
    const qs = new URLSearchParams();
    qs.set("view", "events");
    const kind = search.get("kind");
    if (kind) qs.set("kind", kind);
    router.replace(`/patients/${params.id}?${qs.toString()}`);
  }, [params.id, router, search]);

  return null;
}
