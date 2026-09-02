"use client";

/**
 * Email alert subscriptions by region.
 *
 * Subscribers are notified when new analysis cells in a municipality cross into
 * High or Very high priority after a score refresh.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type AlertRegion, type AlertSubscription } from "@/lib/api";
import { Callout, ErrorNote, Panel, Spinner } from "@/components/ui";

interface Props {
  regions: AlertRegion[];
}

export default function AlertsPanel({ regions }: Props) {
  const [email, setEmail] = useState("");
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [subscriptions, setSubscriptions] = useState<AlertSubscription[] | null>(null);
  const [emailEnabled, setEmailEnabled] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const ingestedRegions = useMemo(
    () => regions.filter((region) => region.ingested),
    [regions],
  );

  useEffect(() => {
    api
      .alertStatus()
      .then((status) => setEmailEnabled(status.email_enabled))
      .catch(() => setEmailEnabled(false));
  }, []);

  const loadSubscriptions = useCallback(async () => {
    const trimmed = email.trim();
    if (!trimmed) {
      setSubscriptions(null);
      setError("Enter your email to load or update subscriptions.");
      return;
    }
    setLoading(true);
    setError(null);
    setSaved(null);
    try {
      const result = await api.lookupAlerts(trimmed);
      setSubscriptions(result.subscriptions);
      const next: Record<string, boolean> = {};
      for (const region of ingestedRegions) {
        next[region.id] = result.subscriptions.some(
          (sub) => sub.municipality_id === region.id,
        );
      }
      setSelected(next);
    } catch (e) {
      setSubscriptions(null);
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [email, ingestedRegions]);

  const save = async () => {
    const trimmed = email.trim();
    if (!trimmed) {
      setError("Enter your email address.");
      return;
    }
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      const municipalityIds = ingestedRegions
        .filter((region) => selected[region.id])
        .map((region) => region.id);
      const result = await api.updateAlertSubscriptions(trimmed, municipalityIds);
      setSubscriptions(result.subscriptions);
      setSaved(
        result.added.length || result.removed.length
          ? "Subscriptions updated."
          : "No changes.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="h-full min-h-0 overflow-y-auto overscroll-contain">
      <Panel
        title="High priority email alerts"
        subtitle="For fire halls, dispatch, and municipal ops. We email you when new cells cross into High or Very high after the daily score refresh."
      >
        {emailEnabled === false && (
          <Callout tone="warn">
            Outbound email is not configured on this server yet. You can still save
            subscriptions; they will take effect once SMTP is enabled.
          </Callout>
        )}

        <label className="mt-3 block text-[10px] uppercase tracking-wider text-zinc-500">
          Email
        </label>
        <input
          type="email"
          autoComplete="email"
          value={email}
          onChange={(event) => {
            setEmail(event.target.value);
            setSaved(null);
          }}
          placeholder="chief@example.org"
          className="mt-1 w-full rounded border border-white/10 bg-black/40 px-2.5 py-2 text-[14px] text-zinc-200 outline-none focus:border-duck/60 lg:text-[13px]"
        />

        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={loadSubscriptions}
            disabled={loading || !email.trim()}
            className="min-h-9 rounded border border-white/10 px-3 py-2 text-[13px] text-zinc-300 hover:text-white disabled:opacity-40 lg:min-h-0 lg:py-1.5 lg:text-[12px]"
          >
            {loading ? "Loading…" : "Load subscriptions"}
          </button>
          <button
            type="button"
            onClick={save}
            disabled={saving || !email.trim()}
            className="min-h-9 rounded bg-duck px-3 py-2 text-[13px] font-medium text-black hover:opacity-90 disabled:opacity-40 lg:min-h-0 lg:py-1.5 lg:text-[12px]"
          >
            {saving ? "Saving…" : "Save subscriptions"}
          </button>
        </div>

        {error && (
          <div className="mt-3">
            <ErrorNote error={error} />
          </div>
        )}
        {saved && (
          <div className="mt-3">
            <Callout>{saved}</Callout>
          </div>
        )}

        <div className="mt-4 space-y-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">Regions</div>
          {ingestedRegions.length === 0 && (
            <Callout tone="gap">No ingested regions are available for alerts yet.</Callout>
          )}
          {ingestedRegions.map((region) => (
            <label
              key={region.id}
              className="flex cursor-pointer items-start gap-2 rounded border border-white/5 bg-white/[0.02] px-2.5 py-2.5 text-[13px] lg:py-2 lg:text-[12px]"
            >
              <input
                type="checkbox"
                checked={Boolean(selected[region.id])}
                onChange={(event) =>
                  setSelected((prev) => ({ ...prev, [region.id]: event.target.checked }))
                }
                className="mt-0.5 accent-duck"
              />
              <span>
                <span className="text-zinc-200">{region.short_name}</span>
                <span className="block text-[10px] text-zinc-600">
                  {region.name} · {region.province}
                </span>
              </span>
            </label>
          ))}
        </div>

        {subscriptions && subscriptions.length > 0 && (
          <div className="mt-4">
            <Callout>
              Currently subscribed to{" "}
              {subscriptions.map((sub) => sub.short_name).join(", ")}. Each alert email
              includes an unsubscribe link for that region.
            </Callout>
          </div>
        )}

        <div className="mt-4">
          <Callout>
            Alerts fire when cells newly cross into High (score ≥ 0.60), not on every
            daily refresh. This is a model output, not a confirmed incident.
          </Callout>
        </div>
      </Panel>
    </div>
  );
}
