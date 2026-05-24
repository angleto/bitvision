"use client";

// Process-wide singleton fetch for the document catalog (kinds /
// provenances / authorities). The endpoint is keyed by the user
// session and short-cached server-side via ETag, so repeating it for
// every dropdown that mounts wastes a round-trip. Instead the first
// caller starts a fetch, every subsequent caller awaits the same
// promise, and the resolved value sticks for the lifetime of the
// JS module (i.e. the page nav). A hard refresh refetches.
//
// We deliberately avoid SWR / react-query: the surface is one
// endpoint, the data is essentially immutable across a session, and
// the rest of the codebase already uses hand-rolled hooks for the
// same shape (see ``useJob``). One file, one hook, no extra deps.

import { useEffect, useState } from "react";

import { type DocumentCatalog, documentCatalogApi } from "./api";

let cached: DocumentCatalog | null = null;
let inflight: Promise<DocumentCatalog> | null = null;

async function fetchOnce(): Promise<DocumentCatalog> {
  if (cached) return cached;
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const data = await documentCatalogApi.list();
      cached = data;
      return data;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

export interface UseDocumentCatalogResult {
  catalog: DocumentCatalog | null;
  loading: boolean;
  error: Error | null;
}

/**
 * Subscribe to the document catalog. Returns ``null`` while the
 * first fetch is in flight; subsequent mounts hand back the cached
 * value synchronously.
 *
 * Callers that need the active subset should derive it themselves
 * (``catalog.kinds.filter(k => k.is_active)``) — the catalog
 * intentionally surfaces inactive entries too so legacy documents
 * can still render a human-readable label for retired kinds.
 */
export function useDocumentCatalog(): UseDocumentCatalogResult {
  const [catalog, setCatalog] = useState<DocumentCatalog | null>(cached);
  const [loading, setLoading] = useState(cached === null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (cached) {
      setCatalog(cached);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    fetchOnce()
      .then((data) => {
        if (cancelled) return;
        setCatalog(data);
        setError(null);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e : new Error(String(e)));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { catalog, loading, error };
}

/**
 * Resolve the locale-preferred label for a catalog entry, falling
 * back to ``en``, then to ``id`` when neither localised string is
 * present (e.g. a partially-seeded row).
 */
export function entryLabel(
  entry: { id: string; display_name: Record<string, string> } | null | undefined,
  locale: string,
): string {
  if (!entry) return "";
  const name = entry.display_name?.[locale] ?? entry.display_name?.en;
  return name || entry.id;
}
