"use client";

import { FlaskConical } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { CriteriaList, SetupView, StrategyCard } from "@/components/fno/backtest";
import type { BacktestsResponse } from "@/components/fno/backtest-types";
import { RiskBanner } from "@/components/fno/risk";
import { Badge, Callout, Card, EmptyState, ErrorNote, PageHeader, Skeleton } from "@/components/ui";
import { api } from "@/lib/api";

export default function BacktestsPage() {
  const [data, setData] = useState<BacktestsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setData(await api<BacktestsResponse>("/api/fno/backtests"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);
  const reg = data?.registration;
  const run = data?.run;

  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      <PageHeader icon={<FlaskConical className="size-5" />} eyebrow="Research" title="F&O strategy backtests"
        description="A few strategies fixed in advance, replayed on NSE's end-of-day F&O history with real charges, STT and slippage, and judged out of sample against criteria written down before the run. A strategy that passes is a candidate with evidence, never a guarantee; one that fails says so."
        actions={<Link href="/fno/lab" className="text-sm text-brand hover:underline">Strategy lab: test your own →</Link>} />
      <RiskBanner />
      <ErrorNote error={error} onRetry={load} />
      {!data ? (
        !error && <Skeleton className="h-40 rounded-xl" />
      ) : (
        <>
          <Callout tone={reg!.matches_code ? "info" : "warn"} title={`Pre-registered ${reg!.registered_on} · ${reg!.variants_tried} variants · hash ${reg!.hash.slice(0, 12)}`}>
            In-sample {reg!.windows.is.join(" → ")}, out-of-sample from {reg!.windows.oos_from}. {reg!.note}{" "}
            {!reg!.matches_code && "The specs in the code no longer match the registration: out-of-sample runs are refused. "}
            Details: <code>{reg!.doc}</code>. Multiple-testing adjustment: {reg!.deflated_sharpe_source}
          </Callout>
          {!run ? (
            <EmptyState icon={<FlaskConical className="size-5" />} title="No backtest saved yet">
              Run <code>finresearch fno backtest run</code> after the F&amp;O history is backfilled (<code>finresearch fno backfill --from 2022-01-01</code>).
            </EmptyState>
          ) : (
            <>
              <Card title="Candidates" subtitle={`Run ${run.id} on ${run.source}, ${run.generated_at.slice(0, 10)} · out of sample ${run.windows.oos ? run.windows.oos.join(" → ") : "not evaluated"}`}>
                {run.candidates.length === 0 ? (
                  <p className="text-sm"><Badge tone="loss">none</Badge> No strategy passed every pre-registered criterion out of sample. Nothing here has shown an edge after costs.</p>
                ) : (
                  <div className="space-y-4">
                    <p className="text-xs text-muted">{run.note}</p>
                    {run.candidates.map((c) => (
                      <div key={c.key} className="space-y-2 border-b border-border/60 pb-3 last:border-0">
                        <p className="font-medium"><Link className="text-brand hover:underline" href={`#${c.key}`}>{c.name}</Link> <Badge tone="gain">candidate</Badge></p>
                        {c.criteria && <CriteriaList items={c.criteria} />}
                        <SetupView s={c.setup} />
                      </div>
                    ))}
                  </div>
                )}
              </Card>
              {run.strategies.map((r) => <StrategyCard key={r.key} r={r} />)}
            </>
          )}
        </>
      )}
    </div>
  );
}
