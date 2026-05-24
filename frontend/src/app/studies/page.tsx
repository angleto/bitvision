import { redirect } from "next/navigation";

/**
 * The Studies list page is gone — its job (cross-patient browse) is now
 * covered by the unified Patients view (own + shared + public scopes)
 * and Visual Search. We keep this route as a redirect so any existing
 * bookmark / inbound link still resolves to a useful page; the search
 * query is forwarded so ``/studies?q=foo`` still searches.
 *
 * The ``/studies/[id]`` page survives — it is the canonical deep-link
 * for an individual study and is still reached from patient cards,
 * report references, and citations.
 */
export default async function StudiesIndexPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  redirect(q ? `/patients?q=${encodeURIComponent(q)}` : "/patients");
}
