"use client";

// Standalone upload entry point for a single patient fascicolo.
//
// The preferred flow is drag-and-drop inside the Drive-style fascicolo
// layout (F3 + F5), which opens the inline uploader in-context with a
// target folder already resolved. This page is the fallback for users
// who land here via a direct link (e.g. "Upload" button in the header)
// — it picks files with a native <input>, then hands them to the very
// same InlineFascicoloUploader so behaviour stays identical.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import InlineFascicoloUploader, {
  type BulkUploadSummary,
} from "@/components/InlineFascicoloUploader";
import { useAuth } from "@/lib/auth-context";

export default function PatientUploadPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const { user, status } = useAuth();
  const t = useTranslations("patientUpload");
  const inputRef = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<File[]>([]);
  const [lastSummary, setLastSummary] = useState<BulkUploadSummary | null>(null);

  useEffect(() => {
    if (status === "ready" && !user) {
      router.replace(`/login?next=/patients/${params.id}/upload`);
    }
  }, [status, user, router, params.id]);

  const handlePick = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      setPicked(Array.from(e.target.files));
    }
    e.target.value = "";
  };

  return (
    <main>
      <p className="meta">
        <Link href={`/patients/${params.id}`}>{t("backToFascicolo")}</Link>
      </p>
      <h1>{t("title")}</h1>
      <p className="meta">{t("intro")}</p>

      <div className="card" style={{ marginTop: "1rem" }}>
        <button type="button" onClick={() => inputRef.current?.click()}>
          {t("selectFile")}
        </button>
        <input
          ref={inputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={handlePick}
        />
        {lastSummary && (
          <p className="meta" style={{ marginTop: "0.75rem" }}>
            {t("lastUpload", { n: lastSummary.routed.length })}
          </p>
        )}
      </div>

      {picked.length > 0 && (
        <InlineFascicoloUploader
          patientId={params.id}
          targetFolderId={null}
          initialFiles={picked}
          onComplete={(s) => setLastSummary(s)}
          onClose={() => setPicked([])}
        />
      )}
    </main>
  );
}
