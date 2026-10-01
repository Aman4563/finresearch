"use client";

// The Costs tab: the weighted expense ratio of your funds (AMFI's monthly TER file) and, for regular plans, what a
// switch to the direct plan would cost now (tax, exit load, stamp duty) before the yearly saving and the break-even.

import { BadgeIndianRupee, CheckCircle2, Percent, Receipt } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Callout, Card, ErrorNote, InfoTip, Stat, Table, cx, inputClass } from "@/components/ui";
import { api, day, useApi } from "@/lib/api";

import { AnalyticsFooter, type AnalyticsBase, Loading, NotEnough } from "./shared-analytics";
import { inr } from "./types";

type Switch = {
  key: string; name: string; holding_ids: number[]; value: number; yearly_saving: number; ten_year_difference: number;
  break_even_years: number | null; text: string; exit_load_input: { pct: number; days: number } | null;
  costs: { value: number; gain: number; tax: number; exit_load: number | null; exit_load_known: boolean; stamp: number; unknown_cost_lots: number; total: number };
};
type Costs = AnalyticsBase & {
  ter_source?: string | null; weighted_ter_pct?: number | null; yearly_cost?: number; matched_value?: number; status?: string; suppressed?: number;
  funds?: { key: string; name: string; plan: string; value: number; ter_pct: number; direct_ter_pct: number | null; regular_ter_pct: number | null; ter_day: string; yearly_cost: number }[];
  unmatched?: string[]; switches?: Switch[];
  assumptions?: { gross_return: number; trivial: string; tax: string; exit_load: string; service: string };
};

function LoadInput({ s, onSaved }: { s: Switch; onSaved: () => void }) {
  const [pct, setPct] = useState(s.exit_load_input ? String(s.exit_load_input.pct) : "");
  const [days, setDays] = useState(s.exit_load_input ? String(s.exit_load_input.days) : "365");
  const save = async () => {
    await api("/api/portfolio/analytics/settings", { method: "PUT", body: JSON.stringify({ exit_loads: Object.fromEntries(s.holding_ids.map((id) => [String(id), { pct: Number(pct), days: Number(days) }])) }) });
    onSaved();
  };
  return (
    <span className="inline-flex flex-wrap items-center gap-1 text-xs">
      Exit load <input aria-label="Exit load %" inputMode="decimal" value={pct} onChange={(e) => setPct(e.target.value)} placeholder="1" className={cx(inputClass, "h-7 w-14 num text-xs")} />%
      within <input aria-label="Exit load period in days" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value)} className={cx(inputClass, "h-7 w-16 num text-xs")} /> days
      <Button variant="ghost" disabled={pct === "" || Number.isNaN(Number(pct)) || Number.isNaN(Number(days))} onClick={save}>Save</Button>
    </span>
  );
}

export function CostsAnalytics({ refresh }: { refresh: number }) {
  const [tick, setTick] = useState(0);
  const { data, error, reload } = useApi<Costs>(`/api/portfolio/analytics/costs?r=${refresh}-${tick}`);
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <Loading what="fund costs" />;
  if (!data.available) return <NotEnough data={data} what="fund cost figures" />;
  const sw = data.switches ?? [], a = data.assumptions!;
  return (
    <div className="space-y-4">
      <div className="stagger grid gap-3 sm:grid-cols-3">
        <Stat label="Weighted expense ratio" display={<span className="num">{data.weighted_ter_pct?.toFixed(2)}%</span>} icon={<Percent className="size-4" />}
          hint="a year, by value" help="Each fund's total expense ratio (TER) weighted by how much you hold in it. The TER is taken out of the NAV every day, so your returns are already net of it." />
        <Stat label="Yearly cost at today's value" display={<span className="num">{inr(data.yearly_cost)}</span>} icon={<BadgeIndianRupee className="size-4" />}
          hint={`on ${inr(data.matched_value)} of funds`} />
        <Stat label="Regular plans to review" display={<span className="num">{sw.length}</span>} icon={<Receipt className="size-4" />}
          hint={data.suppressed ? `${data.suppressed} trivial one(s) not shown` : "material savings only"} />
      </div>
      {sw.length === 0
        ? <Callout tone="gain" icon={<CheckCircle2 className="size-4" />} title={data.status}>{data.suppressed ? `${data.suppressed} regular plan(s) would save too little to matter (${a.trivial}).` : "Every fund matched is a direct plan, or its direct plan is not cheaper."}</Callout>
        : (
          <Card title="Regular → direct: costs first" icon={<Receipt className="size-4" />}
            help="Switching is a redemption (a taxable event, possibly with an exit load) followed by a fresh purchase of the direct plan. The costs are paid now; the saving accrues every year you stay invested.">
            <div className="space-y-3">
              {sw.map((s) => (
                <div key={s.key} className="rounded-lg border border-border p-3">
                  <p className="text-sm font-medium">{s.name} <span className="text-xs font-normal text-muted">· {inr(s.value)}</span></p>
                  <div className="mt-2 grid gap-2 text-xs sm:grid-cols-5">
                    <div><p className="text-muted">Tax now <InfoTip>{a.tax}</InfoTip></p><p className="num font-medium">{inr(s.costs.tax)}</p></div>
                    <div><p className="text-muted">Exit load <InfoTip>{a.exit_load}</InfoTip></p><p className="num font-medium">{s.costs.exit_load_known ? inr(s.costs.exit_load) : <Badge tone="warn">unknown</Badge>}</p></div>
                    <div><p className="text-muted">Stamp duty</p><p className="num font-medium">{inr(s.costs.stamp)}</p></div>
                    <div><p className="text-muted">Saving a year</p><p className="num font-medium text-gain">{inr(s.yearly_saving)}</p></div>
                    <div><p className="text-muted">Break-even <InfoTip>One-off switch cost ÷ yearly saving at today&apos;s value.</InfoTip></p><p className="num font-medium">{s.break_even_years == null ? "—" : `${s.break_even_years} yr`}</p></div>
                  </div>
                  <p className="mt-2 text-xs text-muted">{s.text} Over 10 years the difference is about {inr(s.ten_year_difference)}, assuming a {Math.round(a.gross_return * 100)} % gross return a year (an illustration, not a forecast).</p>
                  <div className="mt-2"><LoadInput s={s} onSaved={() => setTick((t) => t + 1)} /></div>
                </div>
              ))}
              <p className="text-[11px] text-muted">{a.service}</p>
            </div>
          </Card>
        )}
      <Card padded={false} title="Expense ratios" subtitle={data.ter_source ?? undefined}>
        <Table label="Expense ratios" className="!mx-0">
          <thead><tr><th>Fund</th><th>Plan</th><th className="text-right">Value</th><th className="text-right">TER</th><th className="text-right">Direct / regular</th><th className="text-right">Cost a year</th></tr></thead>
          <tbody>{(data.funds ?? []).map((f) => (
            <tr key={f.key}><td className="max-w-[18rem] truncate">{f.name}</td><td className="text-xs">{f.plan}</td><td className="num text-right">{inr(f.value)}</td>
              <td className="num text-right font-medium">{f.ter_pct.toFixed(2)}%</td>
              <td className="num text-right text-xs text-muted">{f.direct_ter_pct?.toFixed(2) ?? "—"} / {f.regular_ter_pct?.toFixed(2) ?? "—"}% <span className="block">{day(f.ter_day)}</span></td>
              <td className="num text-right">{inr(f.yearly_cost)}</td></tr>
          ))}</tbody>
        </Table>
        {(data.unmatched ?? []).length > 0 && <p className="px-4 py-2 text-[11px] text-warn">Not found in AMFI&apos;s TER file (name mismatch): {data.unmatched!.join(", ")}.</p>}
      </Card>
      <AnalyticsFooter data={data} />
    </div>
  );
}
