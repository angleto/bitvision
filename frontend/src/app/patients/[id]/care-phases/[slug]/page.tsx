// Care-phase detail page.
//
// Server-component shell that defers all data fetching to the
// ``CarePhaseDetailBody`` client component. The shell exists so future
// SSR-time concerns (canonical URL, OpenGraph, RBAC pre-check) have a
// home without requiring a client-only refactor.

import CarePhaseDetailBody from "./_body";

export default function CarePhaseDetailPage() {
  return <CarePhaseDetailBody />;
}
