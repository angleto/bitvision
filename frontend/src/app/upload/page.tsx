"use client";

import { useTranslations } from "next-intl";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";

import UniversalUploader from "@/components/UniversalUploader";
import { useAuth } from "@/lib/auth-context";

export default function UploadPage() {
  return (
    <Suspense
      fallback={
        <main>
          <p className="meta">Loading…</p>
        </main>
      }
    >
      <UploadPageInner />
    </Suspense>
  );
}

function UploadPageInner() {
  const { user, status } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const t = useTranslations("upload");

  // Uploads always need an owner subject — an anonymous caller has no
  // home for the resulting Study / Document rows, so we gate the whole
  // page behind the login wall.
  useEffect(() => {
    if (status === "ready" && !user) {
      router.replace("/login?next=/upload");
    }
  }, [status, user, router]);

  // Both params are optional: when provided, the uploader forwards them
  // to /api/upload/bulk so the server can attach everything to a given
  // patient/folder instead of creating orphan studies.
  const patientId = params.get("patient_id") ?? undefined;
  const targetFolderId = params.get("folder_id") ?? undefined;

  return (
    <main>
      <h1>{t("pageTitle")}</h1>
      <p
        className="meta"
        // ``pageIntro`` carries inline ``<code>`` and ``<strong>`` tags
        // for emphasis; ``t.raw`` returns the raw string and we pass
        // it through ``dangerouslySetInnerHTML`` since the markup is
        // controlled (lives in the i18n bundle, not user input).
        // biome-ignore lint/security/noDangerouslySetInnerHtml: source is the static i18n bundle, not user input.
        dangerouslySetInnerHTML={{ __html: t.raw("pageIntro") as string }}
      />

      {status === "ready" && user ? (
        <UniversalUploader patientId={patientId} targetFolderId={targetFolderId} />
      ) : (
        <p className="meta">Loading…</p>
      )}

      <p
        className="meta"
        style={{ marginTop: "2rem" }}
        // biome-ignore lint/security/noDangerouslySetInnerHtml: source is the static i18n bundle, not user input.
        dangerouslySetInnerHTML={{ __html: t.raw("pageHintCli") as string }}
      />
    </main>
  );
}
