"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { ApiError, patientsApi } from "@/lib/api";

export default function NewPatientPage() {
  const router = useRouter();
  const t = useTranslations("patientNew");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [displayName, setDisplayName] = useState("");
  const [externalId, setExternalId] = useState("");
  const [birthDate, setBirthDate] = useState("");
  const [sex, setSex] = useState("");
  const [taxId, setTaxId] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [address, setAddress] = useState("");
  const [bloodType, setBloodType] = useState("");
  const [allergies, setAllergies] = useState("");
  const [notes, setNotes] = useState("");

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const created = await patientsApi.create({
        display_name: displayName.trim(),
        external_id: externalId.trim() || null,
        birth_date: birthDate || null,
        sex: sex || null,
        tax_id: taxId.trim() || null,
        phone: phone.trim() || null,
        email: email.trim() || null,
        address: address.trim() || null,
        blood_type: bloodType.trim() || null,
        allergies: allergies.trim() || null,
        notes: notes.trim() || null,
      });
      router.push(`/patients/${created.id}`);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("errorCreate"));
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>{t("title")}</h1>
      <p className="meta">{t("intro")}</p>

      <form className="form" onSubmit={onSubmit}>
        {err && <div className="error">{err}</div>}

        <label>
          {t("displayName")} <span className="meta">{t("displayNameRequired")}</span>
          <input
            required
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            // biome-ignore lint/a11y/noAutofocus: dedicated form page; sending the cursor to the first field is the obvious productivity choice.
            autoFocus
          />
        </label>

        <label>
          {t("externalId")} <span className="meta">{t("externalIdHint")}</span>
          <input value={externalId} onChange={(e) => setExternalId(e.target.value)} />
        </label>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
          <label>
            {t("birthDate")}
            <input type="date" value={birthDate} onChange={(e) => setBirthDate(e.target.value)} />
          </label>
          <label>
            {t("sex")}
            <select value={sex} onChange={(e) => setSex(e.target.value)}>
              <option value="">—</option>
              <option value="M">M</option>
              <option value="F">F</option>
              <option value="O">O</option>
            </select>
          </label>
        </div>

        <label>
          {t("codiceFiscaleLabel")}
          <input
            value={taxId}
            onChange={(e) => setTaxId(e.target.value.toUpperCase())}
            maxLength={32}
          />
        </label>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
          <label>
            {t("phone")}
            <input
              type="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              maxLength={32}
            />
          </label>
          <label>
            {t("email")}
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              maxLength={255}
            />
          </label>
        </div>

        <label>
          {t("address")}
          <input value={address} onChange={(e) => setAddress(e.target.value)} />
        </label>

        <label>
          {t("bloodType")}
          <select value={bloodType} onChange={(e) => setBloodType(e.target.value)}>
            <option value="">—</option>
            <option value="0+">0+</option>
            <option value="0-">0-</option>
            <option value="A+">A+</option>
            <option value="A-">A-</option>
            <option value="B+">B+</option>
            <option value="B-">B-</option>
            <option value="AB+">AB+</option>
            <option value="AB-">AB-</option>
          </select>
        </label>

        <label>
          {t("allergies")}
          <textarea rows={2} value={allergies} onChange={(e) => setAllergies(e.target.value)} />
        </label>

        <label>
          {t("clinicalNotes")}
          <textarea rows={4} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>

        <div className="actions">
          <Link href="/patients" className="meta">
            {t("cancel")}
          </Link>
          <button type="submit" disabled={busy || !displayName.trim()}>
            {busy ? t("submitBusy") : t("submit")}
          </button>
        </div>
      </form>
    </main>
  );
}
