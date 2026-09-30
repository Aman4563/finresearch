"use client";

// The listed-stock signal on the stock page: the signal itself (shared SignalView) with its position size and ATR
// stop, the forensic scorecard (screening flags with thresholds explained), and the "since this report" strip that
// also sits on a stock report's Summary tab. Everything here is computed in Python; the page only shows it.

import { Activity, ArrowRight, CalendarClock, FlaskConical, History, Microscope, RefreshCw, Scale, ShieldAlert } from "lucide-react";
import Link from "next/link";

import { type Signal, SignalView } from "@/components/signal";
import { Badge, Card, Callout, ErrorNote, InfoTip, SkeletonRows, cx } from "@/components/ui";
import { inr, pctOf } from "@/components/markets/common";
import { day, useApi, when } from "@/lib/api";

// ------------------------------------------------------------------ signal + sizing
type Sizing = {
  weight: number | null; binding?: string; vol_scaled?: number; profile_cap?: number; quarter_kelly?: number | null;
  cap_source?: string; risk_budget?: number; atr14?: number; atr_k?: number; stop_price?: number; stop_distance?: number;
  note?: string; reason?: string;
};

export function StockSignalCard({ symbol }: { symbol: string }) {
  const { data, error, reload } = useApi<Signal>(`/api/signals/stock/${encodeURIComponent(symbol)}`);
  const sz = (data?.sizing ?? null) as Sizing | null;
  return (
    <Card title="Signal" icon={<Activity className="size-4" />}
      subtitle="Momentum, trend, valuation, shareholding and forensic flags, scored by fixed rules"
      help="A personal, research-backed estimate: the probability of a stated event with its range, the factors behind it and how it was validated. Not investment advice."
      actions={<Link href="/stocks/backtest" className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline">Backtest <ArrowRight className="size-3.5" /></Link>}>
      {error ? <ErrorNote error={`No signal: ${error}`} onRetry={reload} /> : !data ? <SkeletonRows rows={5} /> : (
        <div className="space-y-4">
          <SignalView s={data} compact />
          {sz && sz.weight != null && (
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="rounded-lg bg-background-subtle p-3">
                <p className="flex items-center gap-1 text-[11px] text-muted">
                  <Scale className="size-3.5" /> Position size ceiling
                  <InfoTip>
                    Volatility-scaled: the weight whose yearly swing equals {pctOf(sz.risk_budget ?? null, 1)} of the portfolio
                    ({pctOf(sz.vol_scaled ?? null, 1)}), capped by your single-stock limit ({pctOf(sz.profile_cap ?? null, 0)}, {sz.cap_source})
                    {sz.quarter_kelly != null && <> and by a quarter-Kelly ceiling ({pctOf(sz.quarter_kelly, 1)}) from the bucket&apos;s backtested edge</>}.
                    Kelly is noisy, so it can only lower the size, never raise it.
                  </InfoTip>
                </p>
                <p className="num mt-1 text-xl font-semibold">{pctOf(sz.weight, 1)}</p>
                <p className="text-[11px] text-muted">of the portfolio at most · set by {sz.binding}</p>
              </div>
              {sz.stop_price != null && (
                <div className="rounded-lg bg-background-subtle p-3">
                  <p className="flex items-center gap-1 text-[11px] text-muted">
                    <ShieldAlert className="size-3.5" /> ATR stop ({sz.atr_k}× ATR 14)
                    <InfoTip>
                      Average true range: the typical daily high-low swing over 14 days ({inr(sz.atr14 ?? null)}). A stop {sz.atr_k}× ATR below the
                      price limits the loss on a position; it is risk control, not a forecast (Kaminski &amp; Lo 2014).
                    </InfoTip>
                  </p>
                  <p className="num mt-1 text-xl font-semibold">{inr(sz.stop_price)}</p>
                  <p className="text-[11px] text-muted">{pctOf(sz.stop_distance ?? null, 1)} below the price</p>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ forensic scorecard
type ForensicScore = {
  key: string; name: string; value: number | null; flag: string | null; red_flag: boolean; components: Record<string, unknown>;
  missing: string[]; proxies: string[]; reason: string | null; thresholds: string; source: string;
};
type Forensic = {
  symbol: string | null; industry: string | null; fiscal_year_end: string | null; prior_year_end: string | null; basis: string | null;
  sources: string[]; notes: string[]; red_flags: number; scores: ForensicScore[]; disclaimer: string;
};

const HELP: Record<string, string> = {
  piotroski: "Nine yes/no tests of profitability, funding and efficiency (ROA > 0, cash flow > 0, ROA up, cash flow above profit, less long-term debt, better current ratio, no new shares, better gross margin, better asset turnover). One point each; 8-9 is strong, 0-2 weak.",
  altman: "A bankruptcy-risk score built for emerging-market companies: 6.56 × working capital/assets + 3.26 × retained earnings/assets + 6.72 × EBIT/assets + 1.05 × equity/liabilities. Above 2.6 is safe, 1.1-2.6 grey, below 1.1 distress.",
  beneish: "Eight ratios that rose in companies later caught manipulating earnings (receivables vs sales, margins, asset quality, sales growth, depreciation, overheads, leverage, accruals). Above −1.78 is a red flag worth a closer look, not proof.",
  accruals: "How much of the profit did not arrive as cash: (net profit − operating cash flow) ÷ average total assets. High accruals have preceded weaker returns in US studies; above +0.10 is flagged.",
  cfo_ebitda: "Operating cash flow as a share of operating profit before depreciation (EBITDA). Below 0.6 means profits are not turning into cash; check receivables and inventory.",
};

const fmtScore = (s: ForensicScore) => {
  if (s.value == null) return "—";
  if (s.key === "piotroski") return `${s.value}/${String(s.components.out_of ?? 9)}`;
  if (s.key === "accruals") return s.value.toFixed(3);
  return s.value.toFixed(2);
};

export function ForensicCard({ symbol }: { symbol: string }) {
  const { data, error, reload } = useApi<Forensic>(`/api/stocks/${encodeURIComponent(symbol)}/forensic`);
  return (
    <Card title="Forensic scorecard" icon={<Microscope className="size-4" />}
      subtitle={data?.fiscal_year_end ? `FY ending ${day(data.fiscal_year_end)}${data.prior_year_end ? ` vs ${day(data.prior_year_end)}` : ""}, ${data.basis}, from the annual Integrated Filing XBRL` : "Quality and red-flag scores from the filed annual results"}
      help="Screening flags computed from the company's own filed results: they point at what to check, they are not buy or sell signals. None of them has been validated on Indian data."
      actions={data && data.red_flags > 0 ? <Badge tone="loss">{data.red_flags} red flag{data.red_flags > 1 ? "s" : ""}</Badge> : data ? <Badge tone="gain">no red flags</Badge> : undefined}>
      {error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <SkeletonRows rows={5} /> : (
        <div className="space-y-3">
          <ul className="divide-y divide-border/70">
            {data.scores.map((s) => (
              <li key={s.key} className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3 gap-y-1 py-2.5">
                <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium">
                  {s.name}
                  <InfoTip>{HELP[s.key]} {s.thresholds} Source: {s.source}.{s.proxies.length > 0 && <> Mapping used: {s.proxies.join("; ")}.</>}</InfoTip>
                </span>
                <span className="flex items-center gap-2">
                  <span className={cx("num text-sm font-semibold", s.red_flag ? "text-loss" : s.value == null ? "text-muted" : "")}>{fmtScore(s)}</span>
                  {s.flag && <Badge tone={s.red_flag ? "loss" : s.flag === "grey" || s.flag === "middling" ? "warn" : "gain"}>{s.flag}</Badge>}
                </span>
                {s.value == null && (
                  <p className="col-span-2 text-[11px] text-muted">
                    {s.reason}{s.missing.length > 0 && <> Missing: <span className="num">{s.missing.join(", ")}</span>.</>}
                  </p>
                )}
              </li>
            ))}
          </ul>
          {data.notes.map((n) => <p key={n} className="text-[11px] text-warn">{n}</p>)}
          <p className="text-[11px] text-muted">
            {data.disclaimer} Balance sheets and cash flows are only in results filed as Integrated Filing (from the March 2025 quarter), so at most two
            fiscal years are compared.{" "}
            {data.sources.map((u, i) => <a key={u} href={u} target="_blank" rel="noreferrer" className="text-brand hover:underline">{i ? " · " : ""}XBRL {i ? "prior year" : "latest year"}</a>)}
          </p>
        </div>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ since this report
type Band = { group: string; label: string; low: number; high: number; position: "below" | "inside" | "above" | null; distance: number | null; claim_ids: number[]; verified: boolean };
type Since = {
  run_id: number | null; kind?: string; symbol: string | null; report_at?: string | null; days_since?: number | null;
  price?: number | null; price_as_of?: string | null; bands?: Band[];
  new_filings?: { count: number; results: number; items: { at: string; category: string; text: string; attachment: string | null; results_period_end: string | null }[] };
  stale?: boolean; suggest_rerun?: boolean; reasons?: string[]; errors?: string[];
};

const POS: Record<string, { text: string; tone: "gain" | "loss" | "warn" | "neutral" }> = {
  below: { text: "below", tone: "gain" }, inside: { text: "inside", tone: "neutral" }, above: { text: "above", tone: "warn" },
};

function BandRow({ b, price }: { b: Band; price: number | null | undefined }) {
  const span = b.high - b.low || Math.max(1, b.high * 0.02);
  const lo = b.low - span * 0.6, hi = b.high + span * 0.6;
  const at = (v: number) => `${Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100))}%`;
  const p = b.position ? POS[b.position] : null;
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium">{b.label}</span>
        <span className="num text-muted">{inr(b.low, 0)}{b.high !== b.low ? `–${inr(b.high, 0)}` : ""}</span>
        {p && <Badge tone={p.tone}>price {p.text}{b.distance ? ` by ${pctOf(Math.abs(b.distance), 1)}` : ""}</Badge>}
        {!b.verified && <Badge tone="neutral">needs review</Badge>}
      </div>
      <div className="relative mt-1.5 h-2 rounded-full bg-background-subtle">
        <span className="absolute inset-y-0 rounded-full bg-brand/30 ring-1 ring-brand/40" style={{ left: at(b.low), width: `calc(${at(b.high)} - ${at(b.low)})` }} />
        {price != null && <span className="absolute -top-1 h-4 w-0.5 rounded bg-foreground" style={{ left: at(price) }} title={`price ${inr(price)}`} />}
      </div>
    </div>
  );
}

/** "Since this report": pass `runId` (report Summary) or `symbol` (stock page: the latest finished stock report). */
export function SinceReport({ runId, symbol }: { runId?: number; symbol?: string }) {
  const path = runId != null ? `/api/runs/${runId}/since` : symbol ? `/api/stocks/${encodeURIComponent(symbol)}/since-report` : null;
  const { data, error } = useApi<Since>(path);
  if (error || !data || data.run_id == null) return null; // no report yet: the page says nothing rather than guessing
  const f = data.new_filings;
  return (
    <section className={cx("rounded-xl border bg-card p-4 shadow-card animate-fade-up", data.suggest_rerun ? "border-warn/40" : "border-border")}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <p className="flex items-center gap-1.5 text-sm font-semibold">
          <History className="size-4 text-brand" /> Since this report
          <InfoTip>How much has changed since the research run finished: days elapsed, the live price against the report&apos;s own entry zone and fair-value range, and filings made with NSE since then.</InfoTip>
        </p>
        <span className="inline-flex items-center gap-1 text-xs text-muted">
          <CalendarClock className="size-3.5" />
          <span className="num text-foreground">{data.days_since ?? "—"}</span> {data.days_since === 1 ? "day" : "days"}
          {data.report_at && <> (report {day(data.report_at)})</>}
        </span>
        {data.price != null && <span className="text-xs text-muted">Price <span className="num text-foreground">{inr(data.price)}</span>{data.price_as_of && <> · {when(data.price_as_of)}</>}</span>}
        {f && <span className="text-xs text-muted"><span className="num text-foreground">{f.count}</span> new filing{f.count === 1 ? "" : "s"}{f.results ? <>, <span className="text-accent">{f.results} with results</span></> : ""}</span>}
        {symbol && data.run_id && <Link href={`/runs/${data.run_id}/report`} className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline">Open report #{data.run_id} <ArrowRight className="size-3.5" /></Link>}
      </div>
      {data.bands && data.bands.length > 0 && (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">{data.bands.map((b) => <BandRow key={b.group} b={b} price={data.price} />)}</div>
      )}
      {data.suggest_rerun ? (
        <div className="mt-3">
          <Callout tone="warn" icon={<RefreshCw className="size-4" />} title="Worth a re-run">
            {(data.reasons ?? []).join(" ")} A fresh run re-reads the new filings (it uses your Claude plan window).
          </Callout>
        </div>
      ) : (data.reasons ?? []).length > 0 ? (
        <p className="mt-2 text-xs text-warn">{(data.reasons ?? []).join(" ")}</p>
      ) : null}
      {f && f.items.length > 0 && (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-muted hover:text-foreground">Filings since the report</summary>
          <ul className="mt-2 space-y-1">
            {f.items.map((a, i) => (
              <li key={`${a.at}-${i}`} className="flex gap-2">
                <span className="num shrink-0 text-muted">{day(a.at)}</span>
                <span className="min-w-0 truncate">{a.results_period_end ? <Badge tone="accent">results</Badge> : null} {a.text || a.category}</span>
                {a.attachment && <a href={a.attachment} target="_blank" rel="noreferrer" className="ml-auto shrink-0 text-brand hover:underline">PDF</a>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export const BacktestLink = () => (
  <Link href="/stocks/backtest" className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline">
    <FlaskConical className="size-3.5" /> How the signal was backtested
  </Link>
);
