"use client";

// /settings/api-keys is the deprecated BYOK page; the embedded agent
// surface that would consume those keys never landed. AI credential
// management is the job of /settings/ai (AiSettingsHub) and the BYO
// assistant flow under /settings/ai-assistants. Redirect to the hub.

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function ApiKeysRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/settings/ai");
  }, [router]);
  return (
    <main style={{ padding: "1.25rem" }}>
      <p className="meta">…</p>
    </main>
  );
}
