"use client";

// The Tax tab: realised capital gains per financial year from the dated rule table (fincalc/tax.py), the ₹1.25 lakh
// LTCG exemption meter, harvesting ideas, a CSV for your CA and the AIS check. A personal estimate: verify with a CA.

import { CalendarClock, Download, Leaf, Scale, Scissors, ShieldAlert, TrendingDown, TrendingUp } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge, Callout, Card, EmptyState, ErrorNote, InfoTip, Progress, Segmented, SkeletonRows, Stat, Table, cx } from "@/components/ui";
import { API_URL, day, useApi } from "@/lib/api";

import { AisCheck } from "./ais-check";
import { type HarvestIdea, TAX_CLASS_LABEL, type TaxView, inr, signed, units } from "./types";

const BUCKET: Record<string, string> = {
  equity_st: "Equity STCG", equity_lt: "Equity LTCG", slab_st: "STCG at slab rate", other_lt: "Other LTCG",
};

function Ideas({ title, icon, items, kind, help }: { title: string; icon: React.ReactNode; items: HarvestIdea[]; kind: "gain" | "loss"; help: string }) {
  return (
    <Card title={title} icon={icon} help={help}>
      {items.length === 0 ? <p className="text-sm text-muted">{kind === "gain" ? "Nothing to harvest: no long-term equity gains fit in the unused exemption, or no live prices." : "No loss that would lower this year's tax (it needs realised taxable gains and a holding priced below cost)."}</p> : (
        <Table label={title}>
          <thead><tr><th>Holding</th><th className="text-right">Sell units</th><th className="text-right">Value</th><th className="text-right">{kind === "gain" ? "Tax-free gain" : "Loss booked"}</th><th className="text-right">{kind === "gain" ? "Future tax saved ≤" : "Tax saved now"}</th><th className="text-right">Est. costs</th></tr></thead>
          <tbody>
            {items.map((i) => (
              <tr key={i.holding_id}>
                <td className="max-w-[16rem]"><span className="block truncate font-medium">{i.name}</span><span className="text-[11px] text-muted">{i.account} · oldest lots first (FIFO){i.short_term ? " · includes short-term" : ""}</span></td>
                <td className="num text-right">{units(i.sell_units)}</td>
                <td className="num text-right">{inr(i.value)}</td>
                <td className={cx("num text-right", kind === "gain" ? "text-gain" : "text-loss")}>{inr(kind === "gain" ? i.gain : i.loss)}</td>
                <td className="num text-right font-medium">{inr(kind === "gain" ? i.future_tax_saved_up_to : i.tax_saved)}</td>
                <td className="num text-right text-muted">{inr(i.est_costs)}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

export function TaxPanel({ refresh }: { refresh: number }) {
  const { data, error, reload } = useApi<TaxView>(`/api/portfolio/tax?r=${refresh}`);
  const [fy, setFy] = useState<number | null>(null);
  const [showRules, setShowRules] = useState(false);
  const current = data?.fys.find((f) => f.fy === (fy ?? data?.harvest.fy)) ?? data?.fys[0];
  const rows = useMemo(() => (data?.disposals ?? []).filter((d) => current && d.fy === current.fy), [data, current]);

  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data || !current) return <Card><SkeletonRows rows={6} /></Card>;
  const isCurrent = current.fy === data.harvest.fy;

  return (
    <div className="space-y-4">
      <Callout tone="warn" icon={<ShieldAlert className="size-4" />} title="Verify with a chartered accountant">
        {data.verify}
      </Callout>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented value={String(current.fy)} onChange={(v) => setFy(Number(v))}
          options={data.fys.slice(0, 5).map((f) => ({ value: String(f.fy), label: f.label.replace("FY ", "FY") }))} />
        <a href={`${API_URL}/api/portfolio/tax.csv?fy=${current.fy}`} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-card px-3 text-xs font-medium ring-1 ring-inset ring-border hover:bg-card-hover">
          <Download className="size-3.5" />CSV for your CA ({current.label})
        </a>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Short-term gains" value={current.stcg} format={(n) => inr(n)} icon={<TrendingUp className="size-4" />} tone={current.stcg >= 0 ? "gain" : "loss"}
          help="STCG: sold within 12 months (listed equity) or 24 months (most other assets); debt funds bought after 1-Apr-2023 are always short-term." />
        <Stat label="Long-term gains" value={current.ltcg} format={(n) => inr(n)} icon={<Leaf className="size-4" />} tone={current.ltcg >= 0 ? "gain" : "loss"}
          help="LTCG: held longer than the threshold. Listed equity: 12.5 % above ₹1.25 lakh a year (10 % above ₹1 lakh for sales before 23-Jul-2024)." />
        <Stat label="Estimated tax + cess" value={current.total} format={(n) => inr(n)} icon={<Scale className="size-4" />} tone="warn"
          hint={`slab ${current.slab_rate_pct}% from your profile`} help="Before surcharge and rebates; 4 % health and education cess included." />
        <Stat label={isCurrent ? "Days to 31 March" : "Disposals"} value={isCurrent ? data.harvest.days_left : current.disposals} icon={<CalendarClock className="size-4" />}
          hint={isCurrent ? "harvesting must settle before FY end" : current.label} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
        <Card title="LTCG exemption meter" icon={<Leaf className="size-4" />}
          help="Long-term gains on listed equity and equity funds are tax-free up to ₹1.25 lakh a financial year (₹1 lakh before FY 2024-25), after setting off losses. In FY 2024-25 one ₹1.25 lakh limit covers gains before and after 23-Jul-2024.">
          <p className="num text-2xl font-semibold">{inr(current.exemption.used)} <span className="text-sm font-normal text-muted">of {inr(current.exemption.limit)} used</span></p>
          <Progress className="mt-3" value={current.exemption.used} max={current.exemption.limit} tone="gain" />
          <p className="mt-2 text-xs text-muted">{inr(current.exemption.remaining)} left in {current.label}{isCurrent ? ` · ${data.harvest.days_left} days to go` : ""}.</p>
          {(current.losses_carried.short > 0 || current.losses_carried.long > 0) && (
            <p className="mt-2 text-xs">Unabsorbed losses: short-term {inr(current.losses_carried.short)}, long-term {inr(current.losses_carried.long)} (carry forward up to 8 years if you file on time).</p>
          )}
          {current.intraday > 0 && <p className="mt-2 text-xs text-warn">{current.intraday} intraday match(es) excluded: same-day trades are business income.</p>}
          {current.notes.map((n) => <p key={n} className="mt-1 text-xs text-muted">{n}</p>)}
        </Card>
        <Card title="How the tax is computed" subtitle={`${current.label} · gains net within their category, then losses are set off (long-term losses only against long-term gains), then the exemption`}>
          {current.slices.length === 0 ? <p className="text-sm text-muted">No taxable gains in {current.label}.</p> : (
            <Table label="How the tax is computed">
              <thead><tr><th>Category</th><th className="text-right">Rate</th><th className="text-right">Gain</th><th className="text-right">Set off</th><th className="text-right">Exempt</th><th className="text-right">Taxable</th></tr></thead>
              <tbody>
                {current.slices.map((s, i) => (
                  <tr key={i}>
                    <td>{BUCKET[s.bucket] ?? s.bucket}</td>
                    <td className="num text-right">{s.slab ? `slab ${current.slab_rate_pct}%` : `${s.rate_pct}%`}</td>
                    <td className="num text-right">{inr(s.gain)}</td>
                    <td className="num text-right text-muted">{s.set_off ? `−${inr(s.set_off)}` : "—"}</td>
                    <td className="num text-right text-muted">{s.exempted ? `−${inr(s.exempted)}` : "—"}</td>
                    <td className="num text-right font-medium">{inr(s.taxable)}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
          <p className="mt-3 text-xs text-muted">Tax {inr(current.tax)} + cess {inr(current.cess)} = <span className="num font-medium text-foreground">{inr(current.total)}</span></p>
        </Card>
      </div>

      {isCurrent && (
        <>
          <div className="space-y-4">
            <Ideas title="Gain harvesting" icon={<Leaf className="size-4" />} items={data.harvest.gain_harvest} kind="gain"
              help="Sell long-term equity lots whose gain fits in this year's unused exemption (tax-free), then buy back on a later day at the higher price: your cost rises, so future LTCG is lower." />
            <Ideas title="Tax-loss harvesting" icon={<Scissors className="size-4" />} items={data.harvest.loss_harvest} kind="loss"
              help="Sell holdings priced below cost to set the loss off against gains already booked this year. The saving is the change in this year's tax computed by the same rules." />
          </div>
          <Card title="Before you act">
            <ul className="space-y-1.5 text-xs">
              {data.harvest.notes.map((n) => <li key={n} className="flex gap-1.5"><TrendingDown className="mt-0.5 size-3.5 shrink-0 text-muted" />{n}</li>)}
            </ul>
          </Card>
        </>
      )}

      <Card title={`Disposals in ${current.label}`} subtitle="Each sale matched first-in-first-out against a purchase lot, with the rule that applies.">
        {rows.length === 0 ? <EmptyState title="No sales in this year">Sales appear here once imported.</EmptyState> : (
          <Table label={`Disposals in ${current.label}`}>
            <thead><tr><th>Holding</th><th>Bought</th><th>Sold</th><th className="text-right">Units</th><th className="text-right">Cost</th><th className="text-right">Sale</th><th className="text-right">Gain</th><th>Term · rate</th></tr></thead>
            <tbody>
              {rows.map((d, i) => (
                <tr key={i}>
                  <td className="max-w-[14rem]"><span className="block truncate">{d.name}</span><span className="text-[11px] text-muted">{TAX_CLASS_LABEL[d.tax_class]}</span></td>
                  <td className="num text-xs">{d.acquired ? day(d.acquired) : "unknown"}</td>
                  <td className="num text-xs">{day(d.sold)}</td>
                  <td className="num text-right">{units(d.quantity)}</td>
                  <td className="num text-right">{inr(d.tax_cost ?? d.cost)}{d.tax_cost != null && d.cost != null && d.tax_cost !== d.cost && <span className="block text-[10px] text-muted">actual {inr(d.cost)}</span>}</td>
                  <td className="num text-right">{inr(d.proceeds)}</td>
                  <td className={cx("num text-right", (d.gain ?? 0) >= 0 ? "text-gain" : "text-loss")}>{signed(d.gain)}</td>
                  <td className="text-xs">
                    <Badge tone={d.term === "long" ? "gain" : d.term === "short" ? "info" : d.term === "exempt" ? "accent" : "warn"}>{d.term ?? "?"}</Badge>{" "}
                    <span className="num">{d.slab ? "slab" : d.rate_pct != null ? `${d.rate_pct}%` : ""}</span>
                    {d.notes.length > 0 && <InfoTip>{d.notes.join(" ")}</InfoTip>}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <AisCheck refresh={refresh} preferFy={current.fy} />

      <Card title="Rule table" subtitle="Keyed by the date of sale, not by section numbers (the Income-tax Act 2025 renumbered them from tax year 2026-27 with the same rates)."
        actions={<button type="button" className="text-xs font-medium text-brand" onClick={() => setShowRules((s) => !s)}>{showRules ? "Hide" : `Show ${data.rules.length} rules`}</button>}>
        {showRules && (
          <Table label="Capital gains rule table">
            <thead><tr><th>Rule</th><th>Sales from</th><th>to</th><th className="text-right">LT after</th><th className="text-right">STCG</th><th className="text-right">LTCG</th><th>Status</th><th>Note</th></tr></thead>
            <tbody>
              {data.rules.map((r) => (
                <tr key={r.id} className="align-top">
                  <td className="num text-xs">{r.id}</td>
                  <td className="num text-xs">{day(r.effective_from)}</td>
                  <td className="num text-xs">{r.effective_to ? day(r.effective_to) : "—"}</td>
                  <td className="num text-right text-xs">{r.long_term_months ? `${r.long_term_months} m` : "—"}</td>
                  <td className="num text-right text-xs">{r.exempt ? "exempt" : r.stcg_rate_pct != null ? `${r.stcg_rate_pct}%` : "slab"}</td>
                  <td className="num text-right text-xs">{r.exempt ? "exempt" : r.ltcg_rate_pct != null ? `${r.ltcg_rate_pct}%` : "—"}</td>
                  <td><Badge tone={r.status === "verified" ? "gain" : r.status === "secondary" ? "info" : "warn"}>{r.status}</Badge></td>
                  <td className="max-w-md whitespace-normal text-xs text-muted">{r.note} <a className="text-brand" href={r.source} target="_blank" rel="noreferrer">source</a></td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <ul className="mt-2 space-y-1 text-xs text-muted">{data.caveats.map((c) => <li key={c}>• {c}</li>)}</ul>
      </Card>
    </div>
  );
}
