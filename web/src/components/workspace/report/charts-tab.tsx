"use client";

// Charts tab: the report's numbers as interactive charts. Ledger figures (what the report relied on) are marked
// "from the report"; market data fetched now (price, NAV, bond cash flows, later subscription snapshots) is marked
// "live" and never mixed into a ledger series. Clicking a bar, point or slice shows the claims behind it.

import { Activity, BarChart3, CalendarClock, Coins, Gauge as GaugeIcon, LineChart as LineIcon, PieChart, Scale, Target, Users } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";

import { BarsChart, CHART_COLORS, DonutChart, TimeSeriesChart } from "@/components/charts";
import { CashFlowChart, PriceYieldChart } from "@/components/markets/charts";
import { Metric, Timeline, inr, pctOf } from "@/components/markets/common";
import type { BondAnalytics, FundAnalytics, StockHistory } from "@/components/markets/types";
import { Badge, Card, EmptyState, ErrorNote, InfoTip, Segmented, Skeleton, cx } from "@/components/ui";
import { TriangulationCard } from "@/components/workspace/report/accuracy";
import { type ClaimMap, CiteText, type OpenClaim, SourceStrip, StatusDot, STATUS_LABEL } from "@/components/workspace/report/shared";
import { useApi } from "@/lib/api";
import type { FinSeries, Insights, Pt } from "@/lib/insights";

type Fmt = (v: number) => string;

/** Axis / tooltip formatter for a normalised unit. */
export function fmtUnit(unit: string): Fmt {
  const n = (v: number, d = 2) => v.toLocaleString("en-IN", { maximumFractionDigits: d });
  if (unit === "%") return (v) => `${n(v)}%`;
  if (unit === "x") return (v) => `${n(v, Math.abs(v) < 1 ? 3 : 2)}x`;
  if (unit === "₹" || unit.startsWith("₹/")) return (v) => `₹${n(v)}`;
  if (unit.startsWith("₹ ")) return (v) => `₹${n(v, Math.abs(v) >= 1000 ? 0 : 2)} ${unit.slice(2)}`;
  if (unit.startsWith("US$")) return (v) => `US$${n(v)}${unit.slice(3)}`;
  return (v) => `${n(v)} ${unit}`;
}

/** Short axis ticks: the unit is in the card subtitle, so ₹12,000 Mn becomes 12k and ₹1,78,650 Cr 178.7k. */
export const tick: Fmt = (v) => {
  const a = Math.abs(v);
  if (a >= 1e3) return `${(v / 1e3).toLocaleString("en-IN", { maximumFractionDigits: 1 })}k`;
  return v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
};

const Src = ({ live }: { live?: boolean }) =>
  live ? <Badge tone="accent" dot>live market data</Badge> : <Badge tone="neutral">from the report&apos;s ledger</Badge>;

/** A chart card with its provenance and the sources of whatever was clicked. */
function ChartCard({ title, subtitle, icon, help, live, children, sel, claims, onOpen, className, actions }: {
  title: ReactNode; subtitle?: ReactNode; icon?: ReactNode; help?: ReactNode; live?: boolean; children: ReactNode;
  sel?: { label: string; ids: number[] } | null; claims: ClaimMap; onOpen: OpenClaim; className?: string; actions?: ReactNode;
}) {
  return (
    <Card title={title} subtitle={subtitle} icon={icon} help={help} className={cx("animate-fade-up", className)}
      actions={<div className="flex flex-wrap items-center justify-end gap-2">{actions}<span className="hidden sm:inline-flex"><Src live={live} /></span></div>}>
      {children}
      <div className="mt-3 min-h-5 border-t border-border/60 pt-2">
        {sel && sel.ids.length ? (
          <SourceStrip ids={sel.ids} claims={claims} onOpen={onOpen} label={<span className="font-medium text-foreground">{sel.label}</span>} className="animate-fade-in" />
        ) : (
          <p className="text-[11px] text-muted">{live ? "Fetched now from the exchange / AMFI; not part of the report's evidence." : "Click a bar, point or slice to see its sources."}</p>
        )}
      </div>
    </Card>
  );
}

const useSel = () => useState<{ label: string; ids: number[] } | null>(null);

// ------------------------------------------------------------------ financial trends

function FinancialTrends({ series, claims, onOpen }: { series: FinSeries[]; claims: ClaimMap; onOpen: OpenClaim }) {
  const choices = useMemo(() => {
    const seen = new Map<string, FinSeries[]>();
    for (const s of series) {
      if (!s.points.length) continue;
      const k = `${s.key}|${s.unit}`;
      seen.set(k, [...(seen.get(k) ?? []), s]);
    }
    return [...seen.entries()].map(([k, list]) => ({ k, label: `${list[0].label}${list[0].unit.startsWith("US$") ? " (US$)" : ""}`, list, family: list[0].family }));
  }, [series]);
  const [pick, setPick] = useState(choices[0]?.k ?? "");
  const cur = choices.find((c) => c.k === pick) ?? choices[0];
  const freqs = cur?.list.map((s) => s.freq) ?? [];
  const [freq, setFreq] = useState<"annual" | "quarterly">("annual");
  const s = cur?.list.find((x) => x.freq === freq) ?? cur?.list[0];
  const [sel, setSel] = useSel();
  if (!s) return null;
  const fmt = fmtUnit(s.unit);
  const rows = s.points.map((p) => ({ period: p.period, value: p.value, id: p.claim_id, status: p.status }));
  const quick = ["revenue", "pat", "ebitda", "eps", "roe", "cfo", "borrowings", "loan_book"]
    .map((k) => choices.find((c) => c.k.startsWith(`${k}|`) && !c.k.includes("US$")))
    .filter((c): c is (typeof choices)[number] => !!c);
  return (
    <ChartCard title="Financial trends" icon={<BarChart3 className="size-4" />} sel={sel} claims={claims} onOpen={onOpen}
      subtitle={`${s.label} · ${s.freq} · ${s.unit}${s.unit.startsWith("₹ ") ? ` (as the filings report it)` : ""}`}
      help="Each bar is one ledger claim for that period. Red bars are negative (e.g. cash burned). Units are exactly as the company reports them (₹ Mn = million, ₹ Cr = crore; 1 Cr = 10 Mn).">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1">
          {quick.map((c) => (
            <button key={c.k} type="button" onClick={() => { setPick(c.k); setSel(null); }}
              className={cx("rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset transition", c.k === cur.k ? "bg-brand-soft text-brand-strong ring-brand/30" : "text-muted ring-border hover:text-foreground")}>
              {c.label.replace(/\s*\(.*\)$/, "")}
            </button>
          ))}
        </div>
        <select value={cur.k} onChange={(e) => { setPick(e.target.value); setSel(null); }} aria-label="Metric"
          className="h-8 max-w-full rounded-lg border border-border bg-card px-2 text-xs sm:ml-auto sm:max-w-64">
          {choices.map((c) => (
            <option key={c.k} value={c.k}>
              {c.label}
              {c.family ? "" : " ·"}
            </option>
          ))}
        </select>
        {new Set(freqs).size > 1 && (
          <Segmented value={s.freq} onChange={(f) => { setFreq(f); setSel(null); }} options={[{ value: "annual", label: "Years" }, { value: "quarterly", label: "Quarters" }]} />
        )}
      </div>
      <BarsChart data={rows} x="period" series={[{ key: "value", label: s.label }]} format={fmt} tickFormat={s.unit.startsWith("₹ ") || s.unit.startsWith("US$ ") ? tick : undefined} height={250}
        colorBy={(r) => (Number(r.value) < 0 ? "var(--loss)" : r.status === "verified" ? "var(--chart-1)" : "var(--chart-4)")}
        onBarClick={(r) => { setSel({ label: `${s.label}, ${r.period}`, ids: [Number(r.id)] }); onOpen(Number(r.id)); }} />
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
        {s.points.map((p) => (
          <button key={p.claim_id} type="button" onClick={() => onOpen(p.claim_id)} className="flex items-center gap-1 hover:text-foreground">
            <StatusDot status={p.status} />
            <span>{p.period}</span>
            <span className="num text-foreground">{p.display}</span>
          </button>
        ))}
        {s.points.some((p) => p.status !== "verified") && <span className="ml-auto">amber = needs review (UNVERIFIED)</span>}
      </div>
    </ChartCard>
  );
}

/** Margins and returns (all in %) on one line chart, by fiscal year. */
function MarginsReturns({ series, claims, onOpen }: { series: FinSeries[]; claims: ClaimMap; onOpen: OpenClaim }) {
  const pct = series.filter((s) => s.unit === "%" && s.freq === "annual" && s.family && (s.group === "margin" || s.group === "returns") && s.points.length);
  const [sel, setSel] = useSel();
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  if (pct.length === 0 || pct.every((s) => s.points.length < 2)) return null;
  const periods = [...new Set(pct.flatMap((s) => s.points.map((p) => p.period)))].sort((a, b) => Number(a.slice(2)) - Number(b.slice(2)));
  const rows = periods.map((per) => {
    const r: Record<string, unknown> = { period: per };
    for (const s of pct) {
      const p = s.points.find((x) => x.period === per);
      if (p) {
        r[s.key] = p.value;
        r[`${s.key}_id`] = p.claim_id;
      }
    }
    return r;
  });
  const shown = pct.filter((s) => !hidden.has(s.key));
  return (
    <ChartCard title="Margins and returns" icon={<LineIcon className="size-4" />} sel={sel} claims={claims} onOpen={onOpen}
      subtitle="Profitability over the years, in %" help="Margins show how much of each ₹100 of revenue is kept as profit; ROE / ROCE show the return on the money in the business. Falling lines while revenue grows = growth is getting less profitable.">
      <div className="mb-2 flex flex-wrap gap-1">
        {pct.map((s, i) => (
          <button key={s.key} type="button" onClick={() => setHidden((h) => { const n = new Set(h); if (n.has(s.key)) n.delete(s.key); else n.add(s.key); return n; })}
            className={cx("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs ring-1 ring-inset transition", hidden.has(s.key) ? "text-muted ring-border opacity-60" : "ring-border-strong")}>
            <span className="size-2 rounded-full" style={{ background: CHART_COLORS[i % 6] }} />
            {s.label}
          </button>
        ))}
      </div>
      <TimeSeriesChart data={rows} x="period" xFormat={(v) => String(v)} area={false} dots curve="linear" showChange={false} height={240}
        series={shown.map((s) => ({ key: s.key, label: s.label, color: CHART_COLORS[pct.indexOf(s) % 6] }))} format={(v) => `${v.toFixed(1)}%`}
        onPointClick={(r) => setSel({ label: String(r.period), ids: shown.map((s) => r[`${s.key}_id`]).filter((x): x is number => typeof x === "number") })} />
    </ChartCard>
  );
}

// ------------------------------------------------------------------ valuation

function PeerChart({ ins, claims, onOpen }: { ins: Insights; claims: ClaimMap; onOpen: OpenClaim }) {
  const sets = ins.valuation.peers;
  const [key, setKey] = useState(sets[0]?.key ?? "");
  const [hideOutliers, setHide] = useState(true);
  const [sel, setSel] = useSel();
  const set = sets.find((s) => s.key === key) ?? sets[0];
  if (!set) return null;
  const all = [...set.rows, ...(set.subject ? [set.subject] : [])];
  const vals = all.map((r) => r.value).sort((a, b) => a - b);
  const median = vals[Math.floor(vals.length / 2)] ?? 0;
  const outliers = all.filter((r) => median > 0 && r.value > median * 3);
  const rows = all
    .filter((r) => !(hideOutliers && outliers.includes(r)))
    .sort((a, b) => b.value - a.value)
    .map((r) => ({ name: r.name, value: r.value, id: r.claim_id, own: r === set.subject }));
  const subjLabel = ins.kind === "bond_report" ? "▶ This bond" : "▶ This company";
  // the comparator (roadmap §B /runs/9/report#charts 1): the median of the peers, not counting the subject
  const peerVals = all.filter((r) => r !== set.subject).map((r) => r.value).sort((a, b) => a - b);
  const m = peerVals.length;
  const peerMedian = m ? (m % 2 ? peerVals[(m - 1) / 2] : (peerVals[m / 2 - 1] + peerVals[m / 2]) / 2) : null;
  return (
    <ChartCard title="Valuation vs peers" icon={<Scale className="size-4" />} sel={sel} claims={claims} onOpen={onOpen}
      subtitle={`${set.label}${set.subject ? `: ${subjLabel.slice(2).toLowerCase()} highlighted` : ""}${peerMedian != null ? `; dashed line = peer median ${fmtUnit(set.unit)(peerMedian)}` : ""}. Lower = cheaper for P/E and P/B.`}
      help="Peers as named in the report's sources. A higher multiple than peers means you pay more for each rupee of profit or book value; that needs faster growth or better quality to be worth it."
      actions={sets.length > 1 ? <Segmented value={set.key} onChange={(k) => { setKey(k); setSel(null); }} options={sets.map((s) => ({ value: s.key, label: s.label }))} /> : undefined}>
      <BarsChart data={rows.map((r) => ({ ...r, name: r.own ? subjLabel : r.name }))} x="name" layout="vertical" labelWidth={128}
        series={[{ key: "value", label: set.label }]} format={fmtUnit(set.unit)} height={Math.max(160, rows.length * 32 + 30)}
        reference={peerMedian != null ? { value: peerMedian, label: "" } : undefined}
        colorBy={(r) => (r.own ? "var(--brand)" : "var(--chart-2)")}
        onBarClick={(r) => { setSel({ label: String(r.name), ids: [Number(r.id)] }); onOpen(Number(r.id)); }} />
      {outliers.length > 0 && (
        <label className="mt-2 flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={hideOutliers} onChange={(e) => setHide(e.target.checked)} className="accent-[var(--brand)]" />
          Hide outliers ({outliers.map((o) => `${o.name} ${o.display}`).join(", ")}): they squash the scale
        </label>
      )}
    </ChartCard>
  );
}

type FvRow = { key: string; label: string; sub: string | null; low: number; high: number; mid: number | null; range: boolean;
  display: string; ids: number[]; unverified: boolean };

/** Estimates as floating low–high ranges (when the ledger has both ends) and single values as markers, on an axis
 *  around the data (not from zero), with the report's price as a vertical line. */
function FairValue({ ins, claims, onOpen, price }: { ins: Insights; claims: ClaimMap; onOpen: OpenClaim; price: Pt | null }) {
  const [sel, setSel] = useSel();
  const fv = ins.valuation.fair_value.filter((f) => f.unit.startsWith("₹"));
  const rows = useMemo<FvRow[]>(() => {
    const out: FvRow[] = [];
    const groups = new Map<string, typeof fv>();
    for (const f of fv) groups.set(f.group, [...(groups.get(f.group) ?? []), f]);
    for (const [g, list] of groups) {
      const lo = list.find((f) => f.role === "low"), hi = list.find((f) => f.role === "high"), mid = list.find((f) => f.role === "mid");
      if (lo && hi) {
        const ends = [lo, hi, ...(mid ? [mid] : [])];
        out.push({ key: g, label: lo.label, sub: lo.basis && hi.basis ? `${lo.basis}–${hi.basis}` : null, low: lo.value, high: hi.value, mid: mid?.value ?? null,
          range: true, display: `${lo.display}–${hi.display.slice(1)}`, ids: ends.map((e) => e.claim_id), unverified: ends.some((e) => e.status !== "verified") });
        for (const f of list) if (!ends.includes(f)) out.push(point(f));
      } else for (const f of list) out.push(point(f));
    }
    return out;
    function point(f: (typeof fv)[number]): FvRow {
      return { key: `${f.group}-${f.claim_id}`, label: f.label, sub: f.role ? `${f.role} end only` : f.basis, low: f.value, high: f.value, mid: null,
        range: false, display: f.display, ids: [f.claim_id], unverified: f.status !== "verified" };
    }
  }, [fv]);
  if (rows.length === 0) return null;
  const nums = rows.flatMap((r) => [r.low, r.high]).concat(price ? [price.value] : []);
  const min = Math.min(...nums), max = Math.max(...nums), pad = (max - min || max * 0.1 || 1) * 0.1;
  const lo = Math.max(0, min - pad), hi = max + pad, span = hi - lo;
  const pos = (v: number) => `${((v - lo) / span) * 100}%`;
  const step = (() => { const raw = span / 4, mag = 10 ** Math.floor(Math.log10(raw)); return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((m) => m >= raw) ?? raw; })();
  const ticks: number[] = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) ticks.push(t);
  const tone = (v: number) => (!price ? "bg-brand" : v < price.value ? "bg-loss" : "bg-gain");
  return (
    <ChartCard title="Fair value vs price" icon={<Target className="size-4" />} sel={sel} claims={claims} onOpen={onOpen}
      subtitle={price ? `Bars are ranges, dots single estimates; the line is the price the report used, ${price.display}` : "Value estimates in the report"}
      help="Fair-value and entry-zone estimates from the report's valuation work (fincalc). A range is drawn only when the ledger holds both ends; a single number is a dot. Red = below the price, green = above. * = needs review (UNVERIFIED).">
      <div className="relative">
        <div className="space-y-2.5">
          {rows.map((r) => (
            <button key={r.key} type="button" onClick={() => { setSel({ label: r.label, ids: r.ids }); if (r.ids.length === 1) onOpen(r.ids[0]); }}
              className="grid w-full grid-cols-[8.5rem_1fr] items-center gap-3 rounded-md text-left transition hover:bg-card-hover sm:grid-cols-[11.5rem_1fr]">
              <span className="min-w-0">
                <span className="block truncate text-xs font-medium">{r.label}{r.unverified ? " *" : ""}</span>
                <span className="block text-[10px] leading-tight text-muted">
                  <span className="num text-foreground/80">{r.display}</span> · {r.range ? "range" : "point"}
                  {r.sub && <span className="block">{r.sub}</span>}
                </span>
              </span>
              <span className="relative h-6">
                <span className="absolute inset-x-0 top-1/2 h-px bg-border" />
                {price && <span className="absolute inset-y-[-4px] w-px border-l border-dashed border-warn" style={{ left: pos(price.value) }} />}
                {r.range ? (
                  <span className={cx("absolute top-1 h-4 rounded", tone((r.low + r.high) / 2), r.unverified && "opacity-60")} style={{ left: pos(r.low), width: `max(4px, calc(${pos(r.high)} - ${pos(r.low)}))` }}>
                    {r.mid != null && <span className="absolute inset-y-0 w-0.5 bg-card" style={{ left: `${((r.mid - r.low) / (r.high - r.low || 1)) * 100}%` }} />}
                  </span>
                ) : (
                  <span className={cx("absolute top-1/2 size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-card", tone(r.low), r.unverified && "opacity-60")} style={{ left: pos(r.low) }} />
                )}
              </span>
            </button>
          ))}
        </div>
        <div className="mt-1 grid grid-cols-[8.5rem_1fr] gap-3 sm:grid-cols-[11.5rem_1fr]">
          <span className="text-[10px] text-muted">₹ per share</span>
          <span className="relative h-4 text-[10px] text-muted">
            {ticks.map((t) => (
              <span key={t} className="num absolute -translate-x-1/2" style={{ left: pos(t) }}>₹{t.toLocaleString("en-IN")}</span>
            ))}
          </span>
        </div>
        {price && <p className="mt-1 text-right text-[10px] text-warn">┆ price {price.display}</p>}
      </div>
      <div className="mt-3 flex flex-wrap gap-2 border-t border-border/60 pt-3">
        {ins.valuation.multiples.slice(0, 6).map((m) => (
          <button key={`${m.key}-${m.claim_id}`} type="button" onClick={() => onOpen(m.claim_id)}
            className="rounded-lg bg-background-subtle/70 px-2.5 py-1.5 text-left ring-1 ring-inset ring-border/60 transition hover:ring-border-strong">
            <span className="block text-[10px] text-muted">{m.label}{m.context ? ` · ${m.context}` : ""}</span>
            <span className="num flex items-center gap-1 text-sm font-semibold"><StatusDot status={m.status} />{m.display}</span>
          </button>
        ))}
      </div>
    </ChartCard>
  );
}

const SCEN_TONE: Record<string, string> = { bear: "bg-loss/80", base: "bg-info/80", bull: "bg-gain/80" };

/** Bear / base / bull price ranges on one scale, with the reference price marked. */
function Scenarios({ ins, claims, onOpen, price }: { ins: Insights; claims: ClaimMap; onOpen: OpenClaim; price: Pt | null }) {
  const [open, setOpen] = useState<number | null>(null);
  const sc = ins.scenarios;
  if (!sc.length) return null;
  const nums = sc.flatMap((s) => [s.low, s.high]).filter((x): x is number => x != null);
  if (price) nums.push(price.value);
  const lo = Math.min(...nums), hi = Math.max(...nums), span = hi - lo || 1;
  const pos = (v: number) => `${((v - lo) / span) * 100}%`;
  const horizons = [...new Set(sc.map((s) => s.horizon))];
  const H: Record<string, string> = { listing: "Listing day", "12m": "12 months", "3y": "3 years", "3m": "3 months" };
  return (
    <Card title="Scenarios" icon={<Activity className="size-4" />} className="animate-fade-up"
      subtitle={`The report's bear / base / bull price ranges${price ? `; the line is ${price.display}` : ""}. Click a row for the reasoning.`}
      help="Price ranges the analyst built with fincalc from cited figures; they are judgement, not forecasts. 'Most likely' is the analyst's own label."
      actions={<span className="hidden sm:inline-flex"><Src /></span>}>
      <div className="space-y-4">
        {horizons.map((h) => (
          <div key={h}>
            <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">{H[h] ?? h}</p>
            <div className="space-y-1.5">
              {sc.map((s, i) => (s.horizon !== h ? null : (
                <div key={i}>
                  <button type="button" onClick={() => setOpen(open === i ? null : i)} className="grid w-full grid-cols-[4.5rem_1fr] items-center gap-2 text-left sm:grid-cols-[7rem_1fr_9rem]">
                    <span className="text-xs font-medium capitalize">{s.name}{s.likelihood === "most likely" && <span className="ml-1 text-brand">★</span>}</span>
                    <span className="relative h-5 rounded bg-background-subtle">
                      {price && <span className="absolute inset-y-[-3px] w-px bg-foreground/60" style={{ left: pos(price.value) }} />}
                      {s.low != null && s.high != null && (
                        <span className={cx("absolute inset-y-1 rounded", SCEN_TONE[s.name] ?? "bg-brand")} style={{ left: pos(s.low), width: `max(4px, calc(${pos(s.high)} - ${pos(s.low)}))` }} />
                      )}
                    </span>
                    <span className="num col-span-2 text-right text-xs sm:col-span-1">
                      ₹{s.low?.toLocaleString("en-IN")}–{s.high?.toLocaleString("en-IN")} <span className="text-muted">· {s.likelihood}</span>
                    </span>
                  </button>
                  {open === i && (
                    <p className="mt-1 rounded-lg bg-background-subtle/60 p-2 text-xs leading-relaxed animate-fade-in">
                      <CiteText text={s.rationale} claims={claims} onOpen={onOpen} />
                    </p>
                  )}
                </div>
              )))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ IPO

const shortTime = (iso: string) => new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" });

function IpoCharts({ ins, claims, onOpen }: { ins: Insights; claims: ClaimMap; onOpen: OpenClaim }) {
  const ipo = ins.ipo!;
  const [s1, set1] = useSel();
  const [s2, set2] = useSel();
  const [s3, set3] = useSel();
  const [s4, set4] = useSel();
  const subs = ipo.subscription.map((s) => ({ name: s.label, value: s.value, id: s.claim_id }));
  const tl = ipo.subscription_timeline;
  const issue = [ipo.issue.fresh && { name: "Fresh issue (new money)", value: ipo.issue.fresh.value, id: ipo.issue.fresh.claim_id },
    ipo.issue.ofs && { name: "Offer for sale (sellers get it)", value: ipo.issue.ofs.value, id: ipo.issue.ofs.claim_id }].filter(Boolean) as { name: string; value: number; id: number }[];
  const proceeds = ipo.proceeds.map((p) => ({ name: `${p.label}${p.cap ? " (up to)" : ""}`, value: p.value, id: p.claim_id }));
  const unit = ipo.issue.fresh?.unit ?? ipo.proceeds[0]?.unit ?? "₹ Mn";
  const h = ipo.holding;
  return (
    <>
      {subs.length > 0 && (
        <ChartCard title="Demand by investor category" icon={<Users className="size-4" />} sel={s1} claims={claims} onOpen={onOpen}
          subtitle={<>Times subscribed at {ipo.subscription_as_of} <Badge tone="warn">INTERIM</Badge></>}
          help="Shares bid for ÷ shares on offer in each category, when the report was written. Above the 1x line = oversubscribed. Weak QIB (institutional) demand is a warning sign for listing day.">
          <BarsChart data={subs} x="name" series={[{ key: "value", label: "Times subscribed" }]} format={(v) => `${v.toFixed(v < 1 ? 3 : 2)}x`} height={240}
            reference={{ value: 1, label: "1x" }} colorBy={(r) => (Number(r.value) < 1 ? "var(--loss)" : "var(--gain)")}
            onBarClick={(r) => { set1({ label: String(r.name), ids: [Number(r.id)] }); onOpen(Number(r.id)); }} />
        </ChartCard>
      )}
      {tl.length >= 2 && (
        <ChartCard live title="Subscription over the bidding window" icon={<Activity className="size-4" />} claims={claims} onOpen={onOpen}
          subtitle={`${tl.length} NSE+BSE combined snapshots saved by the monitor, to ${shortTime(tl[tl.length - 1].as_of)} IST`}
          help="Recorded by the IPO monitor from the exchange during bidding, including snapshots after the report was written. Not ledger claims.">
          <TimeSeriesChart data={tl as unknown as Record<string, unknown>[]} x="as_of" xFormat={(v) => shortTime(String(v))} area={false} dots curve="linear" showChange={false} height={260}
            series={[{ key: "total", label: "Total" }, { key: "qib", label: "QIB" }, { key: "nii", label: "NII" }, { key: "retail", label: "Retail" }]}
            format={(v) => `${v.toFixed(v < 1 ? 3 : 1)}x`} references={[{ y: 1, label: "1x", tone: "muted" }]} />
        </ChartCard>
      )}
      {issue.length === 2 && (
        <ChartCard title="Where the money goes" icon={<PieChart className="size-4" />} sel={s2} claims={claims} onOpen={onOpen}
          subtitle={`Issue size ${ipo.issue.total?.display ?? ""}: new money for the company vs existing holders selling`}
          help="Fresh issue: new shares, money goes to the company. Offer for sale (OFS): existing shareholders sell, the company gets nothing.">
          <DonutChart data={issue} height={170} format={fmtUnit(unit)} center={<div><p className="num text-sm font-semibold">{ipo.issue.total?.display}</p><p className="text-[10px] text-muted">total</p></div>}
            onSliceClick={(i) => { set2({ label: issue[i].name, ids: [issue[i].id] }); onOpen(issue[i].id); }} />
          {proceeds.length > 0 && (
            <div className="mt-4 border-t border-border/60 pt-3">
              <p className="mb-2 text-xs font-medium">Use of the fresh-issue money</p>
              <DonutChart data={proceeds} height={150} format={fmtUnit(unit)} onSliceClick={(i) => { set2({ label: proceeds[i].name, ids: [proceeds[i].id] }); onOpen(proceeds[i].id); }} />
              {ipo.proceeds.some((p) => p.cap) && <p className="mt-1 text-[11px] text-muted">&ldquo;Up to&rdquo; items are a ceiling, not a plan; issue expenses come out of the rest.</p>}
            </div>
          )}
        </ChartCard>
      )}
      {(ipo.reservation.length > 0 || h.pre) && (
        <ChartCard title="Who gets shares, who owns the company" icon={<Users className="size-4" />} sel={s3} claims={claims} onOpen={onOpen}
          help="The reservation is the share of the offer set aside for each investor category. Promoter holding falls after the IPO when promoters sell (OFS) and new shares are issued.">
          {ipo.reservation.length > 0 && (
            <DonutChart data={ipo.reservation.map((r) => ({ name: r.label, value: r.value }))} height={140} format={(v) => `${v}%`}
              onSliceClick={(i) => { set3({ label: `${ipo.reservation[i].label} reservation`, ids: [ipo.reservation[i].claim_id] }); onOpen(ipo.reservation[i].claim_id); }} />
          )}
          {h.pre && (
            <div className="mt-4 space-y-2 border-t border-border/60 pt-3">
              <p className="text-xs font-medium">Promoter holding</p>
              {[["Before the IPO", h.pre], ["After (upper band)", h.post], ["After (lower band)", h.post_floor]].map(([label, p]) =>
                p ? (
                  <button key={label as string} type="button" onClick={() => { set3({ label: label as string, ids: [(p as Pt).claim_id] }); onOpen((p as Pt).claim_id); }} className="block w-full text-left">
                    <div className="mb-0.5 flex justify-between text-xs"><span className="text-muted">{label as string}</span><span className="num font-medium">{(p as Pt).display}</span></div>
                    <div className="h-2 overflow-hidden rounded-full bg-background-subtle"><div className="h-full rounded-full bg-gradient-to-r from-brand to-accent" style={{ width: `${(p as Pt).value}%` }} /></div>
                  </button>
                ) : null,
              )}
            </div>
          )}
        </ChartCard>
      )}
      {(ipo.timeline.length > 0 || ipo.anchor.shares) && (
        <ChartCard title="Key dates and anchor book" icon={<CalendarClock className="size-4" />} sel={s4} claims={claims} onOpen={onOpen}
          help="Dates with a chip are cited claims; the others are the monitor's expected dates (T+1 allotment, T+3 listing).">
          {ipo.timeline.length > 0 && (
            <Timeline items={ipo.timeline.map((d, i) => ({
              key: `${d.title}-${i}`, date: d.date, title: d.title,
              badge: d.claim_id ? <button type="button" onClick={() => onOpen(d.claim_id!)} className="num rounded border border-gain/40 bg-gain-soft px-1 text-[10px] text-gain">C{d.claim_id}</button> : <Badge tone="neutral">monitor</Badge>,
              highlight: /listing|closes/i.test(d.title),
            }))} />
          )}
          {ipo.anchor.shares && (
            <div className="mt-4 grid grid-cols-2 gap-2 border-t border-border/60 pt-3">
              {[["Anchor shares", ipo.anchor.shares], ["Anchor amount", ipo.anchor.amount], ["To mutual funds", ipo.anchor.mf_shares], ["To insurers", ipo.anchor.insurance_shares]].map(([l, p]) =>
                p ? (
                  <button key={l as string} type="button" onClick={() => { set4({ label: l as string, ids: [(p as Pt).claim_id] }); onOpen((p as Pt).claim_id); }} className="text-left">
                    <Metric label={l as string} value={(p as Pt).display} />
                  </button>
                ) : null,
              )}
            </div>
          )}
        </ChartCard>
      )}
    </>
  );
}

// ------------------------------------------------------------------ funds

function FundCharts({ ins, claims, onOpen }: { ins: Insights; claims: ClaimMap; onOpen: OpenClaim }) {
  const f = ins.fund!;
  const [s1, set1] = useSel();
  const [s2, set2] = useSel();
  const [s3, set3] = useSel();
  const [h, setH] = useState<"r1y" | "r3y" | "r5y">("r5y");
  const keys = (["fund", "benchmark", "category", "index_fund"] as const).filter((k) => f.returns.some((r) => r[k]));
  const LAB = { fund: "This fund", benchmark: "Benchmark", category: "Category median", index_fund: "Index fund" };
  const rows = f.returns.map((r) => {
    const o: Record<string, unknown> = { horizon: r.horizon };
    for (const k of keys) if (r[k]) { o[k] = r[k]!.value; o[`${k}_id`] = r[k]!.claim_id; }
    return o;
  });
  const peers = f.peers.filter((p) => p[h]).map((p) => ({ name: p.name, value: p[h]!.value, id: p[h]!.claim_id, own: p.name === "This fund" })).sort((a, b) => b.value - a.value);
  return (
    <>
      {rows.length > 0 && (
        <ChartCard title="Returns vs benchmark and category" icon={<BarChart3 className="size-4" />} sel={s1} claims={claims} onOpen={onOpen}
          subtitle="Annualised returns, %" help="Each group is one period. An active fund should beat its benchmark (and the cheap index fund) after costs, over 3–5 years especially.">
          <BarsChart data={rows} x="horizon" series={keys.map((k, i) => ({ key: k, label: LAB[k], color: k === "fund" ? "var(--brand)" : CHART_COLORS[(i + 1) % 6] }))}
            format={(v) => `${v.toFixed(1)}%`} height={250}
            onBarClick={(r, k) => { const id = r[`${k}_id`]; if (typeof id === "number") { set1({ label: `${LAB[k as keyof typeof LAB]}, ${r.horizon}`, ids: [id] }); onOpen(id); } }} />
        </ChartCard>
      )}
      {peers.length > 1 && (
        <ChartCard title="Against named peers" icon={<Scale className="size-4" />} sel={s2} claims={claims} onOpen={onOpen}
          subtitle="Trailing annualised return of each fund in the report" actions={<Segmented value={h} onChange={(v) => { setH(v); set2(null); }} options={[{ value: "r1y", label: "1Y" }, { value: "r3y", label: "3Y" }, { value: "r5y", label: "5Y" }]} />}>
          <BarsChart data={peers} x="name" layout="vertical" labelWidth={150} series={[{ key: "value", label: "Return" }]} format={(v) => `${v.toFixed(1)}%`}
            height={peers.length * 32 + 30} colorBy={(r) => (r.own ? "var(--brand)" : "var(--chart-2)")}
            onBarClick={(r) => { set2({ label: String(r.name), ids: [Number(r.id)] }); onOpen(Number(r.id)); }} />
        </ChartCard>
      )}
      {(f.phases.length > 0 || f.rolling.median) && (
        <ChartCard title="How it behaved in different markets" icon={<Activity className="size-4" />} sel={s3} claims={claims} onOpen={onOpen}
          help="Return in each market phase the report looked at (not annualised), and the spread of 3-year returns over rolling windows.">
          {f.phases.length > 0 && (
            <BarsChart data={f.phases.map((p) => ({ name: p.label, value: p.value, id: p.claim_id }))} x="name" layout="vertical" labelWidth={150}
              series={[{ key: "value", label: "Return" }]} format={(v) => `${v.toFixed(1)}%`} height={f.phases.length * 30 + 30}
              colorBy={(r) => (Number(r.value) < 0 ? "var(--loss)" : "var(--gain)")}
              onBarClick={(r) => { set3({ label: String(r.name), ids: [Number(r.id)] }); onOpen(Number(r.id)); }} />
          )}
          {f.rolling.median && (
            <div className="mt-3 grid grid-cols-3 gap-2 border-t border-border/60 pt-3">
              {([["Worst 3Y", f.rolling.min], ["Median 3Y", f.rolling.median], ["Best 3Y", f.rolling.max]] as const).map(([l, p]) => p ? (
                <button key={l} type="button" onClick={() => onOpen(p.claim_id)} className="text-left"><Metric label={l} value={p.display} sub="a year, rolling" /></button>
              ) : null)}
            </div>
          )}
        </ChartCard>
      )}
      {(f.allocation.length > 0 || f.costs.length > 0 || f.risk.length > 0) && (
        <ChartCard title="Portfolio, costs and risk" icon={<GaugeIcon className="size-4" />} claims={claims} onOpen={onOpen}
          help="Market-cap mix of the portfolio, the yearly fee (TER) and how bumpy the ride has been.">
          {f.allocation.length > 0 && <DonutChart data={f.allocation.map((a) => ({ name: a.label, value: a.value }))} height={150} format={(v) => `${v.toFixed(1)}%`} onSliceClick={(i) => onOpen(f.allocation[i].claim_id)} />}
          <div className="mt-3 grid grid-cols-2 gap-2 border-t border-border/60 pt-3 sm:grid-cols-3">
            {[...f.costs.map((c) => ({ ...c, label: `TER: ${c.label}` })), ...f.risk].map((m) => (
              <button key={`${m.label}-${m.claim_id}`} type="button" onClick={() => onOpen(m.claim_id)} className="text-left">
                <Metric label={m.label} value={<span className="inline-flex items-center gap-1"><StatusDot status={m.status} />{m.display}</span>} />
              </button>
            ))}
          </div>
        </ChartCard>
      )}
    </>
  );
}

function LiveNav({ code, claims, onOpen }: { code: string; claims: ClaimMap; onOpen: OpenClaim }) {
  const { data, error, reload } = useApi<FundAnalytics>(`/api/funds/${code}/analytics`);
  return (
    <ChartCard live title="NAV history" icon={<LineIcon className="size-4" />} claims={claims} onOpen={onOpen}
      subtitle={data ? `${data.scheme.name}, AMFI NAV to ${data.scheme.nav_date}` : "AMFI NAV"}>
      {error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <LoadingChart what="the AMFI NAV history" /> : (
        <TimeSeriesChart data={data.navs} series={[{ key: "nav", label: "NAV" }]} ranges={["1Y", "3Y", "ALL"]} defaultRange="ALL" format={(v) => `₹${v.toFixed(2)}`} height={260} />
      )}
    </ChartCard>
  );
}

// ------------------------------------------------------------------ stocks and bonds

/** Skeleton of a chart with a note: live fetches from NSE / AMFI can take 10–20 s the first time. */
function LoadingChart({ what }: { what: string }) {
  return (
    <div className="relative h-64 overflow-hidden rounded-lg">
      <Skeleton className="absolute inset-0 rounded-lg" />
      <p className="absolute inset-0 grid place-items-center text-xs text-muted">Loading {what}… the first load can take 10–20 seconds</p>
    </div>
  );
}

function LivePrice({ symbol, ins, claims, onOpen }: { symbol: string; ins: Insights; claims: ClaimMap; onOpen: OpenClaim }) {
  const { data, error, reload } = useApi<StockHistory>(`/api/stocks/${encodeURIComponent(symbol)}/history?days=1825`);
  const ex = symbol.startsWith("BSE:") ? "BSE" : "NSE";
  const zone = ins.valuation.fair_value.filter((f) => /^entry_zone/.test(f.metric) && f.status === "verified");
  return (
    <ChartCard live title="Share price" icon={<LineIcon className="size-4" />} claims={claims} onOpen={onOpen}
      subtitle={zone.length ? `${ex} closes; dashed lines = the report's entry zone (${zone.map((z) => z.display).join(" – ")})` : `${ex} daily closes`}>
      {error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <LoadingChart what={`five years of ${ex} closes`} /> : data.bars.length < 2 ? (
        <p className="text-sm text-muted">{ex} returned no price history for {symbol}. <button type="button" onClick={reload} className="text-brand hover:underline">Retry</button></p>
      ) : (
        <TimeSeriesChart data={data.bars} series={[{ key: "close", label: "Close" }]} ranges={["3M", "6M", "1Y", "3Y", "5Y"]} defaultRange="1Y" format={(v) => inr(v, 0)} height={260}
          references={zone.map((z) => ({ y: z.value, label: `${z.label}${z.role ? ` ${z.role}` : ""}${z.basis ? ` (${z.basis})` : ""}`, tone: "gain" as const }))} />
      )}
    </ChartCard>
  );
}

function BondCharts({ ins, claims, onOpen }: { ins: Insights; claims: ClaimMap; onOpen: OpenClaim }) {
  const b = ins.bond!;
  const [s1, set1] = useSel();
  const ys = b.yields.map((y) => ({ name: y.label.replace("This bond: ", "This bond, "), value: y.value, id: y.claim_id, own: y.label.startsWith("This bond"), status: y.status }))
    .sort((a, c) => c.value - a.value);
  return (
    <>
      {ys.length > 0 && (
        <ChartCard title="Yield against the alternatives" icon={<Coins className="size-4" />} sel={s1} claims={claims} onOpen={onOpen}
          subtitle="Pre-tax, % a year. Coupon ≠ yield when the price is above or below ₹1,000."
          help="What this bond earns compared with a government bond, a bank FD and debt funds of similar length. YTM is the real return if you buy at today's price and hold to maturity; the coupon is only the interest rate on face value.">
          <BarsChart data={ys} x="name" layout="vertical" labelWidth={170} series={[{ key: "value", label: "Yield" }]} format={(v) => `${v.toFixed(2)}%`}
            height={ys.length * 32 + 30} colorBy={(r) => (r.own ? "var(--brand)" : "var(--chart-2)")}
            onBarClick={(r) => { set1({ label: String(r.name), ids: [Number(r.id)] }); onOpen(Number(r.id)); }} />
        </ChartCard>
      )}
      {b.ratings.length > 0 && (
        <Card title="Credit rating" icon={<Target className="size-4" />} help="A rating agency's opinion of how likely the issuer is to pay on time. AAA is the highest." className="animate-fade-up"
          actions={<span className="hidden sm:inline-flex"><Src /></span>}>
          <ul className="space-y-2 text-sm">
            {b.ratings.map((r) => (
              <li key={r.claim_id} className="flex gap-2">
                <StatusDot status={r.status} className="mt-2" />
                <span className="leading-relaxed"><CiteText text={`${r.text} [C${r.claim_id}]`} claims={claims} onOpen={onOpen} /></span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}

function LiveBond({ isin, claims, onOpen }: { isin: string; claims: ClaimMap; onOpen: OpenClaim }) {
  const { data, error, reload } = useApi<BondAnalytics>(`/api/bonds/${isin}/analytics`);
  const a = data?.analytics;
  return (
    <>
      <ChartCard live title="Cash flows if you buy today" icon={<Coins className="size-4" />} claims={claims} onOpen={onOpen}
        subtitle={a ? `Per bond at ₹${data!.analytics!.price} (settles ${a.settlement}); ${a.cash_flows.length} payments, ₹${a.totals.received.toLocaleString("en-IN")} in all` : "Coupons and principal"}
        help="Every rupee this bond pays from the next settlement date to maturity, computed by fincalc from today's NSE price.">
        {error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <Skeleton className="h-60 rounded-lg" /> : !a ? <p className="text-sm text-muted">{data.error ?? "No analytics for this bond."}</p> : (
          <>
            <CashFlowChart flows={a.cash_flows} />
            <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Metric label="YTM (pre-tax)" value={pctOf(a.ytm)} help="Yield to maturity at today's price." />
              <Metric label="After tax" value={pctOf(a.after_tax_ytm)} sub={`at ${data.tax_slab_pct}% slab`} />
              <Metric label="Current yield" value={pctOf(a.current_yield)} />
              <Metric label="Premium to face" value={`${a.premium_pct.toFixed(2)}%`} />
            </div>
          </>
        )}
      </ChartCard>
      {a && a.curve.length > 0 && (
        <ChartCard live title="Price vs yield" icon={<LineIcon className="size-4" />} claims={claims} onOpen={onOpen}
          subtitle={`If market yields move, the price moves the other way (modified duration ${a.modified_duration.toFixed(2)})`}>
          <PriceYieldChart curve={a.curve} ytm={a.ytm} price={a.dirty_price} />
        </ChartCard>
      )}
    </>
  );
}

// ------------------------------------------------------------------ the tab

export function ChartsTab({ ins, error, reload, claims, onOpen }: { ins: Insights | null; error: string | null; reload: () => void; claims: ClaimMap; onOpen: OpenClaim }) {
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!ins) return <div className="grid gap-5 lg:grid-cols-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-80 rounded-xl" />)}</div>;
  const series = ins.financials.series;
  const price = ins.kind === "ipo_report" ? ins.ipo?.price_band.high ?? null
    : ins.kind === "stock_report" ? (ins.key_numbers.find((t) => t.label === "Share price") ?? null) : null;
  const blocks: ReactNode[] = [];
  if (ins.kind === "ipo_report" && ins.ipo) blocks.push(<IpoCharts key="ipo" ins={ins} claims={claims} onOpen={onOpen} />);
  const stockKey = ins.subject.key ?? ins.subject.nse_symbol; // "BSE:<code>" for a BSE-only stock
  if (ins.kind === "stock_report" && stockKey) blocks.push(<LivePrice key="px" symbol={stockKey} ins={ins} claims={claims} onOpen={onOpen} />);
  if (ins.kind === "fund_report" && ins.fund) {
    blocks.push(<FundCharts key="fund" ins={ins} claims={claims} onOpen={onOpen} />);
    if (ins.subject.amfi_code) blocks.push(<LiveNav key="nav" code={ins.subject.amfi_code} claims={claims} onOpen={onOpen} />);
  }
  if (ins.kind === "bond_report" && ins.bond) {
    blocks.push(<BondCharts key="bond" ins={ins} claims={claims} onOpen={onOpen} />);
    if (ins.subject.isin) blocks.push(<LiveBond key="live" isin={ins.subject.isin} claims={claims} onOpen={onOpen} />);
  }
  if (series.length) {
    blocks.push(<FinancialTrends key="fin" series={series} claims={claims} onOpen={onOpen} />);
    blocks.push(<MarginsReturns key="mr" series={series} claims={claims} onOpen={onOpen} />);
  }
  // bonds: peer yields are already in the yield comparison
  if (ins.valuation.peers.length && ins.kind !== "bond_report") blocks.push(<PeerChart key="peers" ins={ins} claims={claims} onOpen={onOpen} />);
  if (ins.triangulation) blocks.push(<TriangulationCard key="tri" t={ins.triangulation} claims={claims} onOpen={onOpen} />);
  if (ins.valuation.fair_value.length) blocks.push(<FairValue key="fv" ins={ins} claims={claims} onOpen={onOpen} price={price} />);
  if (ins.scenarios.length) blocks.push(<Scenarios key="sc" ins={ins} claims={claims} onOpen={onOpen} price={price} />);
  if (!blocks.length)
    return (
      <EmptyState icon={<BarChart3 className="size-5" />} title="No chartable figures in this run's ledger">
        The report&apos;s claims are mostly text. The full report and the evidence tab still show every cited fact.
      </EmptyState>
    );
  return (
    <div className="space-y-4">
      <p className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Src /> figures the report relied on (click for sources) · <Src live /> fetched now, for context
        <InfoTip>Only verified or needs-review claims are charted; needs-review points are amber and marked UNVERIFIED in the report. Nothing is estimated to fill gaps: a missing year simply has no bar.</InfoTip>
      </p>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,30rem),1fr))] items-start gap-5">{blocks}</div>
      <p className="text-[11px] text-muted">
        Status key: {Object.entries(STATUS_LABEL).slice(0, 2).map(([k, v]) => <span key={k} className="mr-2 inline-flex items-center gap-1"><StatusDot status={k} />{v}</span>)}
      </p>
    </div>
  );
}
