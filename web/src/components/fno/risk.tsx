"use client";

// F&O risk context (docs/dev/RESEARCH_ROADMAP.md §D.5): SEBI's loss statistic, the cost breakdown from the dated charges
// table, risk-neutral vs real-world chance of profit, and the IV rank / percentile tile.

import { ExternalLink, Gauge, ShieldAlert, X } from "lucide-react";
import { useEffect, useState } from "react";

import type { Analysis, IvHistory, Outcome, RiskNotice } from "@/components/fno/model";
import { fmtINR } from "@/components/charts";
import { Badge, InfoTip, Stat, cx } from "@/components/ui";
import { day, useApi } from "@/lib/api";

const KEY = "finresearch:fno-risk-banner";
const pctText = (x: number | null | undefined, d = 1) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const rupees = (x: number | null | undefined) => (x == null ? "—" : `${x < 0 ? "−" : ""}${fmtINR(Math.abs(Math.round(x)))}`);
const paise = (x: number) => `₹${x.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/** SEBI's latest study, always on the page. Dismissing it collapses it to one line for this browser session. */
export function RiskBanner() {
  const { data } = useApi<RiskNotice>("/api/fno/risk-notice");
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        setCollapsed(sessionStorage.getItem(KEY) === "collapsed");
      } catch {
        /* storage blocked: stay expanded */
      }
    }, 0);
    return () => clearTimeout(t);
  }, []);
  const toggle = (v: boolean) => {
    setCollapsed(v);
    try {
      if (v) sessionStorage.setItem(KEY, "collapsed");
      else sessionStorage.removeItem(KEY);
    } catch {
      /* ignore */
    }
  };
  if (!data) return null;
  const cite = <a href={data.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 underline decoration-dotted underline-offset-2 hover:text-foreground">{data.source}, {day(data.date)}<ExternalLink className="size-3" /></a>;
  if (collapsed) {
    return (
      <button type="button" onClick={() => toggle(false)}
        className="flex w-full items-center gap-2 rounded-lg border border-warn/30 bg-warn-soft/60 px-3 py-1.5 text-left text-xs text-foreground/90 transition hover:border-warn/50">
        <ShieldAlert className="size-3.5 shrink-0 text-warn" />
        <span className="min-w-0 flex-1 truncate"><span className="font-medium">{data.headline}</span> <span className="text-muted">· SEBI, {day(data.date)}</span></span>
        <span className="shrink-0 text-muted">Show</span>
      </button>
    );
  }
  return (
    <section aria-label="F&O risk warning" className="relative rounded-xl border border-warn/30 bg-warn-soft/60 p-4 pr-10 animate-fade-in">
      <div className="flex gap-3">
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-warn/15 text-warn"><ShieldAlert className="size-4" /></span>
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-semibold">{data.headline}</p>
          <p className="text-xs leading-relaxed text-foreground/85">{data.text}</p>
          <p className="text-[11px] text-muted">Source: {cite}. This page computes payoffs and model probabilities; it has no edge-generating signal.</p>
        </div>
      </div>
      <button type="button" aria-label="Collapse the risk warning for this session" title="Collapse for this session" onClick={() => toggle(true)}
        className="absolute top-3 right-3 rounded-md p-1 text-muted transition hover:bg-background-subtle hover:text-foreground">
        <X className="size-4" />
      </button>
    </section>
  );
}

const LINE_LABEL: Record<string, { label: string; help: string }> = {
  stt: { label: "STT", help: "Securities transaction tax: on the sell side of options (on premium) and futures." },
  exchange: { label: "Exchange charges", help: "NSE transaction charges on the premium (options) or turnover (futures), both sides." },
  sebi: { label: "SEBI fee", help: "₹10 per crore of turnover, both sides." },
  stamp: { label: "Stamp duty", help: "On the buy side only." },
  brokerage: { label: "Brokerage", help: "Your flat brokerage per executed order (set it on the profile page)." },
  gst: { label: "GST", help: "18% on brokerage + exchange charges + SEBI fee." },
};

export function CostsBreakdown({ a }: { a: Analysis }) {
  const [open, setOpen] = useState(false);
  const e = a.costs.entry;
  const unconfirmed = e.rates.filter((r) => r.status === "unconfirmed");
  return (
    <div className="rounded-xl border border-border p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="flex items-center gap-1 text-xs font-medium text-muted">
          Costs
          <InfoTip>{a.costs.note}</InfoTip>
        </p>
        <p className="num text-sm font-semibold">{paise(a.costs.total_to_expiry ?? e.total)} <span className="text-[11px] font-normal text-muted">to expiry</span></p>
      </div>
      <table className="num mt-2 w-full text-xs">
        <tbody className="[&_td]:py-1">
          {Object.entries(e.lines).map(([k, v]) => (
            <tr key={k} className="border-b border-border/50">
              <td className="text-muted"><span className="inline-flex items-center gap-1">{LINE_LABEL[k]?.label ?? k}<InfoTip>{LINE_LABEL[k]?.help}</InfoTip></span></td>
              <td className="text-right">{paise(v)}</td>
            </tr>
          ))}
          <tr className="border-b border-border/50 font-medium"><td>Entry ({e.orders} orders)</td><td className="text-right">{paise(e.total)}</td></tr>
          <tr className="border-b border-border/50">
            <td className="text-muted"><span className="inline-flex items-center gap-1">Expected STT on exercise<InfoTip>Held to expiry, long options that finish in the money pay STT on their intrinsic value. This is the model&apos;s expected amount.</InfoTip></span></td>
            <td className="text-right">{a.costs.expected_exercise_stt == null ? "—" : paise(a.costs.expected_exercise_stt)}</td>
          </tr>
          <tr><td className="text-muted">If squared off early instead (same prices)</td><td className="text-right text-muted">+{paise(a.costs.square_off_at_same_prices.total)}</td></tr>
        </tbody>
      </table>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
        {unconfirmed.length > 0 && <Badge tone="warn">STT rate unconfirmed</Badge>}
        <button type="button" className="text-brand hover:underline" onClick={() => setOpen((o) => !o)}>{open ? "Hide rates" : "Rates and sources"}</button>
      </div>
      {open && (
        <ul className="mt-2 space-y-1 text-[11px] text-muted">
          {e.rates.map((r) => (
            <li key={r.key} className="flex flex-wrap items-center gap-x-1.5">
              <span className="num text-foreground">{r.rate_pct.toFixed(r.rate_pct < 0.01 ? 5 : 4).replace(/0+$/, "").replace(/\.$/, "")}%</span>
              <span>{r.basis}</span>
              <span>· from {day(r.effective_from)}</span>
              <Badge tone={r.status === "unconfirmed" ? "warn" : r.status === "secondary" ? "neutral" : "gain"}>{r.status}</Badge>
              {r.source.startsWith("http") ? <a href={r.source} target="_blank" rel="noreferrer" className="underline decoration-dotted">source</a> : <span>{r.source}</span>}
              {r.note && <span className="basis-full pl-2 text-muted/80">{r.note}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PopTile({ label, o, help, tone }: { label: string; o: Outcome; help: string; tone: "info" | "accent" }) {
  return (
    <div className="rounded-xl border border-border p-3">
      <p className="flex items-center gap-1 text-[11px] text-muted">{label}<InfoTip>{help}</InfoTip></p>
      <p className={cx("num mt-1 text-xl font-semibold", tone === "info" ? "text-info" : "text-accent")}>{pctText(o?.pop)}</p>
      <p className="num text-[11px] text-muted">
        {o ? <>σ {pctText(o.vol)} · EV {rupees(o.ev)}</> : "no realised volatility"}
      </p>
    </div>
  );
}

/** Risk-neutral vs real-world chance of profit (after costs), expected values, capital check and drawdown risk. */
export function ProbabilityPanel({ a }: { a: Analysis }) {
  const rw = a.real_world;
  const cc = a.capital_check;
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <PopTile label="Chance of profit · risk-neutral" o={a.risk_neutral} tone="info"
          help={`Model probability that the P&L after costs is positive at expiry with σ = ATM implied volatility and drift = the risk-free rate (${(a.rate * 100).toFixed(1)}%), N(d₂)-based. What option prices imply; not a forecast.`} />
        <PopTile label="Chance of profit · real-world" o={rw} tone="accent"
          help={`The same with σ = realised volatility (${rw?.window ?? "20/60-day"}; the less favourable of the two windows) and a stated drift of ${(a.drift * 100).toFixed(1)}% a year (no directional view). A model probability, not a forecast.`} />
      </div>
      <p className="text-[11px] leading-relaxed text-muted">{a.fair_price_note}</p>
      <p className={cx("flex items-start gap-1.5 rounded-lg px-2.5 py-1.5 text-xs", cc.within ? "bg-background-subtle text-foreground/90" : "bg-warn-soft text-foreground/90")}>
        <ShieldAlert className={cx("mt-0.5 size-3.5 shrink-0", cc.within ? "text-muted" : "text-warn")} />
        <span>
          {cc.message}
          {cc.capital <= 0 && <> Set it on the <a href="/profile" className="text-brand hover:underline">profile page</a>.</>}
          {a.drawdown && <> Repeating this {a.drawdown.repeats} times, the model puts the chance of a {a.drawdown.threshold_pct}% drawdown at {pctText(a.drawdown.probability, 0)}.</>}
        </span>
      </p>
      {a.notes.map((x) => <p key={x} className="text-xs text-warn">{x}</p>)}
    </div>
  );
}

/** IV rank and percentile from the recorded daily ATM IV; says how many days are recorded until there are enough. */
export function IvTile({ symbol, atmIv, rv20 }: { symbol: string; atmIv: number | null; rv20?: number | null }) {
  const { data } = useApi<IvHistory>(`/api/fno/${encodeURIComponent(symbol)}/iv`);
  const ok = data?.status === "ok";
  const ratio = atmIv != null && rv20 ? atmIv / 100 / rv20 : null;
  return (
    <Stat label="IV rank" icon={<Gauge className="size-4" />} tone="info"
      help={<>IV rank = where today&apos;s ATM implied volatility sits between its low and high over the last 252 recorded days; IV percentile = share of those days with lower IV. Recorded daily after the close from {data?.min_days ?? 60} days on. {data?.method}</>}
      display={<span className="num">{ok ? pctText(data!.rank, 0) : "—"}</span>}
      hint={!data ? "loading…" : ok ? `percentile ${pctText(data.percentile, 0)} · ${data.n} days${data.skew_25d != null ? ` · skew ${data.skew_25d.toFixed(1)}` : ""}`
        : `${data.status}${ratio != null ? ` · IV/RV20 ${ratio.toFixed(2)}×` : ""}`} />
  );
}
