"use client";

// /settings/ai — single AI configuration surface.
//
// Replaces the older /settings/ai-models + /settings/wallet (top-up
// section) + /settings/api-keys (BYOK) + /settings/ai-assistants
// (MCP wizard) cluster of pages with one progressively-disclosed
// hub. The legacy URLs still resolve so deep-links and the
// PatientAskPanel 402 dialog (which links to /settings/wallet) keep
// working — but the user-facing entry point on the settings index
// is now this single page.
//
// Auth gate is in settings/layout.tsx; ``user`` is guaranteed when
// this body renders.

import AiSettingsHub from "@/components/AiSettingsHub";

export default function AiSettingsPage() {
  return <AiSettingsHub />;
}
