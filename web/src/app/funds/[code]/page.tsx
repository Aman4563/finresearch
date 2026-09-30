"use client";

import { Activity, ArrowLeft, BarChart3, ChartLine, FlaskConical, Loader2, PieChart, Scale, ShieldAlert, TrendingDown, Users } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { BarsChart, TimeSeriesChart, shortDate } from "@/components/charts";
import { ensureFund, startResearch } from "@/components/markets/actions";
import { ReturnHistogram } from "@/components/markets/charts";
import { Metric, type Period, PeriodChart, pctOf, toneOf } from "@/components/markets/common";
import { FundConsistencyCard } from "@/components/markets/signal-charts";
import { SipCalculator } from "@/components/markets/sip";
import type { FundAnalytics, FundPeers } from "@/components/markets/types";
import { SignalCard } from "@/components/signal";
import { Button, Callout, Card, EmptyState, ErrorNote, Field, PageHeader, Segmented, Skeleton, Stat, cx, inputClass } from "@/components/ui";
import { day, useApi } from "@/lib/api";

const PERIODS: Period[] = ["1M", "3M", "6M", "1Y", "3Y", "5Y"];

export default function FundDetail() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const [rfDraft, setRfDraft] = useState("6.5");
  const rf = Math.max(0, Math.min(20, Number(rfDraft) || 0)) / 100;
  const [period, setPeriod] = useState<Period>("3Y");
  const [rollWin, setRollWin] = useState<"1y" | "3y">("1y");
  const [rollView, setRollView] = useState<"dist" | "time">("dist");
  const [wantPeers, setWantPeers] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const a = useApi<FundAnalytics>(`/api/funds/${code}/analytics?years=5&rf=${rf.toFixed(4)}`);
  const peers = useApi<FundPeers>(wantPeers ? `/api/funds/${code}/peers` : null);
  const d = a.data;
  const s = d?.scheme;
  const navs = useMemo(() => d?.navs ?? [], [d]);
  const spanYears = navs.length > 1 ? (new Date(navs[navs.length - 1].date).getTime() - new Date(navs[0].date).getTime()) / (365.25 * 86400000) : 0;

  const underwater = useMemo(() => {
    const out: { date: string; drawdown: number }[] = [];
    for (let i = 0, peak = 0; i < navs.length; i++) {
      peak = Math.max(peak, navs[i].nav);
      out.push({ date: navs[i].date, drawdown: (navs[i].nav / peak - 1) * 100 });
    }
    return out;
  }, [navs]);

  const research = async () => {
    if (!s || !confirm(`Start a full research run for ${s.name} (${s.plan}, ${s.option})? It uses your Claude plan window.`)) return;
    setBusy(true);
    try {
      router.push(`/runs/${await startResearch(await ensureFund(s.scheme_code), "fund_report")}`);
    } catch (e) {
      setActionError((e as Error).message);
      setBusy(false);
    }
  };

  const roll = d?.rolling[rollWin];
  const trailingRows = (["1y", "3y", "5y"] as const).map((k) => ({
    period: k.toUpperCase(),
    fund: d?.trailing[k] != null ? d.trailing[k]! * 100 : null,
    median: peers.data?.periods[k]?.median != null ? peers.data.periods[k].median! * 100 : null,
  }));

  return (
    <div>
      <Link href="/funds" className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-muted transition hover:text-brand">
        <ArrowLeft className="size-3.5" /> All funds
      </Link>
      <PageHeader
        icon={<PieChart className="size-5" />}
        eyebrow={s?.category ?? `AMFI scheme ${code}`}
        title={s?.name ?? (a.error ? `Scheme ${code}` : <Skeleton className="h-7 w-72" />)}
        description={s ? <>{s.amc} · {s.plan} · {s.option} · AMFI code <span className="num">{s.scheme_code}</span>{s.isin && <> · ISIN <span className="num">{s.isin}</span></>}</> : undefined}
        actions={
          <Button size="md" disabled={busy || !s} onClick={research} icon={<FlaskConical className="size-4" />}>
            Research
          </Button>
        }
      />

      <div className="space-y-5">
        <ErrorNote error={actionError} />
        {a.error && <ErrorNote error={`Could not load the NAV history from AMFI: ${a.error}`} onRetry={a.reload} />}
        {!d && !a.error && (
          <Callout tone="info" icon={<Loader2 className="size-4 animate-spin" />}>
            Downloading five years of NAVs from AMFI. The first time for a fund house this can take a minute; after that it&apos;s cached.
          </Callout>
        )}

        <div className="grid [&>*]:min-w-0 grid-cols-2 gap-3 lg:grid-cols-4 stagger">
          {!d ? (
            Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[104px] rounded-xl" />)
          ) : (
            <>
              <Stat label="Latest NAV" icon={<ChartLine className="size-4" />} value={s?.nav ? Number(s.nav) : null}
                format={(n) => `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`} hint={`as of ${day(s?.nav_date)}`}
                help="Net asset value: the price of one unit, published by AMFI after each business day." />
              {(["1y", "3y", "5y"] as const).map((k) => (
                <Stat key={k} label={`${k.toUpperCase()} return${k === "1y" ? "" : " (CAGR)"}`} tone={(d.trailing[k] ?? 0) >= 0 ? "gain" : "loss"}
                  icon={<BarChart3 className="size-4" />}
                  display={<span className={cx("num", toneOf(d.trailing[k]))}>{pctOf(d.trailing[k])}</span>}
                  hint={d.trailing[k] == null ? "history too short" : "a year, point to point"}
                  help={k === "1y" ? "How much the NAV rose over the last year." : "CAGR: the steady yearly growth rate that gets from the NAV then to the NAV now."} />
              ))}
            </>
          )}
        </div>

        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
          <SignalCard asset="fund" instrument={code} title="Invest, hold or switch?" />
          <FundConsistencyCard code={code} />
        </div>

        <Card title="NAV history" icon={<ChartLine className="size-4" />} subtitle="Daily NAV from AMFI (growth option: dividends stay invested)">
          {!d ? (
            <Skeleton className="h-[320px] w-full rounded-lg" />
          ) : navs.length < 2 ? (
            <EmptyState title="No NAV history">AMFI returned no NAVs for this scheme in the last five years.</EmptyState>
          ) : (
            <PeriodChart data={navs} series={[{ key: "nav", label: "NAV" }]} periods={PERIODS} period={period} onPeriod={setPeriod}
              format={(v) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`} />
          )}
        </Card>

        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
          <Card title="Trailing returns" icon={<BarChart3 className="size-4" />}
            subtitle={peers.data ? `Against ${peers.data.peers} direct-growth schemes in ${peers.data.category}` : "Yearly returns over the last 1, 3 and 5 years"}
            help="Point-to-point returns ending on the latest NAV date. Comparing with the category median shows whether the fund beat similar funds, not just the market."
            actions={!wantPeers && d ? (
              <Button variant="secondary" onClick={() => setWantPeers(true)} icon={<Users className="size-3.5" />}>Compare with category</Button>
            ) : undefined}>
            {!d ? (
              <Skeleton className="h-[220px] w-full rounded-lg" />
            ) : (
              <>
                <BarsChart data={trailingRows} x="period" height={220} format={(v) => `${v.toFixed(1)}%`}
                  series={[{ key: "fund", label: "This fund", color: "var(--chart-1)" }, ...(peers.data ? [{ key: "median", label: "Category median", color: "var(--chart-2)" }] : [])]} />
                {peers.error && <div className="mt-3"><ErrorNote error={peers.error} onRetry={peers.reload} /></div>}
                {wantPeers && !peers.data && !peers.error && (
                  <p className="mt-3 flex items-center gap-2 text-xs text-muted"><Loader2 className="size-3.5 animate-spin" /> Reading every scheme&apos;s NAV on three dates from AMFI…</p>
                )}
                {peers.data && (
                  <div className="mt-3 grid grid-cols-3 gap-2">
                    {(["1y", "3y", "5y"] as const).map((k) => {
                      const p = peers.data!.periods[k];
                      return (
                        <Metric key={k} label={`${k.toUpperCase()} rank`}
                          value={p.rank ? `${p.rank} / ${p.count}` : "—"}
                          sub={p.rank ? `top ${Math.max(1, Math.round((p.rank / p.count) * 100))}% · median ${pctOf(p.median, 1)}` : "not enough history"} />
                      );
                    })}
                  </div>
                )}
              </>
            )}
          </Card>

          <Card title="Rolling returns" icon={<Activity className="size-4" />}
            subtitle={roll ? `${roll.count} overlapping ${rollWin === "1y" ? "1-year" : "3-year"} windows, one starting each week` : "Every holding period, not just the latest"}
            help="Instead of one start date, this looks at every 1- or 3-year holding period in the history (a new one each week). It shows how often an investor made money and how widely outcomes varied, which a single trailing return hides."
            actions={<Segmented value={rollWin} onChange={setRollWin} options={[{ value: "1y", label: "1Y" }, { value: "3y", label: "3Y" }]} />}>
            {!d ? (
              <Skeleton className="h-[220px] w-full rounded-lg" />
            ) : !roll ? (
              <EmptyState title="Not enough history">This needs more than {rollWin === "1y" ? "one year" : "three years"} of NAVs.</EmptyState>
            ) : (
              <>
                <div className="mb-3 flex justify-end">
                  <Segmented value={rollView} onChange={setRollView} options={[{ value: "dist", label: "Distribution" }, { value: "time", label: "Over time" }]} />
                </div>
                {rollView === "dist" ? (
                  <>
                    <ReturnHistogram bins={roll.histogram} median={roll.median} reference={{ value: rf, label: `risk-free ${pctOf(rf, 1)}` }} height={200} />
                    <p className="mt-1 text-center text-[11px] text-muted">
                      Yearly return of each window (x) against the share of windows (y). Dashed amber line: risk-free rate; grey line: median.
                    </p>
                  </>
                ) : (
                  <TimeSeriesChart height={200} area={false} showChange={false}
                    data={roll.series.map((p) => ({ date: p.date, r: p.return * 100 }))}
                    series={[{ key: "r", label: `${rollWin.toUpperCase()} return, window starting`, color: "var(--chart-1)" }]}
                    references={[{ y: 0, label: "0%" }]} format={(v) => `${v.toFixed(1)}%`} />
                )}
                <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric label="Worst" value={pctOf(roll.min, 1)} tone={toneOf(roll.min)} />
                  <Metric label="Median" value={pctOf(roll.median, 1)} tone={toneOf(roll.median)} />
                  <Metric label="Best" value={pctOf(roll.max, 1)} tone={toneOf(roll.max)} />
                  <Metric label="Made money" value={pctOf(roll.share_positive, 0)} sub={`${pctOf(roll.share_above_rf, 0)} beat risk-free`} />
                </div>
              </>
            )}
          </Card>
        </div>

        <Card title="Risk" icon={<ShieldAlert className="size-4" />} subtitle={d ? `Over ${spanYears.toFixed(1)} years of daily NAVs` : undefined}
          help="How bumpy the ride has been. Return alone hides how much you'd have had to sit through to earn it.">
          {!d ? (
            <Skeleton className="h-[200px] w-full rounded-lg" />
          ) : (
            <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-[minmax(0,320px)_1fr]">
              <div className="space-y-3">
                <div className="grid [&>*]:min-w-0 grid-cols-2 gap-2">
                  <Metric label="Volatility (yearly)" value={pctOf(d.risk.annualised_volatility, 1)}
                    help="How much the NAV swings, annualised (standard deviation of daily changes × √252). Equity funds are often 12–20%; debt funds 1–3%." />
                  <Metric label="Max drawdown" value={pctOf(d.risk.max_drawdown, 1)} tone="text-loss"
                    sub={d.risk.drawdown_peak ? `${shortDate(d.risk.drawdown_peak)} → ${shortDate(d.risk.drawdown_trough!)}` : undefined}
                    help="The worst fall from a peak to a later low: what someone who bought at the top would have been down at the bottom." />
                  <Metric label="Sharpe ratio" value={d.risk.sharpe?.toFixed(2) ?? "—"} tone={toneOf(d.risk.sharpe)}
                    help="Return above the risk-free rate per unit of volatility. Above 1 is good; below 0 means a risk-free deposit would have done better." />
                  <Metric label="Sortino ratio" value={d.risk.sortino?.toFixed(2) ?? "—"} tone={toneOf(d.risk.sortino)}
                    help="Like Sharpe, but only counts downside swings as risk (upside volatility isn't punished)." />
                </div>
                <Field label="Risk-free rate for Sharpe / Sortino (% a year)" hint="An assumption you choose, e.g. the 91-day T-bill yield or your FD rate.">
                  <input className={cx(inputClass, "num w-32")} inputMode="decimal" value={rfDraft} onChange={(e) => setRfDraft(e.target.value.replace(/[^0-9.]/g, ""))} />
                </Field>
              </div>
              <div className="min-w-0">
                <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted"><TrendingDown className="size-3.5" /> Drawdown: how far below its previous peak the NAV was</p>
                <TimeSeriesChart data={underwater} series={[{ key: "drawdown", label: "Below peak", color: "var(--loss)" }]} height={200}
                  showChange={false} format={(v) => `${v.toFixed(0)}%`} yDomain={["dataMin", 0]} />
              </div>
            </div>
          )}
        </Card>

        {d && navs.length > 30 && <SipCalculator code={code} maxYears={Math.floor(spanYears + 0.02)} />}

        {d && (
          <p className="flex flex-wrap items-center gap-1 text-[11px] text-muted">
            <Scale className="size-3" /> Source: <a href={d.source} target="_blank" rel="noreferrer" className="underline underline-offset-2">AMFI NAV history</a>. Returns and risk are
            computed by FinResearch (fincalc) from those NAVs. Past performance does not predict future returns. Not investment advice.
          </p>
        )}
      </div>
    </div>
  );
}
