"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Callout, ErrorNote, Spinner } from "@/components/ui";

export default function UnsubscribePage() {
  const [token, setToken] = useState<string | null>(null);
  const [status, setStatus] = useState<"loading" | "done" | "error">("loading");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const value = params.get("token");
    setToken(value);
    if (!value) {
      setStatus("error");
      setMessage("Missing unsubscribe token.");
      return;
    }
    api
      .unsubscribeAlert(value)
      .then((result) => {
        setStatus("done");
        setMessage(
          `You will no longer receive ${result.short_name} priority alerts at ${result.email}.`,
        );
      })
      .catch((error) => {
        setStatus("error");
        setMessage((error as Error).message);
      });
  }, []);

  return (
    <main className="flex min-h-dvh items-center justify-center bg-ink p-6 text-zinc-200">
      <div className="w-full max-w-md space-y-4 rounded border border-white/10 bg-black/50 p-6">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-duck">
          Yellow Duck Labs · Fire Watch
        </div>
        <h1 className="text-lg font-medium text-white">Unsubscribe</h1>

        {status === "loading" && <Spinner label="Updating your subscription" />}

        {status === "done" && message && <Callout>{message}</Callout>}
        {status === "error" && message && <ErrorNote error={message} />}

        <Link
          href="/firewatch"
          className="inline-block text-[13px] text-duck hover:underline"
        >
          Back to Fire Watch
        </Link>
        {token && status === "error" && (
          <p className="text-[11px] text-zinc-600">
            Token: <span className="font-mono">{token.slice(0, 8)}…</span>
          </p>
        )}
      </div>
    </main>
  );
}
