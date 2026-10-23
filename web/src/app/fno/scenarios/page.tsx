"use client";

import { ShieldAlert } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Fig } from "@/components/fno/fit";
import { pnlTone, rs } from "@/components/fno/paper-trade";
import { RiskBanner } from "@/components/fno/risk";
import { ScenarioHeatmap } from "@/components/fno/scenario-grid";
import { EpisodesCard, ExposuresCard, LiquidityCard, Reasons, ScanCard, ShocksCard, loss } from "@/components/fno/scenario-parts";
import { rateLabel } from "@/components/fno/model";
import type { BookScenarios, ForwardView } from "@/components/fno/scenario-types";
import { Card, EmptyState, ErrorNote, PageHeader, Skeleton, Stat, Table } from "@/components/ui";
import { api, day, when } from "@/lib/api";

const H: Record<string, string> = { "1d": "1 session", "5d": "5 sessions", expiry: "To nearest expiry" };
const pct = (m: number) => `${m > 0 ? "+" : m < 0 ? "−" : ""}${Math.abs(m * 100).toFixed(1)}%`;
const pa = (x: number) => `${(x * 100).toFixed(2)}%`;
// #480: the forward, the carry its scenarios roll it at (or why it is unknown) and the expiry's own discount rate
const fwdLine = (f: ForwardView) => `${f.underlying} ${day(f.expiry)}: forward ${f.forward == null ? "unknown" : f.forward.toLocaleString("en-IN")} (${f.source}); `
  + `carry ${f.carry == null ? `unknown (${f.carry_reason})` : `${pa(f.carry)} a year (${f.carry_source})`}${f.implied_q == null ? "" : `, implied dividend yield ${pa(f.implied_q)}`}; `
  + `discount rate ${rateLabel(f.rate_info)}`;

export default function BookScenariosPage() {
  const [v, setV] = useState<BookScenarios | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setV(await api<BookScenarios>("/api/fno/scenarios"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);
  const es = v?.history.es;
  const worst = v?.grid.cells.filter((c) => c.horizon === "1d" && c.pnl != null).reduce<number | null>((a, c) => (a == null || c.pnl! < a ? c.pnl! : a), null);

  return (
    <div className="space-y-6">
      <PageHeader icon={<ShieldAlert className="size-5" />} eyebrow="Research" title="Book scenarios"
        description="The open F&O book (paper and Groww) under spot, vol and time shocks, replays of real days and episodes, and a scan over every stored day: what a bad day costs the whole book. Analysis only; no orders are ever placed. Local only." />
      <RiskBanner />
      <ErrorNote error={error} onRetry={load} />
      {!v ? (!error && <Skeleton className="h-64 rounded-xl" />) : !v.legs.length ? (
        <EmptyState title="No open legs">Nothing is open in the paper book or the Groww F&amp;O book, so every scenario is ₹0. Open a paper trade from the <Link className="text-brand hover:underline" href="/fno">builder</Link>.</EmptyState>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            <Stat label="ES97.5, 1 session" display={<Fig className="text-loss">{loss(es!.value)}</Fig>}
              hint={es!.value == null ? "unknown: see the scan" : `${es!.n.toLocaleString("en-IN")} start days · ${es!.ratio == null ? "no limit" : `${(es!.ratio * 100).toFixed(0)}% of the ${loss(es!.limit)} limit`}`} help={es!.note} />
            <Stat label="99th-percentile loss, 1 session" display={<Fig className="text-loss">{loss(v.history.scan.find((x) => x.h === 1)?.loss_99)}</Fig>} hint="historical scan" />
            <Stat label="Worst grid cell, 1 session" display={<Fig className={pnlTone(worst ?? null)}>{v.grid.complete ? rs(worst) : "unknown"}</Fig>} hint="−15% to +10%, vol −3 to +20 pts" />
            <Stat label="NIFTY-equivalent delta" display={<Fig>{v.exposures.nifty_delta == null ? "unknown" : v.exposures.nifty_delta.toFixed(1)}</Fig>} hint="units of NIFTY (point β)" />
          </div>
          <Reasons items={[...v.notes, ...es!.reasons]} />
          <Card title="Spot × vol grid" subtitle={`Full revaluation. Spot-vol response: +${v.grid.vol_response.down_per_pct} vol pts per 1% fall, ${v.grid.vol_response.up_per_pct} per 1% rise; the vol shift is on top`}
            help="Every leg repriced in full with Black-76 (no greek approximation), each expiry discounted at its own rate. Each forward is rolled to the horizon at its own expiry's carry (the market's dividend and financing; see the footer), never at the rate. Other underlyings move by the worse end of their beta band × NIFTY's move.">
            <ScenarioHeatmap cells={v.grid.cells} moves={v.grid.moves} vols={v.grid.vol_shifts} />
            <Reasons items={v.grid.reasons} />
          </Card>
          <Card title="Reverse stress" subtitle={v.reverse.limit == null ? "No book limit" : `The smallest grid point whose loss reaches the ${loss(v.reverse.limit)} book loss limit`}>
            {v.reverse.limit == null ? <p className="text-sm text-muted">{v.reverse.reason}</p> : (
              <ul className="space-y-1 text-sm">{v.reverse.rows.map((r) => (
                <li key={r.horizon} className="[overflow-wrap:anywhere]">{H[r.horizon] ?? r.horizon}: {!r.complete ? <span className="text-muted">unknown</span> : r.cell ? <>NIFTY {pct(r.cell.move)}, vol {r.cell.dvol > 0 ? "+" : ""}{r.cell.dvol} pts: <span className={`num whitespace-nowrap ${pnlTone(r.cell.pnl)}`}>{rs(r.cell.pnl)}</span></> : "no grid point breaches the limit"}<Reasons items={r.reasons} /></li>))}</ul>
            )}
          </Card>
          <Card title="Single-day replays" subtitle="Each day's NIFTY move and IV30 change over one session" help={v.history.skew_note}>
            <ul className="space-y-1 text-sm">{v.replays.map((r) => (
              <li key={r.day} className="[overflow-wrap:anywhere]">{r.label}: <span className={`num whitespace-nowrap ${pnlTone(r.pnl)}`}>{r.pnl == null ? "unknown" : rs(r.pnl)}</span> <span className="text-xs text-muted">({r.source})</span><Reasons items={r.reasons} /></li>))}</ul>
          </Card>
          <ScanCard scans={v.history.scan} stored={v.history.stored} />
          <EpisodesCard episodes={v.history.episodes} h={v.history.episode_h} />
          <ShocksCard gaps={v.history.gaps} expiry={v.history.expiry_day} note={v.history.scenario_note} />
          <LiquidityCard liq={v.liquidity} />
          <ExposuresCard ex={v.exposures} />
          <Card title="Betas to NIFTY" subtitle="Registered EWMA estimates from stored front-future settles" help={v.beta_rule}>
            {v.betas.length ? (
              <Table label="Betas to NIFTY" bleed={false}>
                <thead><tr><th>Underlying</th><th className="text-right">β</th><th className="text-right">SE</th><th className="text-right">Band</th><th className="text-right">Returns</th><th>Window</th><th>As of</th></tr></thead>
                <tbody>{v.betas.map((b) => (
                  <tr key={b.underlying}><td className="max-w-48 [overflow-wrap:anywhere]">{b.underlying}</td>
                    {b.beta == null ? <td colSpan={6} className="text-muted [overflow-wrap:anywhere]">unknown: {b.reason}</td> : <>
                      <td className="num text-right">{b.beta.toFixed(2)}</td><td className="num text-right">{b.se?.toFixed(2)}</td>
                      <td className="num text-right">{b.lo?.toFixed(2)} to {b.hi?.toFixed(2)}</td><td className="num text-right">{b.n}{b.missing ? ` (${b.missing} missing)` : ""}</td>
                      <td>{day(b.window_start)}, half-life {b.half_life}</td><td>{day(b.as_of)}</td></>}</tr>))}</tbody>
              </Table>
            ) : <p className="text-sm text-muted">Only NIFTY is held (β = 1 by definition).</p>}
          </Card>
          <p className="text-[11px] text-muted [overflow-wrap:anywhere]">As of {when(v.as_of)}. Per expiry: {v.forwards.map(fwdLine).join(". ")}.</p>
        </>
      )}
      <p className="text-xs text-muted"><Link className="text-brand hover:underline" href="/fno/paper">Paper book →</Link></p>
    </div>
  );
}
