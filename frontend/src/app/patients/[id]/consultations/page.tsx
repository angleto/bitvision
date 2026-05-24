"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import ConsultationCard from "@/components/ConsultationCard";
import {
  ApiError,
  type Consultation,
  type ConsultationAuthorKind,
  type ConsultationStatus,
  consultationsApi,
} from "@/lib/api";

type StatusFilter = ConsultationStatus | "all";
type AuthorFilter = ConsultationAuthorKind | "all";

export default function ConsultationsListPage() {
  const params = useParams<{ id: string }>();
  const tA = useTranslations("actions");
  const tCl = useTranslations("consultationsList");
  const t = useTranslations("patientConsultations");
  const [items, setItems] = useState<Consultation[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [status, setStatus] = useState<StatusFilter>("all");
  const [authorKind, setAuthorKind] = useState<AuthorFilter>("all");

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setErr(null);
    consultationsApi
      .list(params.id, { status, author_kind: authorKind })
      .then((data) => !cancelled && setItems(data))
      .catch((e) => !cancelled && setErr(e instanceof ApiError ? e.message : t("errorLoad")));
    return () => {
      cancelled = true;
    };
  }, [params.id, status, authorKind, t]);

  const count = useMemo(() => items?.length ?? 0, [items]);

  return (
    <main>
      <p className="meta">
        <Link href={`/patients/${params.id}`}>{t("backToFascicolo")}</Link>
      </p>
      <h1>
        {t("title")} <span className="meta">({count})</span>
      </h1>

      <div
        className="card"
        style={{
          display: "flex",
          gap: "1rem",
          alignItems: "center",
          marginBottom: "1rem",
          flexWrap: "wrap",
        }}
      >
        <label>
          <span className="meta">{t("filterStatusLabel")}</span>{" "}
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as StatusFilter)}
            style={{ padding: "0.3rem" }}
          >
            <option value="all">{t("filterStatusAll")}</option>
            <option value="submitted">{t("filterStatusSubmitted")}</option>
            <option value="signed">{t("filterStatusSigned")}</option>
            <option value="rejected">{t("filterStatusRejected")}</option>
          </select>
        </label>
        <label>
          <span className="meta">{t("filterAuthorLabel")}</span>{" "}
          <select
            value={authorKind}
            onChange={(e) => setAuthorKind(e.target.value as AuthorFilter)}
            style={{ padding: "0.3rem" }}
          >
            <option value="all">{t("filterAuthorAll")}</option>
            <option value="agent">{t("filterAuthorAgent")}</option>
            <option value="human">{t("filterAuthorHuman")}</option>
          </select>
        </label>
      </div>

      {err && <p className="error">{err}</p>}
      {!err && items === null && <p className="meta">{tA("loading")}</p>}
      {!err && items && items.length === 0 && <p className="meta">{tCl("empty")}</p>}
      {items?.map((c) => (
        <ConsultationCard key={c.id} patientId={params.id} consultation={c} />
      ))}
    </main>
  );
}
