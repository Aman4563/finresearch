"use client";

// Brinson-Fachler attribution by sector against the Nifty 50 (#348): how much of the gap between today's holdings and
// the index came from sector weights, stock picks and both. Loaded only on request (it reads NSE's index weights and
// each holding's prices). A sector whose benchmark weight or return is unknown shows its reason, never 0.

import { Scale } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote, Segmented, Table } from "@/components/ui";
import { day, useApi } from "@/lib/api";

import { Loading, pct, spct } from "./shared-analytics";

type Line = {
  segment: string; wp: number | null; wb: number | null; rp: number | null; rb: number | null;
  allocation: number | null; selection: number | null; interaction: number | null; total: number | null;
  note: string | null; reason: string | null;
};
type Totals = Record<"portfolio_return" | "benchmark_return" | "active" | "allocation" | "selection" | "interaction", number | null>;
type Bench = {
  name: string; members: number; rebuilt_return: number | null; index_return: number | null; difference: number | null;
  tolerance: number; warning: string | null; source: { name: string; url: string; as_of: string | null; fetched_at: string | null };
};
type Attribution = {
  available: boolean; reason: string | null; period: string; method: string; caveats: string[]; disclaimer: string;
  window: { start: string; end: string } | null; benchmark: Bench | null; segments: Line[]; totals: Totals | null;
  excluded: { key: string; name: string; reason: string }[]; benchmark_unknown: { symbol: string; name: string; reason: string }[];
  coverage: number | null; complete: boolean;
};
type Period = "1m" | "3m" | "6m";
const PERIODS: { value: Period; label: string }[] = [{ value: "1m", label: "1M" }, { value: "3m", label: "3M" }, { value: "6m", label: "6M" }];
const pp = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(2)} pp`);

function Lines({ items }: { items: Line[] }) {
  return (
    <Table label="Attribution by sector">
      <thead><tr><th>Sector</th><th className="text-right">You</th><th className="text-right">Nifty 50</th><th className="text-right">Your return</th>
        <th className="text-right">Index return</th><th className="text-right">Allocation</th><th className="text-right">Selection</th>
        <th className="text-right">Interaction</th><th className="text-right">Total</th></tr></thead>
      <tbody>{items.map((x) => (
        <tr key={x.segment}>
          <td className="max-w-[14rem] truncate" title={x.note ?? undefined}>{x.segment}</td>
          <td className="num text-right">{pct(x.wp)}</td><td className="num text-right">{pct(x.wb)}</td>
          <td className="num text-right">{spct(x.rp)}</td><td className="num text-right">{spct(x.rb)}</td>
          {x.reason ? <td colSpan={4} className="text-muted">unknown: {x.reason}</td> : <>
            <td className="num text-right">{pp(x.allocation)}</td><td className="num text-right">{pp(x.selection)}</td>
            <td className="num text-right">{pp(x.interaction)}</td><td className="num text-right font-medium">{pp(x.total)}</td></>}
        </tr>
      ))}</tbody>
    </Table>
  );
}

function Body({ data }: { data: Attribution }) {
  const t = data.totals;
  const b = data.benchmark;
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted">{day(data.window?.start)} to {day(data.window?.end)}: your holdings {spct(t?.portfolio_return, 2)}, {b?.name ?? "Nifty 50"} {spct(t?.benchmark_return, 2)},
        active {t?.active == null ? "unknown" : pp(t.active)} = allocation {pp(t?.allocation)} + selection {pp(t?.selection)} + interaction {pp(t?.interaction)}.</p>
      {b?.warning && <p className="text-xs text-warn"><Badge tone="warn">check</Badge> {b.warning}</p>}
      {!data.complete && data.coverage != null && <p className="text-xs text-warn">Covers {pct(data.coverage)} of your holdings&apos; value; the rest is listed below.</p>}
      <Lines items={data.segments} />
      {data.benchmark_unknown.length > 0 && <ul className="text-xs text-muted">{data.benchmark_unknown.map((u) => <li key={u.symbol}>Index member {u.name}: {u.reason}</li>)}</ul>}
      {data.excluded.length > 0 && <ul className="text-xs text-muted">{data.excluded.map((u) => <li key={u.key}>Left out: {u.name}: {u.reason}</li>)}</ul>}
      {b && <p className="text-[11px] text-muted">Rebuilt index {spct(b.rebuilt_return, 2)} against NSE&apos;s own Nifty 50 {spct(b.index_return, 2)} (tolerance ±{(b.tolerance * 100).toFixed(1)} pp). Weights: {b.source.name}, {b.members} members{b.source.as_of ? ` as of ${day(b.source.as_of)}` : ""}.</p>}
      <p className="text-[11px] text-muted">{data.method} {data.caveats.join(" ")}</p>
    </div>
  );
}

export function AttributionCard({ refresh }: { refresh: number }) {
  const [run, setRun] = useState(false);
  const [period, setPeriod] = useState<Period>("3m");
  const { data, error, reload } = useApi<Attribution>(run ? `/api/portfolio/analytics/attribution?period=${period}&r=${refresh}` : null);
  return (
    <Card title="Attribution vs Nifty 50" icon={<Scale className="size-4" />}
      help="Brinson-Fachler: allocation is the effect of over- or under-weighting sectors, selection the effect of the stocks you picked inside a sector, interaction the two together. They add up to the gap between your holdings and the index. Today's holdings as if held through the window; past, not advice.">
      {!run ? (
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          <p className="flex-1">Compares today&apos;s holdings with the Nifty 50 sector by sector. It reads NSE&apos;s Nifty 50 member list, one quote per member for its free-float market cap (the first run can take a minute or two) and the stored daily bhavcopy.</p>
          <Button variant="secondary" onClick={() => setRun(true)}>Attribute returns</Button>
        </div>
      ) : (
        <div className="space-y-2">
          <Segmented label="Window" value={period} onChange={setPeriod} options={PERIODS} />
          {error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <Loading what="attribution" /> : !data.available ? (
            <p className="text-sm text-muted">{data.reason}</p>
          ) : <Body data={data} />}
        </div>
      )}
    </Card>
  );
}
