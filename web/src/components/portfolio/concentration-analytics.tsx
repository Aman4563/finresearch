"use client";

// The Concentration tab: single-stock, sector and business-group weights against your limits (the profile's max
// position, else labelled rules of thumb), HHI and the effective number of holdings. Business groups come from a small
// [unverified] starting map that you can correct here.

import { Building2, CheckCircle2, Layers, PieChart, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { BarsChart } from "@/components/charts";
import { Badge, Button, Callout, Card, ErrorNote, InfoTip, Stat, Table, cx, inputClass } from "@/components/ui";
import { api, useApi } from "@/lib/api";

import { LookthroughConcentration } from "./lookthrough-panel";
import { AnalyticsFooter, type AnalyticsBase, Loading, NotEnough } from "./shared-analytics";
import { inr } from "./types";

type Row = { label: string; value: number; weight_pct: number };
type Conc = AnalyticsBase & {
  total?: number; hhi?: number; n_effective?: number; top5_pct?: number; status?: string; group_source?: string; funds_note?: string | null;
  positions?: { key: string; name: string; asset_type: string; value: number; weight_pct: number; sector: string; group: string | null; group_source: string; accounts: number }[];
  sectors?: Row[]; groups?: Row[];
  flags?: { kind: string; label: string; weight_pct: number; limit_pct: number; text: string }[];
  limits?: { stock_pct: number; stock_source: string; sector_pct: number; group_pct: number };
};

function GroupEditor({ k, current, onSaved }: { k: string; current: string | null; onSaved: () => void }) {
  const [v, setV] = useState(current ?? "");
  const [busy, setBusy] = useState(false);
  const save = async (value: string | null) => {
    setBusy(true);
    try {
      await api("/api/portfolio/analytics/settings", { method: "PUT", body: JSON.stringify({ groups: { [k]: value } }) });
      onSaved();
    } finally { setBusy(false); }
  };
  return (
    <span className="inline-flex items-center gap-1">
      <input aria-label="Business group" value={v} onChange={(e) => setV(e.target.value)} placeholder="none" className={cx(inputClass, "h-7 w-28 text-xs")} />
      <Button variant="ghost" disabled={busy || v === (current ?? "")} onClick={() => save(v)}>Save</Button>
    </span>
  );
}

const bar = (rows: Row[], limit: number) => (
  <BarsChart data={rows.slice(0, 10).map((r) => ({ label: r.label.length > 24 ? r.label.slice(0, 23) + "…" : r.label, weight: r.weight_pct }))} x="label"
    layout="vertical" labelWidth={160} height={Math.max(140, Math.min(rows.length, 10) * 30 + 30)} format={(v) => `${v.toFixed(1)}%`}
    series={[{ key: "weight", label: "Share of portfolio", color: "var(--chart-1)" }]} reference={{ value: limit, label: "" }} />
);

export function ConcentrationAnalytics({ refresh }: { refresh: number }) {
  const [tick, setTick] = useState(0);
  const { data, error, reload } = useApi<Conc>(`/api/portfolio/analytics/concentration?r=${refresh}-${tick}`);
  const [edit, setEdit] = useState(false);
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <Loading what="concentration figures" />;
  if (!data.available || !data.positions) return <NotEnough data={data} what="concentration figures" />;
  const flags = data.flags ?? [], lim = data.limits!;
  const stocks = data.positions.filter((p) => p.asset_type === "stock");
  return (
    <div className="space-y-4">
      {flags.length === 0
        ? <Callout tone="gain" icon={<CheckCircle2 className="size-4" />} title={data.status}>No single stock above your limit ({lim.stock_source}), no sector above {lim.sector_pct}% and no business group above {lim.group_pct}% (rules of thumb).</Callout>
        : <Callout tone="warn" icon={<TriangleAlert className="size-4" />} title={data.status}><ul className="list-disc space-y-0.5 pl-4">{flags.map((f) => <li key={f.kind + f.label}>{f.text}</li>)}</ul></Callout>}
      <div className="stagger grid gap-3 sm:grid-cols-3">
        <Stat label="Effective number of holdings" display={<span className="num">{data.n_effective?.toFixed(1)}</span>} icon={<Layers className="size-4" />}
          hint={`${data.positions.length} positions`} help="1 ÷ HHI: the number of equal-sized holdings that would be as concentrated as yours. Ten holdings where one is 60 % behave like about two." />
        <Stat label="Largest five" display={<span className="num">{data.top5_pct?.toFixed(1)}%</span>} icon={<PieChart className="size-4" />} hint="share of the portfolio" />
        <Stat label="HHI" display={<span className="num">{data.hhi?.toFixed(3)}</span>} icon={<Building2 className="size-4" />}
          hint="0 = spread out, 1 = one holding" help="Herfindahl–Hirschman index: the sum of squared weights. Funds count as one position each here." />
      </div>
      {data.funds_note && <p className="text-xs text-muted">{data.funds_note}</p>}
      {data.funds_note && <LookthroughConcentration />}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="By sector" icon={<PieChart className="size-4" />} subtitle={`dashed line: ${lim.sector_pct} % rule of thumb`} help={`Stocks by NSE industry (or your own label on the holding). Rule of thumb [W]: no sector above ${lim.sector_pct} %.`}>
          {bar(data.sectors ?? [], lim.sector_pct)}
        </Card>
        <Card title="By business group (stocks)" icon={<Building2 className="size-4" />} subtitle={`dashed line: ${lim.group_pct} % rule of thumb`} help={`${data.group_source} Rule of thumb [W]: no group above ${lim.group_pct} %.`}
          actions={<button type="button" className="text-xs text-brand" onClick={() => setEdit((e) => !e)}>{edit ? "Done" : "Correct groups"}</button>}>
          {bar(data.groups ?? [], lim.group_pct)}
        </Card>
      </div>
      <Card padded={false} title="Positions" subtitle={`A stock held in several accounts counts once · valued at ${inr(data.total)}`}>
        <Table label="Positions" className="!mx-0">
          <thead><tr><th>Holding</th><th className="text-right">Value</th><th className="text-right">Weight</th><th>Sector</th>
            <th><span className="inline-flex items-center gap-1">Group <InfoTip>{data.group_source}</InfoTip></span></th></tr></thead>
          <tbody>{data.positions.map((p) => (
            <tr key={p.key}>
              <td className="max-w-[16rem]"><span className="block truncate font-medium">{p.name}</span><span className="text-[11px] text-muted">{p.key}{p.accounts > 1 ? ` · ${p.accounts} accounts` : ""}</span></td>
              <td className="num text-right">{inr(p.value)}</td>
              <td className={cx("num text-right", p.asset_type === "stock" && p.weight_pct > lim.stock_pct && "font-medium text-warn")}>{p.weight_pct.toFixed(1)}%</td>
              <td className="text-xs">{p.sector}</td>
              <td className="text-xs">
                {edit && p.asset_type === "stock" ? <GroupEditor k={p.key} current={p.group} onSaved={() => setTick((t) => t + 1)} />
                  : p.group ? <span>{p.group} {p.group_source !== "your map" && <Badge tone="neutral">unverified</Badge>}</span> : <span className="text-muted">—</span>}
              </td>
            </tr>
          ))}</tbody>
        </Table>
      </Card>
      {stocks.length === 0 && <p className="text-xs text-muted">No direct stock holdings: single-stock and group limits do not apply.</p>}
      <AnalyticsFooter data={data} />
    </div>
  );
}
