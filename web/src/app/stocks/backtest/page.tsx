"use client";

// The stock signal's backtest: the pre-registered momentum + trend rule, walk-forward on the NIFTY 50 universe with
// costs, and the probability buckets the signal quotes. Read from the committed artefact via /api/backtests/stock.

import { ArrowLeft, FlaskConical, Percent, ShieldAlert, Target, TrendingDown, TrendingUp } from "lucide-react";
import Link from "next/link";

import { TimeSeriesChart } from "@/components/charts";
import { pctOf, signedPct } from "@/components/markets/common";
import { Badge, Callout, Card, EmptyState, ErrorNote, InfoTip, PageHeader, Skeleton, Stat, Table } from "@/components/ui";
import { day, useApi } from "@/lib/api";

type Bucket = { n: number; hits: number; p: number | null; n_effective: number; wilson95: [number, number] | null;
  mean_excess_12m: number | null; median_excess_12m: number | null };
type Backtest = {
  rule: string; generated: string;
  universe: { name: string; constituents_file: string | null; symbols: number;
    coverage: Record<string, { first?: string; last?: string; days: number; splits_bonuses_applied?: [string, number][]; unexplained_jumps?: string[] }> };
  data: Record<string, string>;
  costs: { buy: number; sell: number; components: Record<string, number> };
  caveats: string[];
  stats: {
    months: number; from: string; to: string;
    cagr: { strategy: number; equal_weight_universe: number; nifty50_price: number; strategy_gross_of_costs: number };
    excess_cagr_vs_equal_weight: number; excess_cagr_vs_nifty50: number;
    volatility: Record<string, number>; max_drawdown: Record<string, number>;
    monthly_hit_rate_vs_equal_weight: { hits: number; n: number; rate: number; wilson95: [number, number] | null };
    annual_turnover: number; annual_cost_drag: number; newey_west_t_excess_vs_equal_weight: number | null; average_names_held: number;
  };
  buckets: Record<string, Bucket>;
  by_period: Record<string, Record<string, Bucket>>;
  equity_curve: { date: string; strategy: number; equal_weight: number; nifty50: number }[];
};

const ci = (w: [number, number] | null | undefined) => (w ? `${pctOf(w[0], 0)}–${pctOf(w[1], 0)}` : "—");

export default function StockBacktest() {
  const { data, error, reload } = useApi<Backtest>("/api/backtests/stock");
  const s = data?.stats;
  const buckets = data ? Object.entries(data.buckets).filter(([k]) => k !== "all") : [];
  const periods = data ? Object.entries(data.by_period) : [];
  const coverage = data ? Object.entries(data.universe.coverage) : [];
  return (
    <div>
      <Link href="/stocks" className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-muted transition hover:text-brand">
        <ArrowLeft className="size-3.5" /> All stocks
      </Link>
      <PageHeader icon={<FlaskConical className="size-5" />} eyebrow="Stock signal · validation"
        title="Momentum + trend backtest"
        description="The part of the stock signal that was tested on past data: hold the strongest risk-adjusted 12-1 month momentum among NIFTY 50 stocks above their 200-day average, rebalanced monthly, after costs." />
      <div className="space-y-5">
        {error ? (
          error.includes("404") || error.toLowerCase().includes("no stock backtest") ? (
            <EmptyState icon={<FlaskConical className="size-5" />} title="No backtest yet">Run <code>uv run python -m finresearch.evals.stock_harvest</code> then <code>python -m finresearch.evals.stock_backtest</code>.</EmptyState>
          ) : <ErrorNote error={error} onRetry={reload} />
        ) : !data || !s ? (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[104px] rounded-xl" />)}</div>
        ) : (
          <>
            <Callout tone="warn" icon={<ShieldAlert className="size-4" />} title="Read with care">
              {data.caveats.join(" ")}
            </Callout>
            <div className="grid [&>*]:min-w-0 grid-cols-2 gap-3 lg:grid-cols-4 stagger">
              <Stat label="Strategy CAGR" icon={<TrendingUp className="size-4" />} display={<span className="num">{pctOf(s.cagr.strategy, 1)}</span>}
                hint={`${pctOf(s.cagr.strategy_gross_of_costs, 1)} before costs`}
                help="Compound annual growth after costs, price returns only (no dividends), from the first month the rule could be computed." />
              <Stat label="Excess vs universe" tone="accent" icon={<Target className="size-4" />}
                display={<span className="num">{signedPct(s.excess_cagr_vs_equal_weight, 1)}</span>}
                hint={`universe ${pctOf(s.cagr.equal_weight_universe, 1)} · NIFTY 50 ${pctOf(s.cagr.nifty50_price, 1)}`}
                help="Strategy CAGR minus the CAGR of the same stocks held equally weighted (also rebalanced monthly after costs). The fairer comparison, because both share the survivorship bias." />
              <Stat label="Months beating the universe" tone="info" icon={<Percent className="size-4" />}
                display={<span className="num">{pctOf(s.monthly_hit_rate_vs_equal_weight.rate, 0)}</span>}
                hint={`95% CI ${ci(s.monthly_hit_rate_vs_equal_weight.wilson95)} · n=${s.monthly_hit_rate_vs_equal_weight.n}`}
                help="Share of months the strategy's return beat the equal-weight universe's, with a Wilson 95% interval." />
              <Stat label="Max drawdown" tone="loss" icon={<TrendingDown className="size-4" />}
                display={<span className="num text-loss">{pctOf(s.max_drawdown.strategy, 1)}</span>}
                hint={`universe ${pctOf(s.max_drawdown.equal_weight_universe, 1)} · NIFTY 50 ${pctOf(s.max_drawdown.nifty50_price, 1)}`}
                help="The largest fall from a peak to a later low of the monthly growth curve." />
            </div>

            <Card title="Growth of ₹1" subtitle={`${day(s.from)} to ${day(s.to)}, ${s.months} monthly rebalances, after costs`} icon={<TrendingUp className="size-4" />}
              help="Each line compounds the monthly returns. The NIFTY 50 line is the price index (NSE has no total-return history on its API), so dividends are left out on every line.">
              <TimeSeriesChart height={300} area={false} showChange={false} data={data.equity_curve}
                series={[{ key: "strategy", label: "Strategy" }, { key: "equal_weight", label: "Universe, equal weight", color: "var(--chart-3)" },
                  { key: "nifty50", label: "NIFTY 50 (price)", color: "var(--chart-5)", dashed: true }]}
                format={(v) => `₹${v.toFixed(2)}`} />
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                <p className="text-muted">Volatility <span className="num text-foreground">{pctOf(s.volatility.strategy, 1)}</span> vs <span className="num">{pctOf(s.volatility.equal_weight_universe, 1)}</span></p>
                <p className="text-muted">Turnover <span className="num text-foreground">{(s.annual_turnover * 100).toFixed(0)}%</span>/yr</p>
                <p className="text-muted">Cost drag <span className="num text-foreground">{pctOf(s.annual_cost_drag, 2)}</span>/yr</p>
                <p className="flex items-center gap-1 text-muted">t-stat <span className="num text-foreground">{s.newey_west_t_excess_vs_equal_weight?.toFixed(2) ?? "—"}</span>
                  <InfoTip>Newey-West t-statistic of the monthly excess return over the equal-weight universe (6 lags). Around 2 or more is conventionally &ldquo;significant&rdquo;; overlapping tests and the survivorship bias make it optimistic.</InfoTip></p>
              </div>
            </Card>

            <Card title="Probability buckets the signal quotes" icon={<Target className="size-4" />}
              subtitle="Share of stock-months whose next 12 months' price return beat the NIFTY 50 index"
              help="The stock signal's probability is the hit rate of the bucket the stock is in today. Monthly 12-month windows overlap, so the 95% Wilson interval uses an effective sample of n ÷ 12 non-overlapping years; stocks in the same month also move together, so even that is optimistic.">
              <Table>
                <thead>
                  <tr><th>Bucket</th><th className="text-right!">Stock-months</th><th className="text-right!">Beat NIFTY 50</th><th className="text-right!">95% CI (n/12)</th><th className="text-right!">Median excess</th></tr>
                </thead>
                <tbody>
                  {[["all", data.buckets.all] as [string, Bucket], ...buckets].map(([k, b]) => (
                    <tr key={k}>
                      <td className="text-xs font-medium">{k === "all" ? "All stock-months (base rate)" : k}</td>
                      <td className="num text-right text-xs">{b.n.toLocaleString("en-IN")}</td>
                      <td className="num text-right text-xs">{pctOf(b.p, 1)}</td>
                      <td className="num text-right text-xs">{ci(b.wilson95)}</td>
                      <td className="num text-right text-xs">{signedPct(b.median_excess_12m, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
              {periods.length > 0 && (
                <details className="mt-3 text-xs">
                  <summary className="cursor-pointer text-muted hover:text-foreground">Stability: first half vs second half of the sample</summary>
                  <Table className="mt-2">
                    <thead><tr><th>Bucket</th>{periods.map(([p]) => <th key={p} className="text-right!">{p}</th>)}</tr></thead>
                    <tbody>
                      {buckets.map(([k]) => (
                        <tr key={k}>
                          <td className="text-xs">{k}</td>
                          {periods.map(([p, rows]) => <td key={p} className="num text-right text-xs">{pctOf(rows[k]?.p ?? null, 0)} <span className="text-muted">(n={rows[k]?.n ?? 0})</span></td>)}
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                </details>
              )}
            </Card>

            <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
              <Card title="Rule and costs" icon={<FlaskConical className="size-4" />} subtitle={data.rule}>
                <ul className="space-y-1.5 text-sm">
                  <li>Trend: adjusted close above its 200-day average.</li>
                  <li>Momentum score: return from 12 months to 1 month ago ÷ one-year volatility.</li>
                  <li>Hold the top fifth of the universe by that score among stocks in an uptrend, equal weight; empty slots stay in cash at 0%.</li>
                  <li>Costs per side: buy <span className="num">{pctOf(data.costs.buy, 3)}</span>, sell <span className="num">{pctOf(data.costs.sell, 3)}</span> (STT, stamp duty, exchange and SEBI fees, GST, 0.05% slippage; no brokerage).</li>
                </ul>
                <p className="mt-3 text-[11px] text-muted">{data.data.prices}. {data.data.index}.</p>
              </Card>
              <Card title="Universe and data coverage" icon={<ShieldAlert className="size-4" />}
                subtitle={`${data.universe.symbols} symbols · ${data.universe.name}${data.universe.constituents_file ? ` · ${data.universe.constituents_file}` : ""}`}>
                <div className="max-h-[300px] overflow-y-auto">
                  <Table>
                    <thead><tr><th>Symbol</th><th>From</th><th className="text-right!">Days</th><th>Adjustments</th></tr></thead>
                    <tbody>
                      {coverage.map(([sym, c]) => (
                        <tr key={sym}>
                          <td className="text-xs font-medium"><Link href={`/stocks/${sym}`} className="hover:text-brand">{sym}</Link></td>
                          <td className="num text-xs">{c.first ? day(c.first) : "—"}</td>
                          <td className="num text-right text-xs">{c.days.toLocaleString("en-IN")}</td>
                          <td className="text-xs">
                            {(c.splits_bonuses_applied ?? []).map(([d, f]) => <Badge key={d} tone="info">{f}× {d.slice(0, 7)}</Badge>)}
                            {(c.unexplained_jumps ?? []).length > 0 && <Badge tone="warn">{c.unexplained_jumps!.length} jump{c.unexplained_jumps!.length > 1 ? "s" : ""} excluded</Badge>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                </div>
              </Card>
            </div>
            <p className="text-[11px] text-muted">Generated {day(data.generated)} from NSE data. A personal research backtest, not a track record and not investment advice.</p>
          </>
        )}
      </div>
    </div>
  );
}
