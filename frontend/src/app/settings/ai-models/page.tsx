"use client";

// /settings/ai-models is superseded by /settings/ai (AiSettingsHub),
// which absorbs tier picking together with BYOK and assistants. This
// route stays around to satisfy any external bookmark and silently
// forwards. ``router.replace`` (not ``push``) keeps the back button
// behaving — the deprecated URL never appears in history.

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function AiModelsRedirect() {
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
