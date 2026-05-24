"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { ApiError, authApi, setStoredToken } from "@/lib/api";

type Status = "pending" | "verifying" | "success" | "error";

export default function VerifyEmailPage() {
  return (
    <Suspense
      fallback={
        <div className="form">
          <p className="meta">Loading…</p>
        </div>
      }
    >
      <VerifyEmailInner />
    </Suspense>
  );
}

function VerifyEmailInner() {
  const router = useRouter();
  const search = useSearchParams();
  const token = search.get("token");
  const [status, setStatus] = useState<Status>("pending");
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    // A React strict-mode double-effect would also re-spend the token.
    // We guard with the state transition: only the first "pending" run
    // proceeds to call the API.
    if (!token) {
      setStatus("error");
      setMsg("Missing verification token.");
      return;
    }
    if (status !== "pending") return;
    setStatus("verifying");
    authApi
      .verifyEmail(token)
      .then(({ access_token }) => {
        setStoredToken(access_token);
        setStatus("success");
        // Small delay so the user sees the confirmation before redirect.
        setTimeout(() => router.push("/studies"), 1200);
      })
      .catch((e) => {
        setStatus("error");
        setMsg(e instanceof ApiError ? e.message : "verification failed");
      });
  }, [token, status, router]);

  if (status === "success") {
    return (
      <div className="form">
        <h1>Email verified</h1>
        <p>Thanks — your account is now active. Redirecting you to the app…</p>
      </div>
    );
  }
  if (status === "error") {
    return (
      <div className="form">
        <h1>Verification failed</h1>
        <div className="error">{msg ?? "The link is invalid or expired."}</div>
        <div className="actions">
          <Link href="/register" className="meta">
            Back to sign up
          </Link>
          <Link href="/login" className="meta">
            Log in
          </Link>
        </div>
      </div>
    );
  }
  return (
    <div className="form">
      <h1>Verifying…</h1>
      <p className="meta">One moment while we confirm your email.</p>
    </div>
  );
}
