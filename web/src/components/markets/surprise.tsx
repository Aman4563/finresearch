"use client";

// Earnings surprise (issue #181): the latest quarters' SUE against the company's own history (seasonal random walk),
// the latest SUE's decile among the pre-registered experiment's events, and the [0,+1] reaction vs NIFTYBEES.
// Informational: labelled "experimental — not part of the signal" unless the experiment passed
// (GET /api/stocks/{symbol}/surprises; evals/experiments/earnings_surprise/RESULTS.md).

import { FlaskConical, Info } from "lucide-react";

import { signedPct } from "@/components/markets/common";
import { Badge, Callout, Card, ErrorNote, Skeleton, Table } from "@/components/ui";
import { day, useApi } from "@/lib/api";

type SurpriseQuarter = {
  quarter_end: string; label: string; announced: string; announcement_plausible: boolean;
  eps: number | null; eps_year_ago: number | null; sue: number | null; sue_reason: string | null;
  revenue_sue: number | null; decile: number | null; t0?: string | null; reaction?: number | null;
  reaction_note?: string | null;
};

type StockSurprises = {
  status: "ok" | "no_data" | "unsupported"; symbol: string; message?: string; experimental?: boolean;
  label?: string | null; basis?: string; quarters?: SurpriseQuarter[]; latest?: SurpriseQuarter | null;
  errors?: string[]; reference?: { n?: number; period?: string | null; universe?: string; source?: string };
  method?: string; benchmark?: string;
};

const num = (x: number | null | undefined, digits = 2) => (x == null ? "—" : `${x > 0 ? "+" : ""}${x.toFixed(digits)}`);

function decileText(q: SurpriseQuarter, s: StockSurprises): string {
  if (q.decile == null) return "no reference yet";
  const ref = s.reference;
  return `decile ${q.decile} of 10 vs ${ref?.n ?? "?"} ${ref?.universe ?? ""} results${ref?.period ? ` (${ref.period.replaceAll("..", " to ")})` : ""}`;
}

export function StockSurpriseCard({ symbol }: { symbol: string }) {
  const r = useApi<StockSurprises>(`/api/stocks/${encodeURIComponent(symbol)}/surprises`);
  const d = r.data;
  const last = d?.latest ?? null;
  return (
    <Card title="Earnings surprise" icon={<FlaskConical className="size-4" />}
      subtitle={d?.label ?? "Results vs the same quarter a year earlier"}
      help={`SUE: this quarter's EPS minus the same quarter a year earlier, divided by how much that yearly change has varied over the previous 8 quarters (Bernard & Thomas 1990). No analyst consensus is used. Reaction: the stock's return minus ${d?.benchmark ?? "NIFTYBEES"} from the close before the results to the close of the next session (results after 15:30 IST count from the next session).`}>
      {r.error && <ErrorNote error={r.error} onRetry={r.reload} />}
      {!d && !r.error && <Skeleton className="h-[140px] w-full rounded-lg" />}
      {d && d.status !== "ok" && <Callout tone="info" icon={<Info className="size-4" />}>{d.message ?? "No quarterly results to measure."}</Callout>}
      {d?.status === "ok" && (
        <div className="space-y-3">
          {d.experimental && (
            <p className="text-xs text-muted"><Badge tone="warn">Experimental</Badge> Not part of the signal: the pre-registered test has not passed (see {d.reference?.source ?? "RESULTS.md"}).</p>
          )}
          {last && (
            <div className="grid gap-2 sm:grid-cols-3">
              <div><div className="text-xs text-muted">Latest SUE ({last.label})</div><div className="text-lg font-semibold tabular-nums">{num(last.sue)}</div>
                <div className="text-xs text-muted">{last.sue == null ? last.sue_reason : decileText(last, d)}</div></div>
              <div><div className="text-xs text-muted">Reaction [0, +1]</div><div className="text-lg font-semibold tabular-nums">{signedPct(last.reaction)}</div>
                <div className="text-xs text-muted">{last.t0 ? `t0 ${day(last.t0)}` : ""}{last.reaction == null && last.reaction_note ? ` · ${last.reaction_note}` : ""}</div></div>
              <div><div className="text-xs text-muted">Revenue SUE</div><div className="text-lg font-semibold tabular-nums">{num(last.revenue_sue)}</div>
                <div className="text-xs text-muted">{d.basis} results, as first reported</div></div>
            </div>
          )}
          <Table label={`${symbol}: earnings surprise by quarter`} className="!mx-0">
            <thead><tr><th>Quarter</th><th>Announced</th><th className="text-right">EPS</th><th className="text-right">Year ago</th><th className="text-right">SUE</th><th className="text-right">Revenue SUE</th></tr></thead>
            <tbody>
              {(d.quarters ?? []).map((q) => (
                <tr key={q.quarter_end}>
                  <td>{q.label}</td>
                  <td>{day(q.announced.slice(0, 10))}{!q.announcement_plausible && <span className="ml-1 text-[10px] text-muted" title="NSE's broadcast date is past the legal deadline: probably a re-upload, not the announcement.">re-upload?</span>}</td>
                  <td className="text-right tabular-nums">{q.eps?.toFixed(2) ?? "—"}</td>
                  <td className="text-right tabular-nums">{q.eps_year_ago?.toFixed(2) ?? "—"}</td>
                  <td className="text-right tabular-nums" title={q.sue_reason ?? undefined}>{num(q.sue)}</td>
                  <td className="text-right tabular-nums">{num(q.revenue_sue)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
          {(d.errors?.length ?? 0) > 0 && <p className="text-xs text-muted">Not read: {d.errors!.slice(0, 3).join("; ")}</p>}
        </div>
      )}
    </Card>
  );
}
