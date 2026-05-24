"use client";

// Standalone "Tasks" page redirect. The operational checklist lives as
// a tab inside the patient Health Record (``FascicoloViewToggle``,
// ``view=tasks``); this route exists so deep-linked URLs from MCP
// tools, share links and external bookmarks land on the canonical
// view. Preserves the ``?merge`` flag so a unified-view link stays
// unified through the redirect.

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

export default function PatientTasksRedirect() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const search = useSearchParams();

  useEffect(() => {
    const qs = new URLSearchParams();
    qs.set("view", "tasks");
    // Carry forward filters that the MCP tools / share links can
    // legitimately deep-link to. We only forward the keys we know
    // about so a malicious / mistaken caller can't smuggle arbitrary
    // params into the canonical page state.
    for (const key of ["merge", "st", "cat", "pri", "from", "to", "q", "s"]) {
      const v = search.get(key);
      if (v !== null) qs.set(key, v);
    }
    router.replace(`/patients/${params.id}?${qs.toString()}`);
  }, [params.id, router, search]);

  return null;
}
