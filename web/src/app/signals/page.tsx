"use client";

import { CalendarClock, CheckCircle2, Clock, FlaskConical, Hourglass, Info, Radar, Scale, Target, XCircle } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { ReliabilityDiagram } from "@/components/signals/reliability";
import { SignalCard, type Signal } from "@/components/signal";
import {
  Badge, Callout, Card, EmptyState, ErrorNote, InfoTip, PageHeader, Segmented, Skeleton, SkeletonRows, Stat, Table, cx,
} from "@/components/ui";
import { type Calibration, type CalibrationGroup, type Company, day, type Forecast, type ForecastPage, useApi, type WatchSummary } from "@/lib/api";

type Asset = Signal["asset"];
type Instrument = { key: string; asset: Asset; instrument: string; name: string; slug: string; watched: boolean; run: number | null };

const ASSET_LABEL: Record<string, string> = { ipo: "IPO", stock: "Stock", fund: "Fund", bond: "Bond", fno: "F&O", all: "All" };
const ASSETS: Asset[] = ["ipo", "stock", "fund", "bond", "fno"];
const NOUN: Record<string, string> = { ipo: "IPO", stock: "stock", fund: "mutual-fund", bond: "bond", fno: "F&O" };
const KIND_ASSET: Record<string, Asset> = { ipo_report: "ipo", stock_report: "stock", fund_report: "fund", bond_report: "bond" };
const pc = (p: number | null | undefined, d = 0) => (p == null ? "—" : `${(p * 100).toFixed(d)}%`);
const num = (v: number | null | undefined, d = 3) => (v == null ? "—" : v.toFixed(d));

/** The instrument a researched or watched company maps to, per asset class. */
function instrumentOf(c: Company): Instrument | null {
  const asset = KIND_ASSET[c.kind];
  if (!asset) return null;
  const id = asset === "fund" ? c.slug.replace(/^mf-/, "") : asset === "bond" ? c.slug.replace(/^bond-/, "").toUpperCase()
    : asset === "stock" ? (c.key ?? c.nse_symbol) : c.nse_symbol; // a BSE-only stock: "BSE:<code>"
  if (!id) return null;
  return { key: `${asset}:${id}`, asset, instrument: id, name: c.name, slug: c.slug, watched: false, run: c.latest_run };
}

function resolvesText(f: Forecast) {
  return f.asset === "ipo" ? `lists (expected ${day(f.resolve_on)})` : `reaches its ${f.horizon} mark on ${day(f.resolve_on)}`;
}

export default function Signals() {
  const companies = useApi<Company[]>("/api/companies");
  const watches = useApi<WatchSummary[]>("/api/watches", 60000);
  const available = useApi<{ assets: { asset: Asset; available: boolean }[]; disclaimer: string }>("/api/signals");
  const cal = useApi<Calibration>("/api/calibration", 120000);
  const [view, setView] = useState<"open" | "resolved" | "all">("all");
  const forecasts = useApi<ForecastPage>(`/api/forecasts?limit=100${view === "all" ? "" : `&status=${view}`}`, 120000);
  const [groupKey, setGroupKey] = useState<string>("");
  const [showAll, setShowAll] = useState(false);

  const instruments = useMemo(() => {
    const out = new Map<string, Instrument>();
    const watchedSlugs = new Set((watches.data ?? []).filter((w) => w.active).map((w) => w.company));
    for (const c of companies.data ?? []) {
      const i = instrumentOf(c);
      if (!i) continue;
      i.watched = watchedSlugs.has(c.slug);
      if (i.watched || i.run != null) out.set(i.key, i);
    }
    return [...out.values()].sort((a, b) => Number(b.watched) - Number(a.watched) || a.name.localeCompare(b.name));
  }, [companies.data, watches.data]);

  const have = new Map((available.data?.assets ?? []).map((a) => [a.asset, a.available]));
  const live = instruments.filter((i) => have.get(i.asset));
  const coming = ASSETS.map((a) => [a, instruments.filter((i) => i.asset === a && !have.get(a))] as const).filter(([, l]) => l.length > 0);
  const groups = cal.data?.groups ?? [];
  const overall = groups.find((g) => g.asset === "all");
  const perMethod = groups.filter((g) => g.asset !== "all");
  const selected: CalibrationGroup | undefined = groups.find((g) => `${g.asset}|${g.method}` === groupKey) ?? overall;
  const next = cal.data?.next_open ?? null;
  const loadingCal = !cal.data && !cal.error;

  return (
    <div className="space-y-6">
      <PageHeader icon={<Radar className="size-5" />} eyebrow="Research" title="Signals"
        description="Buy/sell signals for what you research and watch, and an honest track record: every call is logged as a probability before the outcome, then scored." />

      <div className="stagger grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Instruments" value={companies.data ? instruments.length : null} icon={<Target className="size-4" />}
          hint="researched or watched" />
        <Stat label="Open forecasts" value={overall ? overall.open : cal.data ? 0 : null} icon={<Hourglass className="size-4" />} tone="info"
          hint={cal.error ? "unavailable" : next ? `next: ${next.name ?? next.instrument} ${day(next.resolve_on)}` : "waiting for outcomes"} />
        <Stat label="Brier score" icon={<Scale className="size-4" />} tone="neutral"
          help="Average of (forecast − outcome)², where the outcome is 1 if the event happened and 0 if not. 0 is perfect; always saying 50% scores 0.25. Lower is better."
          display={<span className="num">{loadingCal ? "—" : num(overall?.brier)}</span>}
          hint={cal.error ? "unavailable" : overall?.brier_reference != null ? `base-rate reference ${num(overall.brier_reference)}` : `${overall?.n ?? 0} scored`} />
        <Stat label="Skill vs base rate" icon={<FlaskConical className="size-4" />} tone={overall?.brier_skill != null && overall.brier_skill > 0 ? "gain" : "neutral"}
          help="Brier skill score = 1 − Brier ÷ reference, where the reference always forecasts the observed base rate. Above 0 means the calls beat simply knowing how often the event happens; below 0 means they did worse."
          display={<span className="num">{loadingCal ? "—" : overall?.brier_skill == null ? "—" : `${overall.brier_skill > 0 ? "+" : ""}${overall.brier_skill.toFixed(2)}`}</span>}
          hint={cal.error ? "unavailable" : overall?.brier_skill == null ? "needs resolved outcomes of both kinds" : `n = ${overall.n}`} />
      </div>

      <section className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold tracking-tight">Your instruments</h2>
          <InfoTip>Everything you have researched or are watching. Each signal is the probability of a stated event with a range, the factors behind it and how it was validated. Python computes it from public data; it is not investment advice.</InfoTip>
        </div>
        <ErrorNote error={companies.error ?? available.error} onRetry={() => { companies.reload(); available.reload(); }} />
        {!companies.data && !companies.error ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-48 rounded-xl" />)}</div>
        ) : companies.data && instruments.length === 0 ? (
          <EmptyState icon={<Target className="size-5" />} title="Nothing researched or watched yet">
            Research an IPO, stock, fund or bond, or watch one from the <Link href="/monitor" className="text-brand hover:underline">Monitor</Link>; its signal appears here.
          </EmptyState>
        ) : (
          <div className="space-y-3">
            {!available.data && !available.error ? <Skeleton className="h-40 rounded-xl" /> : live.length > 0 && (
              <div className="stagger grid gap-3 md:grid-cols-2 xl:grid-cols-3 [&>*]:min-w-0">
                {(showAll ? live : live.slice(0, 6)).map((i) => (
                  <div key={i.key} className="space-y-1.5">
                    <InstrumentLine i={i} />
                    <SignalCard asset={i.asset} instrument={i.instrument} title={`${ASSET_LABEL[i.asset]} signal`} />
                  </div>
                ))}
              </div>
            )}
            {live.length > 6 && (
              <button type="button" onClick={() => setShowAll((v) => !v)} className="text-xs font-medium text-brand">
                {showAll ? "Show fewer" : `Show all ${live.length} signals`}
              </button>
            )}
            {available.data && coming.length > 0 && (
              <div className="stagger grid gap-3 md:grid-cols-2 xl:grid-cols-4 [&>*]:min-w-0">
                {coming.map(([asset, list]) => <ComingCard key={asset} asset={asset} items={list} />)}
              </div>
            )}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold tracking-tight">Track record</h2>
          <InfoTip>A forecaster is calibrated when things it calls 70% likely happen about 70% of the time. The ledger checks that by logging each call as a probability before the outcome is known and scoring it once the outcome is in.</InfoTip>
        </div>
        <ErrorNote error={cal.error} onRetry={cal.reload} />
        <div className="grid gap-4 lg:grid-cols-5 [&>*]:min-w-0">
          {cal.error ? null : <Card className="lg:col-span-3" title="Calibration" icon={<Target className="size-4" />}
            help="Each dot is a group of forecasts: across is what was forecast, up is how often it happened, and the whisker is the 95% Wilson interval. On the dashed diagonal is perfect; with few cases the whiskers are long and nothing can be concluded yet."
            subtitle={selected && selected.asset !== "all" ? `${ASSET_LABEL[selected.asset]} · ${selected.method}` : "All live methods pooled (shadow tests apart)"}
            actions={perMethod.length > 1 ? (
              <select aria-label="Method" className="max-w-44 truncate rounded-md border border-border bg-card px-2 py-1 text-xs"
                value={groupKey} onChange={(e) => setGroupKey(e.target.value)}>
                <option value="">All methods</option>
                {perMethod.map((g) => <option key={`${g.asset}|${g.method}`} value={`${g.asset}|${g.method}`}>{ASSET_LABEL[g.asset]}: {g.validation_status === "shadow" ? "[shadow test] " : ""}{g.method}</option>)}
              </select>
            ) : undefined}>
            {loadingCal ? <Skeleton className="mx-auto aspect-square w-full max-w-[380px]" /> : (
              <div className="grid items-center gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,200px)]">
                <div className="relative">
                  <ReliabilityDiagram bins={selected?.bins ?? []} baseRate={selected?.base_rate ?? null} />
                  {!selected?.n && (
                    <div className="absolute inset-0 grid place-items-center p-8">
                      <p className="max-w-72 rounded-lg border border-border bg-card/90 px-3 py-2 text-center text-xs text-muted shadow-card">
                        <EmptyChartNote next={next} scored={cal.data?.next_scored ?? null} />
                      </p>
                    </div>
                  )}
                </div>
                <GroupFacts g={selected} />
              </div>
            )}
          </Card>}
          <div className={cal.error ? "lg:col-span-5" : "lg:col-span-2"}><Callout tone="info" icon={<Info className="size-4" />} title="How the numbers are made">
            <ul className="list-disc space-y-1.5 pl-4 text-xs leading-relaxed">
              <li>Reports only say low / medium / high confidence. Until outcomes say otherwise that maps to fixed probabilities:{" "}
                <span className="num">{Object.entries(cal.data?.confidence_map ?? { low: 0.55, medium: 0.65, high: 0.75 }).map(([k, v]) => `${k} ${pc(v)}`).join(" · ")}</span>{" "}
                that the verdict&apos;s direction is right (AVOID or REDUCE is the same number against the event).</li>
              <li>IPO calls are about the <b>listing-day open above the issue price</b>; stock calls about the <b>12-month return beating the Nifty 50</b> (NIFTYBEES as the index proxy, dividends counted).</li>
              <li>Conditional or neutral verdicts are logged as <b>no call</b>: their outcome is still recorded, but they are not scored. Fund and bond verdicts have no checkable event yet and are not logged.</li>
              <li>A <b>shadow test</b> is a candidate method logged beside the call for out-of-sample scoring, never used for the call; it has its own row in the method list and is left out of the pooled headline.</li>
              <li>Below about {cal.data?.min_n_for_recalibration ?? 50} resolved cases nothing is re-fitted and nothing should be concluded: expect years for stock calls.</li>
            </ul>
          </Callout></div>
        </div>

        <Card title="Forecasts" icon={<CalendarClock className="size-4" />} padded
          subtitle="Open ones resolve automatically after the close on their date; nothing is ever edited after it resolves."
          actions={<Segmented value={view} onChange={setView} options={[{ value: "all", label: "All" }, { value: "open", label: "Open" }, { value: "resolved", label: "Resolved" }]} />}>
          <ErrorNote error={forecasts.error} onRetry={forecasts.reload} />
          {!forecasts.data && !forecasts.error ? <SkeletonRows rows={4} /> : forecasts.data && forecasts.data.items.length === 0 ? (
            <EmptyState icon={<Hourglass className="size-5" />} title={view === "resolved" ? "Nothing resolved yet" : "No forecasts yet"}>
              {view === "resolved" && next
                ? <>The first outcome comes when {next.name ?? next.instrument} {resolvesText(next)}.</>
                : <>A forecast is logged when an IPO or stock report finishes, or when a signal provider records one.</>}
            </EmptyState>
          ) : forecasts.data && <ForecastTable rows={forecasts.data.items} total={forecasts.data.total} />}
        </Card>
        {available.data && <p className="flex items-start gap-1.5 text-[11px] text-muted"><Info className="mt-0.5 size-3 shrink-0" />{available.data.disclaimer}</p>}
      </section>
    </div>
  );
}

function EmptyChartNote({ next, scored }: { next: Forecast | null; scored: Forecast | null }) {
  const who = (f: Forecast) => <span className="font-medium text-foreground">{f.name ?? f.instrument}</span>;
  if (!next) return <>No forecasts yet. One is logged when an IPO or stock report finishes.</>;
  if (next.probability != null || !scored) {
    return <>No resolved forecasts yet. The first {next.probability == null ? "outcome" : "one"} comes when {who(next)} {resolvesText(next)}.</>;
  }
  return (
    <>No resolved forecasts yet. The first outcome comes when {who(next)} {resolvesText(next)}, but its verdict ({next.action}) was a no call, so it
      is recorded, not scored. The first scored one resolves when {who(scored)} {resolvesText(scored)}.</>
  );
}

function InstrumentLine({ i }: { i: Instrument }) {
  return (
    <div className="flex min-w-0 items-center gap-2 px-1 text-xs">
      <Badge tone="brand">{ASSET_LABEL[i.asset]}</Badge>
      <Link href={i.run != null ? `/runs/${i.run}` : "/monitor"} className="truncate font-medium hover:text-brand">{i.name}</Link>
      <span className="num shrink-0 text-muted">{i.instrument}</span>
      {i.watched && <Badge tone="info">watched</Badge>}
    </div>
  );
}

/** One card per asset class whose signal provider has not shipped yet, listing the instruments waiting for it. */
function ComingCard({ asset, items }: { asset: Asset; items: readonly Instrument[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, 5);
  return (
    <Card title={`${ASSET_LABEL[asset]} signals`} icon={<Clock className="size-4" />} actions={<Badge tone="neutral">coming</Badge>}
      subtitle={`The ${NOUN[asset]} signal model is still being built and validated.`}>
      <ul className="space-y-1.5 text-xs">
        {shown.map((i) => (
          <li key={i.key} className="flex min-w-0 items-center gap-2">
            <Link href={i.run != null ? `/runs/${i.run}` : "/monitor"} className="truncate font-medium hover:text-brand">{i.name}</Link>
            <span className="num shrink-0 text-muted">{i.instrument}</span>
            {i.watched && <Badge tone="info">watched</Badge>}
          </li>
        ))}
      </ul>
      {items.length > 5 && (
        <button type="button" onClick={() => setAll((v) => !v)} className="mt-2 text-xs font-medium text-brand">
          {all ? "Show fewer" : `+${items.length - 5} more`}
        </button>
      )}
      <p className="mt-3 text-[11px] text-muted">It appears here, with its method and track record, once ready. Report verdicts with a checkable call are already in the track record below.</p>
    </Card>
  );
}

const TIER_LABEL: Record<string, string> = { base_rate: "base rate only", shrink: "shrink to base rate", platt: "Platt", isotonic: "isotonic" };

function GroupFacts({ g }: { g: CalibrationGroup | undefined }) {
  if (!g) return <p className="text-xs text-muted">No forecasts logged yet.</p>;
  const rows: [string, React.ReactNode, string][] = [
    ["Scored", <span key="n" className="num">{g.n}</span>, "Resolved forecasts that carried a probability."],
    ["Hit rate", <span key="h" className="num">{g.calls ? `${g.hits}/${g.calls} · ${pc(g.hit_rate)}` : "—"}</span>, "Calls that pointed the right way (above 50% and it happened, or below 50% and it did not)."],
    ["95% CI", <span key="c" className="num">{g.hit_rate_ci ? `${pc(g.hit_rate_ci[0])}–${pc(g.hit_rate_ci[1])}` : "—"}</span>, "Wilson interval for the hit rate: the range the true rate plausibly lies in, given so few cases."],
    ["Base rate", <span key="b" className="num">{pc(g.base_rate)}</span>, "How often the event happened across the scored cases."],
    ["Log loss", <span key="l" className="num">{num(g.log_loss)}</span>, "Punishes confident misses hard; lower is better. 0.693 is what always saying 50% scores."],
    ["Open · no call · void", <span key="o" className="num">{g.open} · {g.no_call} · {g.void}</span>, "Waiting for their date; logged without a probability; could not be scored honestly (e.g. a split in the window)."],
  ];
  if (g.policy) {
    const p = g.policy;
    rows.push(["Calibration tier", <span key="t">{TIER_LABEL[p.tier] ?? p.tier}</span>,
      `${p.description}. Effective n ${p.n_effective}${p.overlap > 1 ? ` (${p.n} forecasts ÷ ${p.overlap} overlapping months)` : ""}${p.next_tier ? `; the next tier starts at n = ${p.next_tier.at_n}` : ""}. Informational: probabilities are not re-fitted yet.`]);
  }
  if (g.validation_status === "shadow")
    rows.push(["Status", <Badge key="s" tone="neutral">shadow test</Badge>, "Shadow test: logged for out-of-sample scoring, not used for the call. The switch criterion is pre-registered."]);
  const enough = g.policy ? g.policy.tier !== "base_rate" : g.n >= 50;
  return (
    <dl className="space-y-2 text-xs">
      {rows.map(([k, v, help]) => (
        <div key={k} className="flex items-center justify-between gap-2 border-b border-border/60 pb-1.5 last:border-0">
          <dt className="flex items-center gap-1 text-muted">{k}<InfoTip>{help}</InfoTip></dt>
          <dd className="whitespace-nowrap">{v}</dd>
        </div>
      ))}
      <dd className="pt-1"><Badge tone={enough ? "info" : "neutral"}>{enough ? "enough cases to read" : "too few cases to judge"}</Badge></dd>
    </dl>
  );
}

function Outcome({ f }: { f: Forecast }) {
  if (f.status === "open") return <Badge status="pending">{f.resolution_note?.startsWith("waiting") ? "waiting" : "open"}</Badge>;
  if (f.status === "void") return <span title={f.resolution_note ?? undefined}><Badge tone="warn">void</Badge></span>;
  const happened = f.outcome === 1;
  const hit = f.probability == null ? null : (f.probability > 0.5) === happened;
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap" title={`${happened ? "The event happened" : "The event did not happen"}. ${f.resolution_note ?? ""}`}>
      {hit == null ? <Badge tone="neutral">no call</Badge> : hit
        ? <span className="inline-flex items-center gap-1 text-gain"><CheckCircle2 className="size-3.5" />hit</span>
        : <span className="inline-flex items-center gap-1 text-loss"><XCircle className="size-3.5" />miss</span>}
      {hit == null && <span className="text-muted">{happened ? "happened" : "did not happen"}</span>}
      {f.resolution_value != null && <span className="num text-muted">({f.resolution_value > 0 ? "+" : ""}{f.resolution_value.toFixed(2)}{f.asset === "stock" ? " pp" : "%"})</span>}
    </span>
  );
}

function ForecastTable({ rows, total }: { rows: Forecast[]; total: number }) {
  return (
    <>
      <Table label="Recorded forecasts">
        <thead>
          <tr><th>Instrument</th><th>Call</th><th className="text-right">P(event)</th><th>Event</th><th>Made → resolves</th><th>Outcome</th></tr>
        </thead>
        <tbody>
          {rows.map((f) => (
            <tr key={f.id}>
              <td>
                <div className="flex items-center gap-2">
                  <Badge tone="brand">{ASSET_LABEL[f.asset] ?? f.asset}</Badge>
                  {f.run_id
                    ? <Link href={`/runs/${f.run_id}`} className="block max-w-44 truncate font-medium hover:text-brand" title={`${f.name ?? f.instrument}: research run ${f.run_id}`}>{f.name ?? f.instrument}</Link>
                    : <span className="block max-w-44 truncate font-medium" title={f.source}>{f.name ?? f.instrument}</span>}
                </div>
              </td>
              <td><span className="inline-flex items-center gap-1"><Badge status={f.action}>{f.action}</Badge>{f.validation_status === "shadow" && <span title="Shadow test: logged for out-of-sample scoring, not used for the call."><Badge tone="neutral">shadow</Badge></span>}</span></td>
              <td className="num text-right">{f.probability == null ? <span className="text-muted">no call</span> : pc(f.probability)}</td>
              <td>
                <span className="inline-flex max-w-44 items-center gap-1">
                  <span className="block truncate text-xs text-muted">{f.event}</span>
                  <InfoTip>{f.event}. Horizon: {f.horizon}. Method: {f.method} ({f.validation_status}).</InfoTip>
                </span>
              </td>
              <td className="num text-xs whitespace-nowrap"><span className="text-muted">{day(f.created_at)} →</span> {day(f.resolve_on)}</td>
              <td className={cx("text-xs")}><Outcome f={f} /></td>
            </tr>
          ))}
        </tbody>
      </Table>
      {total > rows.length && <p className="mt-2 text-xs text-muted">Showing the latest {rows.length} of {total}.</p>}
    </>
  );
}
