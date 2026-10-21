"use client";

import { Lightbulb, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Button, Callout, Card, ErrorNote, Skeleton } from "@/components/ui";
import { api } from "@/lib/api";

import type { Leg } from "./model";
import { PlanCard } from "./plan-card";
import type { SuggestResponse, TradePlan } from "./plan-types";

/** The trade plan for the legs in the strategy builder (POST /api/fno/plan). */
export function PlanForLegs({ symbol, expiry, legs }: { symbol: string; expiry: string; legs: Leg[] }) {
  const [plan, setPlan] = useState<TradePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const body = JSON.stringify({ symbol, expiry, legs: legs.map(({ right, strike, side, lots }) => ({ right, strike, side, lots })) });
  useEffect(() => {
    let live = true;
    api<TradePlan>("/api/fno/plan", { method: "POST", body })
      .then((p) => live && (setPlan(p), setError(null)))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [body]);
  return (
    <Card title="Trade plan" icon={<Lightbulb className="size-4" />} subtitle="Verdict, size, entry, stop, target and exits for these legs"
      help="ENTER only when every check passes and a strategy that passed its pre-registered out-of-sample backtest matches; every check passing without one reads 'NO PROVEN EDGE — paper only (0 real lots)'. AVOID when a hard check fails or the matching backtest failed.">
      {error ? <ErrorNote error={error} /> : plan ? <PlanCard plan={plan} /> : <Skeleton className="h-40 rounded-xl" />}
    </Card>
  );
}

/** "Suggest setups": the library's pre-registered recipes applied to today's chain, one plan each. */
export function SuggestSetups({ symbol }: { symbol: string }) {
  const [data, setData] = useState<SuggestResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    try {
      setData(await api<SuggestResponse>(`/api/fno/${encodeURIComponent(symbol)}/suggest`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Suggest setups" icon={<Lightbulb className="size-4" />} subtitle={`The library's recipes on today's ${symbol} chain, each with its backtest result`}
      actions={<Button onClick={run} disabled={busy} icon={busy ? <Loader2 className="size-3.5 animate-spin" /> : <Lightbulb className="size-3.5" />}>{data ? "Refresh" : "Suggest setups"}</Button>}>
      <ErrorNote error={error} onRetry={run} />
      {data ? (
        <div className="space-y-3">
          <Callout tone="info">{data.note}</Callout>
          {data.cards.map((c) => <PlanCard key={c.spec_key ?? c.title} plan={c} />)}
        </div>
      ) : (
        !error && <p className="text-xs text-muted">Press Suggest setups to apply each pre-registered recipe to today&apos;s chain.</p>
      )}
    </Card>
  );
}
