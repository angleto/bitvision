"use client";

// /settings/wallet — credit wallet balance (F7.4).
// Read-only. Top-up is admin-only until the payment integration lands (F9).
// Auth gate is in ``settings/layout.tsx``; ``user`` is guaranteed set here.

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError, type WalletBalanceOut, creditsApi } from "@/lib/api";

export default function WalletSettingsPage() {
  const [balance, setBalance] = useState<WalletBalanceOut | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const out = await creditsApi.balance();
        if (!cancelled) setBalance(out);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main>
      <h1>Wallet</h1>
      <p className="meta" style={{ marginBottom: "1rem" }}>
        LLM calls draw from this balance unless you have installed a BYOK API key, in which case
        they run against your own provider account instead. The platform adds a 20% markup on top of
        Anthropic's wholesale pricing; BYOK calls waive the markup.
      </p>

      {err && <p className="error">{err}</p>}
      {balance === null && !err && <p className="meta">Loading…</p>}

      {balance && (
        <section className="card">
          <h3>Current balance</h3>
          <p style={{ fontSize: "2rem", margin: "0.25rem 0" }}>${balance.balance_usd.toFixed(2)}</p>
          <p className="meta" style={{ margin: 0 }}>
            {balance.balance_cents.toLocaleString()} cents
          </p>
        </section>
      )}

      <section className="card" style={{ marginTop: "1rem" }}>
        <h3>Top-up</h3>
        <p className="meta" style={{ margin: 0 }}>
          Self-serve top-up will ship with the payment integration (F9). In the meantime, ask the
          platform admin to credit your wallet — they can do so through the admin API.
        </p>
      </section>

      <section className="card" style={{ marginTop: "1rem" }}>
        <h3>Sponsorizzazioni</h3>
        <p className="meta" style={{ margin: "0 0 0.5rem" }}>
          Autorizzi un altro utente (ad esempio un medico chiamato per consulto) a consumare crediti
          dal tuo wallet, fino a un massimale che decidi tu. Revocabile in qualsiasi momento.
        </p>
        <Link className="button" href="/settings/wallet/sponsorships">
          Gestisci sponsorizzazioni
        </Link>
      </section>

      <p className="meta" style={{ marginTop: "1.5rem" }}>
        See also <Link href="/settings/ai-models">AI models</Link> to pick the tier (free / standard
        / premium) the Q&A orchestrator uses, and <Link href="/settings/api-keys">API keys</Link> to
        install your own provider key and run on your own bill (BYOK waives the markup).
      </p>
    </main>
  );
}
