"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import ClinicalNotesSticky from "@/components/ClinicalNotesSticky";
import EvidenceEditor from "@/components/EvidenceEditor";
import ExternalIdentifiersPanel from "@/components/ExternalIdentifiersPanel";
import FascicoloDriveLayout from "@/components/FascicoloDriveLayout";
import FascicoloViewToggle from "@/components/FascicoloViewToggle";
import PatientContactsPanel from "@/components/PatientContactsPanel";
import PendingConsultsBadge from "@/components/PendingConsultsBadge";
import RevisionHistoryDrawer from "@/components/RevisionHistoryDrawer";
import SendStudyDialog from "@/components/SendStudyDialog";
import ShareWithAiModal from "@/components/ShareWithAiModal";
import { ApiError, type Patient, type PatientContact, patientsApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

// Closed set of dialog names this page knows about. Anything else in
// ``?dialog=`` is ignored (treated as "no dialog open"). Keeping this
// type here, not in a shared module, because the dialog-set is per-page
// and contained: a different page may have a different one.
type DialogName = "edit" | "share" | "history" | "ai-share";
const DIALOG_NAMES: ReadonlyArray<DialogName> = ["edit", "share", "history", "ai-share"];

function isDialogName(s: string | null): s is DialogName {
  return s !== null && (DIALOG_NAMES as readonly string[]).includes(s);
}

/**
 * Patient fascicolo (Health Record) — Drive-style layout.
 *
 * Vertical structure (top → bottom):
 *   1. Header (name, demographics line, actions)
 *      - Anagrafica completa (collapsible)
 *      - Allergie (warning chip if present)
 *      - Contacts panel
 *   2. ClinicalNotesSticky — sticky compact preview of ``patient.notes``
 *      with fade-out + expand/collapse + inline edit. Stays under the
 *      eye while the user navigates the Health Record below.
 *   3. Health Record (FascicoloViewToggle): always-on first-class
 *      container with tabs ``Drive | Eventi | Documenti | Sintesi &
 *      Evidenze | Provenance``. Default ``drive``; persisted via
 *      ``?view=`` so refresh / browser-back / deep-link survive.
 *
 * On mount, if ``?path=`` or ``?view=`` are present, smooth-scrolls to
 * the Health Record so the user lands directly in the work surface
 * instead of above the sticky notes.
 */
export default function PatientFascicoloPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const initialView = (() => {
    const v = searchParams.get("view");
    return v === "events" ||
      v === "documents" ||
      v === "provenance" ||
      v === "evidence" ||
      v === "drive"
      ? v
      : "drive";
  })();
  // Single source of truth for which auxiliary dialog/drawer is on
  // screen: ``?dialog=edit|share|history|ai-share`` in the URL. Putting
  // this in the URL means: (1) the browser back button closes the
  // dialog instead of leaving the page, (2) the URL is shareable so a
  // colleague can deep-link into "open this patient with the share
  // panel revealed", (3) refresh restores the same dialog the user had
  // open. Unknown values collapse to ``null``.
  const dialogParam = searchParams.get("dialog");
  const dialog: DialogName | null = isDialogName(dialogParam) ? dialogParam : null;
  const openDialog = useCallback(
    (name: DialogName) => {
      const next = new URLSearchParams(searchParams);
      next.set("dialog", name);
      const url = `${pathname}?${next.toString()}`;
      // Switching from one dialog to another should not stack a new
      // history entry — otherwise pressing back would reopen the
      // previous dialog instead of returning to the page. Only the
      // *first* open from "no dialog" pushes; subsequent switches
      // replace.
      if (dialog) router.replace(url, { scroll: false });
      else router.push(url, { scroll: false });
    },
    [router, pathname, searchParams, dialog],
  );
  const closeDialog = useCallback(() => {
    // ``router.back()`` is the right move when the dialog open pushed a
    // history entry (the common case), because it lets the browser
    // back button feel symmetric with the close button. Fall back to
    // ``replace`` when there's no history to pop (deep-link entry,
    // ``window.history.length === 1``) so close doesn't bounce out of
    // the SPA.
    if (typeof window !== "undefined" && window.history.length > 1) {
      router.back();
      return;
    }
    const next = new URLSearchParams(searchParams);
    next.delete("dialog");
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }, [router, pathname, searchParams]);
  const { user } = useAuth();
  const t = useTranslations("patient");
  const [patient, setPatient] = useState<Patient | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const healthRecordRef = useRef<HTMLDivElement | null>(null);

  const refreshPatient = useCallback(async () => {
    try {
      const data = await patientsApi.detail(params.id);
      setPatient(data);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [params.id, t]);

  useEffect(() => {
    refreshPatient();
  }, [refreshPatient]);

  // After patient loads, if the URL carries a deep-link into a folder
  // (``?path=``) or a non-default tab (``?view=``), snap the viewport
  // to the Health Record so the user is dropped directly into the work
  // surface. Without this, every refresh / browser-back lands above
  // the sticky notes and the user has to scroll.
  // Only fires once after first patient load to avoid yanking the
  // viewport on subsequent refetches (edit, contacts changed, etc).
  const didInitialScrollRef = useRef(false);
  useEffect(() => {
    if (!patient || didInitialScrollRef.current) return;
    didInitialScrollRef.current = true;
    const hasDeepLink =
      searchParams.has("path") ||
      (searchParams.has("view") && searchParams.get("view") !== "drive");
    if (!hasDeepLink) return;
    // Defer one frame so the FascicoloViewToggle has had a chance to
    // mount its tab content, otherwise scrollIntoView lands at a
    // shorter document and re-flows when the tab renders.
    const id = window.requestAnimationFrame(() => {
      healthRecordRef.current?.scrollIntoView({ behavior: "auto", block: "start" });
    });
    return () => window.cancelAnimationFrame(id);
  }, [patient, searchParams]);

  if (err)
    return (
      <main>
        <p className="error">{err}</p>
      </main>
    );
  if (!patient)
    return (
      <main>
        <p className="meta">{t("loading")}</p>
      </main>
    );

  const isOwner =
    !!user &&
    (user.is_admin ||
      user.subject_id === patient.managed_by_subject_id ||
      user.subject_id === patient.self_user_subject_id);

  return (
    <main>
      <p className="meta">
        <Link href="/patients">{t("backToPatients")}</Link>
      </p>
      <PatientHeader
        patient={patient}
        isOwner={isOwner}
        onEdit={() => openDialog("edit")}
        onShare={() => (dialog === "share" ? closeDialog() : openDialog("share"))}
        onOpenHistory={() => openDialog("history")}
        onOpenAiShare={() => openDialog("ai-share")}
        sharingOpen={dialog === "share"}
        onContactsChanged={refreshPatient}
      />
      {dialog === "edit" && (
        <EditProfileForm
          patient={patient}
          onCancel={closeDialog}
          onSaved={() => {
            closeDialog();
            refreshPatient();
          }}
        />
      )}
      <SendStudyDialog
        kind="patient"
        studyId={patient.id}
        patientId={patient.id}
        studyLabel={patient.display_name}
        open={dialog === "share"}
        onClose={closeDialog}
      />
      <ClinicalNotesSticky patient={patient} isOwner={isOwner} onUpdated={refreshPatient} />
      <div ref={healthRecordRef}>
        <FascicoloViewToggle
          patient={patient}
          isOwner={isOwner}
          initial={initialView}
          driveSlot={<FascicoloDriveLayout patientId={patient.id} isOwner={isOwner} />}
        />
      </div>
      <RevisionHistoryDrawer
        patientId={patient.id}
        open={dialog === "history"}
        onClose={closeDialog}
      />
      <ShareWithAiModal patientId={patient.id} open={dialog === "ai-share"} onClose={closeDialog} />
    </main>
  );
}

// ---- Header (name + demographics + share/export/edit) ----

function PatientHeader({
  patient,
  isOwner,
  onEdit,
  onShare,
  onOpenHistory,
  onOpenAiShare,
  sharingOpen,
  onContactsChanged,
}: {
  patient: Patient;
  isOwner: boolean;
  onEdit: () => void;
  onShare: () => void;
  onOpenHistory: () => void;
  onOpenAiShare: () => void;
  sharingOpen: boolean;
  /** Called by ``PatientContactsPanel`` after a successful promote /
   *  revoke so the page can refresh the patient row. */
  onContactsChanged: () => void;
}) {
  const t = useTranslations("patient");
  const [anagraphicsOpen, setAnagraphicsOpen] = useState(false);
  const yearsOld = patient.birth_date
    ? Math.max(0, new Date().getFullYear() - Number.parseInt(patient.birth_date.slice(0, 4), 10))
    : null;
  return (
    <header
      style={{
        marginBottom: "1.25rem",
        paddingBottom: "1rem",
        borderBottom: "1px solid var(--bv-card-border)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: "0.75rem",
        }}
      >
        <div style={{ minWidth: 0 }}>
          <h1
            style={{
              marginBottom: "0.25rem",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
            }}
          >
            <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
              {patient.display_name}
            </span>
            <button
              type="button"
              className="ghost"
              onClick={() => setAnagraphicsOpen((v) => !v)}
              aria-expanded={anagraphicsOpen}
              aria-controls="patient-anagraphics-panel"
              title={anagraphicsOpen ? t("anagraphicsHide") : t("anagraphicsShow")}
              style={{
                fontSize: "0.85rem",
                fontWeight: 400,
                padding: "0.15rem 0.55rem",
                borderRadius: 6,
                border: "1px solid var(--bv-card-border)",
                lineHeight: 1.2,
                cursor: "pointer",
                flexShrink: 0,
              }}
            >
              {anagraphicsOpen ? "▴" : "▾"}{" "}
              {anagraphicsOpen ? t("anagraphicsHide") : t("anagraphicsShow")}
            </button>
          </h1>
          <p className="meta" style={{ marginBottom: 0 }}>
            {patient.birth_date ? (
              <>
                {patient.birth_date}
                {yearsOld !== null ? ` · ${yearsOld}${t("yearsShort")}` : ""}
              </>
            ) : (
              t("noBirthDate")
            )}
            {patient.sex ? ` · ${patient.sex}` : ""}
            {patient.tax_id ? ` · ${t("taxIdShort")} ${patient.tax_id}` : ""}
            {patient.blood_type ? ` · ${patient.blood_type}` : ""}
          </p>
        </div>

        <div
          style={{
            display: "inline-flex",
            flexWrap: "wrap",
            gap: "0.4rem",
            fontSize: "0.85rem",
            fontWeight: 400,
            alignItems: "center",
          }}
        >
          <PendingConsultsBadge patientId={patient.id} isOwner={isOwner} />
          {/* Export + Events were duplicated header entry-points
          shadowing the canonical surfaces inside the Health Record:
          - Export → button inside FascicoloDriveLayout opens
            ExportFascicoloDialog (async-job, section picker,
            DICOM opt-in). The legacy header button issued GET on
            an endpoint that's POST-only and 405'd.
          - Events → tab inside FascicoloViewToggle (with the
            kind-filter chip bar). The legacy link pointed at the
            now-redirected /events route. */}
          <button
            type="button"
            className="ghost"
            onClick={onOpenHistory}
            style={headerLinkStyle as React.CSSProperties}
            title={t("btnHistoryTitle")}
          >
            {t("btnHistory")}
          </button>
          {isOwner && (
            <>
              <SharingMenu
                patientId={patient.id}
                onCreateShare={onShare}
                onOpenAiShare={onOpenAiShare}
              />
              <button type="button" className="ghost" onClick={onEdit}>
                {t("btnEditProfile")}
              </button>
            </>
          )}
        </div>
      </div>

      {anagraphicsOpen && (
        <div
          id="patient-anagraphics-panel"
          style={{
            marginTop: "0.85rem",
            padding: "0.75rem 1rem",
            background: "var(--bv-card-bg)",
            border: "1px solid var(--bv-card-border)",
            borderRadius: "var(--bv-r-sm)",
            fontSize: "0.88rem",
          }}
        >
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
              gap: "0.5rem 1.25rem",
              margin: 0,
            }}
          >
            {[
              ["anagDisplayName", patient.display_name],
              ["anagExternalId", patient.external_id],
              ["anagBirthDate", patient.birth_date],
              ["anagSex", patient.sex],
              ["anagTaxId", patient.tax_id],
              ["anagPhone", patient.phone],
              ["anagEmail", patient.email],
              ["anagAddress", patient.address],
              ["anagBloodType", patient.blood_type],
              ["anagBirthPlaceCity", patient.birth_place_city],
              ["anagBirthPlaceProvince", patient.birth_place_province],
              ["anagAslCode", patient.asl_code],
              ["anagAslName", patient.asl_name],
            ].map(([k, v]) => (
              <div
                key={String(k)}
                style={{ display: "flex", flexDirection: "column", gap: "0.1rem" }}
              >
                <dt
                  style={{
                    color: "var(--bv-fg-soft)",
                    fontSize: "0.75rem",
                    textTransform: "uppercase",
                    letterSpacing: "0.03em",
                    margin: 0,
                  }}
                >
                  {t(String(k))}
                </dt>
                <dd
                  style={{
                    margin: 0,
                    fontWeight: 500,
                    wordBreak: "break-word",
                  }}
                >
                  {v ? String(v) : <span style={{ color: "var(--bv-fg-muted)" }}>—</span>}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {patient.allergies && (
        <div
          style={{
            marginTop: "0.85rem",
            padding: "0.3rem 0.6rem",
            background: "var(--bv-warning-soft)",
            color: "var(--bv-warning)",
            border: "1px solid color-mix(in srgb, var(--bv-warning) 22%, transparent)",
            borderRadius: "var(--bv-r-sm)",
            fontSize: "0.82rem",
            lineHeight: 1.35,
          }}
        >
          <strong style={{ marginRight: "0.35rem" }}>{t("allergiesLabel")}</strong>
          {patient.allergies}
        </div>
      )}

      <PatientContactsPanel patient={patient} isOwner={isOwner} onChanged={onContactsChanged} />
    </header>
  );
}

const headerLinkStyle: React.CSSProperties = {
  fontSize: "0.85rem",
  fontWeight: 400,
  textDecoration: "none",
  padding: "0.3rem 0.7rem",
  border: "1px solid #d0d5dd",
  borderRadius: 6,
  color: "inherit",
};

// ---- Edit profile form (moved out of the old <ProfileSection>) ----

function EditProfileForm({
  patient,
  onCancel,
  onSaved,
}: {
  patient: Patient;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const t = useTranslations("patient");
  const [form, setForm] = useState({
    display_name: patient.display_name,
    birth_date: patient.birth_date ?? "",
    sex: patient.sex ?? "",
    tax_id: patient.tax_id ?? "",
    phone: patient.phone ?? "",
    email: patient.email ?? "",
    address: patient.address ?? "",
    blood_type: patient.blood_type ?? "",
    allergies: patient.allergies ?? "",
    notes: patient.notes ?? "",
  });
  // Stable per-row UI key so React reconciliation survives reorder /
  // remove without confusing input focus or controlled-state. Backend
  // ``id`` is the natural choice when present (persisted contact);
  // newly-added rows get a fresh UUID. The field is stripped out
  // before POST in ``handleSave``.
  type LocalContact = PatientContact & { _uiKey: string };
  const [contacts, setContacts] = useState<LocalContact[]>(() =>
    (patient.contacts ?? []).map((c) => ({
      ...c,
      _uiKey: c.id ?? crypto.randomUUID(),
    })),
  );
  const [busy, setBusy] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setSaveErr(null);
    try {
      const updates: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(form)) {
        updates[k] = v || null;
      }
      // display_name cannot be null.
      updates.display_name = form.display_name;
      // Drop empty contact rows (no label) and normalise empty strings
      // to null so the backend persists ``null`` consistently.
      updates.contacts = contacts
        .filter((c) => c.label.trim().length > 0)
        .map((c) => ({
          label: c.label.trim(),
          relationship: c.relationship?.trim() || null,
          email: c.email?.trim() || null,
          phone: c.phone?.trim() || null,
        }));
      await patientsApi.update(patient.id, updates, patient.etag);
      onSaved();
    } catch (e) {
      setSaveErr(e instanceof ApiError ? e.message : t("saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  function updateContact(i: number, patch: Partial<PatientContact>) {
    setContacts((prev) => {
      const next = [...prev];
      next[i] = { ...next[i], ...patch };
      return next;
    });
  }
  function removeContact(i: number) {
    setContacts((prev) => prev.filter((_, j) => j !== i));
  }
  function addContact() {
    setContacts((prev) => [
      ...prev,
      {
        label: "",
        relationship: "",
        email: "",
        phone: "",
        _uiKey: crypto.randomUUID(),
      },
    ]);
  }

  return (
    <form className="card" onSubmit={handleSave} style={{ marginBottom: "1rem" }}>
      <h2>{t("editTitle")}</h2>
      {saveErr && <p className="error">{saveErr}</p>}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
        <label>
          <span className="meta">{t("fieldName")}</span>
          <input
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            required
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("fieldBirthDate")}</span>
          <input
            type="date"
            value={form.birth_date}
            onChange={(e) => setForm({ ...form, birth_date: e.target.value })}
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("fieldSex")}</span>
          <select
            value={form.sex}
            onChange={(e) => setForm({ ...form, sex: e.target.value })}
            style={{ width: "100%", padding: "0.4rem" }}
          >
            <option value="">-</option>
            <option value="M">M</option>
            <option value="F">F</option>
            <option value="O">{t("fieldSexOther")}</option>
          </select>
        </label>
        <label>
          <span className="meta">{t("fieldTaxId")}</span>
          <input
            value={form.tax_id}
            onChange={(e) => setForm({ ...form, tax_id: e.target.value })}
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("fieldPhone")}</span>
          <input
            value={form.phone}
            onChange={(e) => setForm({ ...form, phone: e.target.value })}
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("fieldEmail")}</span>
          <input
            type="email"
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("fieldBloodType")}</span>
          <input
            value={form.blood_type}
            onChange={(e) => setForm({ ...form, blood_type: e.target.value })}
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("fieldAddress")}</span>
          <input
            value={form.address}
            onChange={(e) => setForm({ ...form, address: e.target.value })}
            style={{ width: "100%" }}
          />
        </label>
      </div>
      <label style={{ display: "block", marginTop: "0.5rem" }}>
        <span className="meta">{t("fieldAllergies")}</span>
        <textarea
          value={form.allergies}
          onChange={(e) => setForm({ ...form, allergies: e.target.value })}
          rows={2}
          style={{ width: "100%" }}
        />
      </label>
      <div style={{ display: "block", marginTop: "0.5rem" }}>
        <span className="meta">{t("fieldClinicalNotes")}</span>
        <EvidenceEditor
          value={form.notes}
          onChange={(md) => setForm({ ...form, notes: md })}
          embedded
          patientId={patient.id}
        />
      </div>

      <fieldset
        style={{
          marginTop: "1rem",
          padding: "0.75rem 1rem",
          border: "1px solid var(--bv-card-border)",
          borderRadius: "var(--bv-r-sm)",
        }}
      >
        <legend className="meta" style={{ padding: "0 0.4rem", fontSize: "0.82rem" }}>
          {t("contactsLegend")}
        </legend>
        {contacts.length === 0 && (
          <p className="meta" style={{ marginBottom: "0.5rem", fontSize: "0.85rem" }}>
            {t("contactsEmpty")}
          </p>
        )}
        {contacts.map((c, i) => (
          <div
            key={c._uiKey}
            style={{
              display: "grid",
              gridTemplateColumns: "1.4fr 1fr 1.4fr 1.2fr auto",
              gap: "0.4rem",
              marginBottom: "0.4rem",
              alignItems: "center",
            }}
          >
            <input
              value={c.label}
              onChange={(e) => updateContact(i, { label: e.target.value })}
              placeholder={t("contactPlaceholderName")}
              required
            />
            <input
              value={c.relationship ?? ""}
              onChange={(e) => updateContact(i, { relationship: e.target.value })}
              placeholder={t("contactPlaceholderRelationship")}
            />
            <input
              type="email"
              value={c.email ?? ""}
              onChange={(e) => updateContact(i, { email: e.target.value })}
              placeholder={t("contactPlaceholderEmail")}
            />
            <input
              type="tel"
              value={c.phone ?? ""}
              onChange={(e) => updateContact(i, { phone: e.target.value })}
              placeholder={t("contactPlaceholderPhone")}
            />
            <button
              type="button"
              className="ghost"
              onClick={() => removeContact(i)}
              title={t("contactRemoveTitle")}
              style={{ color: "var(--bv-danger)", fontSize: "0.78rem" }}
            >
              ×
            </button>
          </div>
        ))}
        <button
          type="button"
          className="ghost"
          onClick={addContact}
          style={{ fontSize: "0.82rem", marginTop: "0.4rem" }}
        >
          {t("contactAdd")}
        </button>
      </fieldset>

      <fieldset style={{ marginTop: "1rem" }}>
        <legend>Identificatori esterni</legend>
        <ExternalIdentifiersPanel patientId={patient.id} editable />
      </fieldset>

      <div
        style={{
          marginTop: "0.75rem",
          display: "flex",
          gap: "0.5rem",
          justifyContent: "flex-end",
        }}
      >
        <button type="button" className="ghost" onClick={onCancel}>
          {t("btnCancel")}
        </button>
        <button type="submit" disabled={busy}>
          {busy ? t("btnSaveBusy") : t("btnSave")}
        </button>
      </div>
    </form>
  );
}

function SharingMenu({
  patientId,
  onCreateShare,
  onOpenAiShare,
}: {
  patientId: string;
  onCreateShare: () => void;
  onOpenAiShare: () => void;
}) {
  const t = useTranslations("patient");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const itemStyle: React.CSSProperties = {
    display: "block",
    padding: "0.55rem 0.9rem",
    color: "inherit",
    textDecoration: "none",
    cursor: "pointer",
    background: "transparent",
    border: 0,
    width: "100%",
    textAlign: "left",
  };

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-block" }}>
      <button
        type="button"
        className="ghost"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        title={t("btnSharingMenuTitle")}
      >
        {t("btnSharingMenu")} ▾
      </button>
      {open && (
        <div
          role="menu"
          style={{
            position: "absolute",
            top: "100%",
            right: 0,
            marginTop: "0.25rem",
            background: "var(--bv-card-bg)",
            color: "var(--bv-fg)",
            border: "1px solid var(--bv-card-border)",
            borderRadius: "var(--bv-r-md, 6px)",
            padding: "0.3rem 0",
            minWidth: 280,
            zIndex: 100,
            boxShadow: "0 8px 24px rgba(0,0,0,0.22)",
          }}
        >
          <button
            type="button"
            role="menuitem"
            style={itemStyle}
            onClick={() => {
              setOpen(false);
              onCreateShare();
            }}
          >
            <strong>{t("btnSharingCreate")}</strong>
            <div className="meta" style={{ fontSize: "0.74rem", marginTop: 2 }}>
              {t("btnSharingCreateHint")}
            </div>
          </button>
          <Link
            role="menuitem"
            href={`/patients/${patientId}/shares`}
            style={itemStyle}
            onClick={() => setOpen(false)}
          >
            <strong>{t("btnSharingManage")}</strong>
            <div className="meta" style={{ fontSize: "0.74rem", marginTop: 2 }}>
              {t("btnSharingManageHint")}
            </div>
          </Link>
          <div
            style={{
              borderTop: "1px solid var(--bv-card-border)",
              margin: "0.3rem 0",
            }}
          />
          <button
            type="button"
            role="menuitem"
            style={itemStyle}
            onClick={() => {
              setOpen(false);
              onOpenAiShare();
            }}
          >
            <strong>{t("btnSharingAi")}</strong>
            <div className="meta" style={{ fontSize: "0.74rem", marginTop: 2 }}>
              {t("btnSharingAiHint")}
            </div>
          </button>
        </div>
      )}
    </div>
  );
}
