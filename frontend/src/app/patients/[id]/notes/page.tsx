"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";

// The legacy /notes view has been merged into the patient page as the
// "Evidenze e sintesi" collapsible. Old in-app links and bookmarks
// land here and bounce to /patients/[id], preserving any #note-<id>
// hash so the evidence row scroll-into-view still triggers.
export default function NotesRedirect() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  useEffect(() => {
    if (!params?.id) return;
    const hash = typeof window !== "undefined" ? window.location.hash : "";
    router.replace(`/patients/${params.id}${hash}`);
  }, [params?.id, router]);
  return null;
}
