"use client";

// Sections of the book's scenarios page (#394). Every figure may be null: it then reads "unknown" with the reasons.

import { Fig } from "@/components/fno/fit";
import { pnlTone, rs } from "@/components/fno/paper-trade";
import type { BookScenarios, EpisodeView, Scan, ShockView } from "@/components/fno/scenario-types";
import { Card, InfoTip, Table } from "@/components/ui";
import { day } from "@/lib/api";

export const loss = (x: number | null | undefined) => (x == null ? "unknown" : `₹${Math.round(x).toLocaleString("en-IN")}`);
const pct = (m: number | null, d = 1) => (m == null ? "unknown" : `${m > 0 ? "+" : m < 0 ? "−" : ""}${Math.abs(m * 100).toFixed(d)}%`);
const BUCKET: Record<string, string> = { this_week: "This week", this_month: "This month", later: "Later" };

export function Reasons({ items }: { items: string[] }) {
  if (!items.length) return null;
  return <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted [overflow-wrap:anywhere]">{[...new Set(items)].map((r) => <li key={r}>{r}</li>)}</ul>;
}

export function ScanCard({ scans, stored }: { scans: Scan[]; stored: [string, string] | null }) {
  return (
    <Card title="Historical scan" subtitle={stored ? `Today's book under every stored start day's moves, ${day(stored[0])} to ${day(stored[1])}` : "Today's book under every stored start day's moves"}
      help="For each stored session s, the book is revalued h sessions ahead with each underlying's own front-future move and IV30 change from s to s + h (beta × NIFTY where it has none). Sticky strike: the skew change is not applied.">
      <div className="grid gap-4 lg:grid-cols-2">
        {scans.map((x) => (
          <div key={x.h} className="min-w-0 space-y-2">
            <p className="text-sm font-medium">{x.h === 1 ? "1 session" : `${x.h} sessions`}: {x.n.toLocaleString("en-IN")} start days{x.skipped ? `, ${x.skipped} skipped (a missing return or IV30)` : ""}</p>
            <dl className="grid grid-cols-2 gap-2 text-sm">
              <div className="min-w-0"><dt className="text-xs text-muted">99th-percentile loss</dt><dd><Fig className="text-loss">{loss(x.loss_99)}</Fig></dd></div>
              <div className="min-w-0"><dt className="text-xs text-muted">ES97.5 (historical)</dt><dd><Fig className="text-loss">{loss(x.es)}</Fig></dd></div>
            </dl>
            <Reasons items={[...x.reasons, ...x.es_reasons]} />
            {Object.entries(x.mapped).filter(([, n]) => n).map(([u, n]) => <p key={u} className="text-xs text-muted [overflow-wrap:anywhere]">{u}: beta × NIFTY on {n} start days (no own stored move)</p>)}
            {x.worst.length > 0 && (
              <Table label={`Ten worst start days, ${x.h} session(s)`} bleed={false}>
                <thead><tr><th>From</th><th>To</th><th className="text-right">NIFTY</th><th className="text-right">IV30</th><th className="text-right">P&amp;L</th></tr></thead>
                <tbody>{x.worst.map((d) => (
                  <tr key={d.start}><td>{day(d.start)}</td><td>{day(d.end)}</td><td className="num text-right">{pct(d.move)}</td>
                    <td className="num text-right">{d.dvol > 0 ? "+" : ""}{d.dvol.toFixed(1)}</td><td className={`num text-right ${pnlTone(d.pnl)}`}>{rs(d.pnl)}</td></tr>))}</tbody>
              </Table>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

export function EpisodesCard({ episodes, h }: { episodes: EpisodeView[]; h: number }) {
  return (
    <Card title="Multi-week episodes" subtitle={`Daily paths of up to ${h} sessions; legs expiring inside the path at intrinsic`}
      help="Each start from the last session before the window through the window is tried; the path whose worst point is lowest is shown.">
      <ul className="space-y-3 text-sm">
        {episodes.map((e) => (
          <li key={e.key} className="min-w-0 space-y-1">
            <p className="font-medium [overflow-wrap:anywhere]">{e.label} <span className="text-xs font-normal text-muted">({day(e.window[0])} to {day(e.window[1])})</span></p>
            {e.worst ? (
              <p className="[overflow-wrap:anywhere]">Worst point <span className={`num whitespace-nowrap ${pnlTone(e.worst.pnl)}`}>{rs(e.worst.pnl)}</span> after {e.worst.d} sessions
                (NIFTY {pct(e.worst.move)}, IV30 {e.worst.dvol > 0 ? "+" : ""}{e.worst.dvol.toFixed(1)}), path from {day(e.start)} to {day(e.worst.day)}; end of path <span className={`num whitespace-nowrap ${pnlTone(e.path.at(-1)!.pnl)}`}>{rs(e.path.at(-1)!.pnl)}</span>.</p>
            ) : <p className="text-muted">unknown</p>}
            <Reasons items={e.reasons} />
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function ShocksCard({ gaps, expiry, note }: { gaps: ShockView[]; expiry: ShockView[]; note: string }) {
  const rows = [...gaps, ...expiry];
  return (
    <Card title="Gap and expiry-day scenarios" subtitle="NIFTY's stored overnight gaps (at once) and expiry-day moves (at the nearest expiry's close)" help={note}>
      {rows.length ? (
        <Table label="Gap and expiry-day scenarios" bleed={false}>
          <thead><tr><th>Scenario</th><th className="text-right">NIFTY</th><th className="text-right">Observations</th><th className="text-right">P&amp;L</th></tr></thead>
          <tbody>{rows.map((x) => (
            <tr key={`${x.kind}:${x.label}`}><td>{x.kind === "gap" ? "Overnight gap" : "Expiry day"}, {x.label}</td><td className="num text-right">{pct(x.move)}</td>
              <td className="num text-right">{x.n.toLocaleString("en-IN")}</td><td className={`num text-right ${pnlTone(x.pnl)}`}>{x.pnl == null ? "unknown" : rs(x.pnl)}</td></tr>))}</tbody>
        </Table>
      ) : <p className="text-sm text-muted">Nothing to stress.</p>}
      <Reasons items={rows.flatMap((x) => x.reasons)} />
    </Card>
  );
}

export function LiquidityCard({ liq }: { liq: BookScenarios["liquidity"] }) {
  return (
    <Card title="Liquidity" subtitle="Exit cost on top of the stress loss" help={liq.note}>
      <dl className="grid gap-3 text-sm sm:grid-cols-2">
        <div className="min-w-0"><dt className="text-xs text-muted">Liquidity-adjusted stress <InfoTip>L_liq = L_stress + Σ |q| × mid × relative half-spread × k at the scenario&apos;s moneyness and DTE (#394).</InfoTip></dt>
          <dd className="text-muted">unknown</dd><dd className="text-xs text-muted [overflow-wrap:anywhere]">{liq.reason}</dd></div>
        <div className="min-w-0"><dt className="text-xs text-muted">Quoted exit cost today</dt><dd><Fig>{loss(liq.quoted_exit_cost)}</Fig></dd><Reasons items={liq.reasons} /></div>
      </dl>
    </Card>
  );
}

export function ExposuresCard({ ex }: { ex: BookScenarios["exposures"] }) {
  const cols = ["rs_delta", "rs_gamma_1pct", "vega_pt", "theta_day"] as const;
  return (
    <Card title="Exposures" subtitle="₹ delta per 100% move, ₹ gamma P&L of a 1% move, ₹ vega per vol point, ₹ theta per day"
      help="Black-76 on each leg's forward with its own IV inverted from its mark. NIFTY-equivalent delta = Σ β Δ F q ÷ NIFTY (point β).">
      <Table label="Exposures by underlying and expiry bucket" bleed={false}>
        <thead><tr><th>Underlying</th><th>Bucket</th><th className="text-right">Legs</th><th className="text-right">Δ ₹</th><th className="text-right">Γ ₹ (1%)</th><th className="text-right">Vega ₹</th><th className="text-right">Θ ₹/day</th></tr></thead>
        <tbody>
          {ex.rows.map((r) => (
            <tr key={`${r.underlying}:${r.bucket}`}><td className="max-w-48 [overflow-wrap:anywhere]">{r.underlying}</td><td>{BUCKET[r.bucket] ?? r.bucket}</td><td className="num text-right">{r.legs}</td>
              {cols.map((k) => <td key={k} className="num text-right">{r.exposure ? rs(r.exposure[k]) : "unknown"}</td>)}</tr>))}
          <tr className="font-medium"><td>Total</td><td /><td /> {cols.map((k) => <td key={k} className="num text-right">{ex.total ? rs(ex.total[k]) : "unknown"}</td>)}</tr>
        </tbody>
      </Table>
      <p className="mt-2 text-sm">NIFTY-equivalent delta: <span className="num whitespace-nowrap">{ex.nifty_delta == null ? "unknown" : `${ex.nifty_delta.toFixed(1)} units`}</span></p>
      <Reasons items={[...ex.reasons, ...ex.nifty_delta_reasons, ...ex.flags]} />
    </Card>
  );
}
