"use client";

// Days to liquidate (#348): each holding's units against its average daily NSE volume at a chosen participation rate.
// Loaded only on request. A holding without enough stored volume shows its reason, never 0 days; funds are "not
// applicable" (redeemed at NAV), not "instantly liquid".

import { Droplets } from "lucide-react";
import { useState } from "react";

import { Button, Card, ErrorNote, Segmented, Table } from "@/components/ui";
import { day, useApi } from "@/lib/api";

import { Loading, pct } from "./shared-analytics";

type Row = {
  key: string; name: string; asset_type: string; units: number; value: number; weight: number | null; symbol: string | null;
  adv_shares: number | null; adv_rupees: number | null; pct_of_adv: number | null; days: number | null;
  status: "known" | "unknown" | "not_applicable"; reason: string | null;
};
type Liquidity = {
  available: boolean; reason: string | null; participation: number; adv_sessions: number; method: string; as_of: string;
  source: { name: string; note: string }; positions: Row[];
  summary: { within: Record<string, number | null>; unknown: number | null; not_applicable: number | null; max_days: number | null; complete: boolean };
};
const RATES = [{ value: "0.1", label: "10 %" }, { value: "0.2", label: "20 %" }, { value: "0.33", label: "33 %" }];
const n0 = (v: number | null) => (v == null ? "—" : Math.round(v).toLocaleString("en-IN"));

export function LiquidityCard({ refresh }: { refresh: number }) {
  const [run, setRun] = useState(false);
  const [rate, setRate] = useState("0.2");
  const { data, error, reload } = useApi<Liquidity>(run ? `/api/portfolio/analytics/liquidity?participation=${rate}&r=${refresh}` : null);
  const s = data?.summary;
  return (
    <Card title="Days to liquidate" icon={<Droplets className="size-4" />}
      help="How many trading days it would take to sell each holding if you sold no more than a set share (participation) of its usual daily volume on NSE. A rule of thumb for how quickly you could exit without moving the price much, not a promise.">
      {!run ? (
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          <p className="flex-1">Compares each holding with its average daily NSE volume over the last 20 stored sessions.</p>
          <Button variant="secondary" onClick={() => setRun(true)}>Estimate days</Button>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted"><span>Participation</span>
            <Segmented label="Participation" value={rate} onChange={setRate} options={RATES} /></div>
          {error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <Loading what="liquidity" /> : !data.available ? (
            <p className="text-sm text-muted">{data.reason}</p>
          ) : (
            <>
              {s && <p className="text-xs text-muted">Sellable within 1 day: {s.complete ? "" : "at least "}{pct(s.within["1"])}; 5 days: {s.complete ? "" : "at least "}{pct(s.within["5"])}; 20 days: {s.complete ? "" : "at least "}{pct(s.within["20"])}.
                {s.unknown ? ` Unknown: ${pct(s.unknown)}.` : ""}{s.not_applicable ? ` Funds (redeemed, not sold on the exchange): ${pct(s.not_applicable)}.` : ""}</p>}
              <Table label="Days to liquidate">
                <thead><tr><th>Holding</th><th className="text-right">Weight</th><th className="text-right">Avg daily volume</th>
                  <th className="text-right">Your units, % of a day</th><th className="text-right">Days</th></tr></thead>
                <tbody>{data.positions.map((r) => (
                  <tr key={r.key}><td className="max-w-[14rem] truncate">{r.name}</td><td className="num text-right">{pct(r.weight)}</td>
                    {r.status === "known" ? <>
                      <td className="num text-right">{n0(r.adv_shares)}</td><td className="num text-right">{r.pct_of_adv?.toFixed(1)}%</td>
                      <td className="num text-right font-medium">{r.days?.toFixed(1)}</td></>
                      : <td colSpan={3} className="text-muted">{r.status === "not_applicable" ? "n/a: " : "unknown: "}{r.reason}</td>}
                  </tr>
                ))}</tbody>
              </Table>
              <p className="text-[11px] text-muted">{data.method} Source: {data.source.name}; {data.source.note} As of {day(data.as_of)}.</p>
            </>
          )}
        </div>
      )}
    </Card>
  );
}
