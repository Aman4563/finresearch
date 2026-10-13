"use client";

import { Compass } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge, Callout, Card, ErrorNote, InfoTip, Skeleton, type Tone, cx } from "@/components/ui";
import { api, day } from "@/lib/api";

import type { BuildupCell, Evidence, MarketContext } from "./plan-types";

const n0 = (x: number | null | undefined) => (x == null ? "—" : Math.round(x).toLocaleString("en-IN"));
const n2 = (x: number | null | undefined, d = 2) => (x == null ? "—" : x.toFixed(d));
const signed = (x: number | null | undefined) => (x == null ? "—" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(Math.round(x)).toLocaleString("en-IN")}`);
const BUILD: Record<string, Tone> = { "long build-up": "gain", "short covering": "gain", "short build-up": "loss", "long unwinding": "loss" };

/** The evidence label every indicator carries: what it measures and what is known about its predictive value. */
function Ev({ e }: { e: Evidence }) {
  const tone: Tone = e.level.startsWith("documented") ? "info" : e.level === "none verified" ? "neutral" : "warn";
  return (
    <span className="inline-flex items-center gap-1">
      <Badge tone={tone}>evidence: {e.level}</Badge>
      <InfoTip label="What it measures and the evidence"><b>Measures:</b> {e.measures} <b>Evidence:</b> {e.evidence}</InfoTip>
    </span>
  );
}

function Tile({ title, ev, children }: { title: string; ev: Evidence; children: React.ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg border border-border p-3 text-xs">
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-1">
        <p className="font-semibold">{title}</p>
        <Ev e={ev} />
      </div>
      {children}
    </div>
  );
}

function Cell({ c }: { c: BuildupCell }) {
  return <Badge tone={BUILD[c.buildup] ?? "neutral"}>{c.buildup}</Badge>;
}

/** Market context per underlying (#317): trend, volatility, term, PCR, build-up, participants, events. Context only:
 * none of it feeds the trade-plan verdict. */
export function MarketContextPanel({ symbol, expiry }: { symbol: string; expiry: string }) {
  const [data, setData] = useState<MarketContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<MarketContext>(`/api/fno/${encodeURIComponent(symbol)}/context?expiry=${expiry}`)
      .then((d) => live && (setData(d), setError(null)))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [symbol, expiry]);
  return (
    <Card title="Market context" icon={<Compass className="size-4" />} subtitle={`${symbol}: what the market looks like, with how much each reading is worth`}>
      {error ? <ErrorNote error={error} /> : !data ? <Skeleton className="h-48 rounded-xl" /> : <Body d={data} />}
    </Card>
  );
}

function Body({ d }: { d: MarketContext }) {
  const t = d.trend, v = d.volatility, c = d.chain, b = d.buildup, p = d.participants;
  return (
    <div className="space-y-3">
      <Callout tone="warn">{d.not_a_signal}</Callout>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <Tile title={`Trend: ${t.trend} (${t.strength})`} ev={t.evidence}>
          {t.averages.map((a) => (
            <p key={a.days} className="num">{a.days}-day avg {n0(a.sma)} · price {a.distance_pct == null ? "—" : `${a.distance_pct > 0 ? "+" : ""}${a.distance_pct.toFixed(1)}%`} · slope {a.slope_pct_10d == null ? "—" : `${a.slope_pct_10d.toFixed(2)}%/10d`}</p>
          ))}
          <p className="mt-1 flex flex-wrap items-center gap-1">ADX {t.adx ? `${n2(t.adx.adx, 1)} (+DI ${n2(t.adx.plus_di, 1)} / −DI ${n2(t.adx.minus_di, 1)})` : "unknown (no stored high/low)"} <Ev e={t.adx_evidence} /></p>
          <p className="mt-1 text-[11px] text-muted">{t.note ?? t.source}</p>
        </Tile>
        <Tile title="Implied vs realised volatility" ev={v.evidence}>
          <p className="num">ATM IV {n2(v.atm_iv, 1)}% · realised 20d {n2(v.realised_20d, 1)}% · 60d {n2(v.realised_60d, 1)}%</p>
          <p className="num">IV − RV20: {v.iv_minus_rv20 == null ? "—" : `${v.iv_minus_rv20 > 0 ? "+" : ""}${v.iv_minus_rv20.toFixed(1)} pts`}</p>
          <p className="mt-1 flex flex-wrap items-center gap-1">IV rank {v.iv_rank == null ? "unknown" : `${(v.iv_rank * 100).toFixed(0)}`} · percentile {v.iv_percentile == null ? "unknown" : `${(v.iv_percentile * 100).toFixed(0)}`} <Ev e={v.rank_evidence} /></p>
          <p className="text-[11px] text-muted">{v.iv_status} ({v.iv_days} days, {v.iv_source})</p>
          <p className="mt-1 flex flex-wrap items-center gap-1">25-delta skew {n2(v.skew_25d, 2)} pts <Ev e={v.skew_evidence} /></p>
        </Tile>
        <Tile title="Term structure" ev={d.term.evidence}>
          {d.term.status === "unknown" ? <p className="text-muted">{d.term.note}</p> : (
            <>
              <p className="num">near {d.term.near_expiry ? day(d.term.near_expiry) : "—"}: {n2(d.term.near_iv, 1)}% · next {d.term.next_expiry ? day(d.term.next_expiry) : "—"}: {n2(d.term.next_iv, 1)}%</p>
              <p>{d.term.shape ?? "shape unknown"} <span className="text-[11px] text-muted">(as of {d.term.day})</span></p>
            </>
          )}
        </Tile>
        <Tile title="Put-call ratio and max pain" ev={c.pcr_evidence}>
          <p className="num">PCR (OI) {n2(c.pcr_oi)} · PCR (volume) {n2(c.pcr_volume)}</p>
          <p className="mt-1 flex flex-wrap items-center gap-1">Max pain {n0(c.max_pain)} (spot {n0(c.spot)}) <Ev e={c.max_pain_evidence} /></p>
          <p className="text-[11px] text-muted">{c.source} chain{c.as_of ? `, ${day(c.as_of)}` : ""}</p>
        </Tile>
        <Participants p={p} />
        <Tile title="Events before expiry" ev={d.events.evidence}>
          {d.events.items?.length ? d.events.items.map((x) => <p key={`${x.kind}${x.day}`}>{day(x.day)} · {x.title}</p>) : <p className="text-muted">unknown</p>}
          {d.events.rbi?.status === "no_source" && <p className="text-[11px] text-muted">{d.events.rbi.note}</p>}
        </Tile>
      </div>
      <Buildup b={b} />
    </div>
  );
}

const NET_KEYS = [["index_futures", "Index fut"], ["index_calls", "Index calls"], ["index_puts", "Index puts"]] as const;

function Participants({ p }: { p: MarketContext["participants"] }) {
  return (
    <Tile title="Participant-wise F&O OI (NSE)" ev={p.evidence}>
      {!p.rows ? <p className="text-muted">{p.note}</p> : (
        <>
          <table className="num w-full text-[11px]" aria-label="Net contracts by participant">
            <thead><tr className="text-muted"><th className="text-left font-normal">Net long</th>{NET_KEYS.map(([, l]) => <th key={l} className="text-right font-normal">{l}</th>)}</tr></thead>
            <tbody>
              {p.rows.map((r) => (
                <tr key={r.participant}>
                  <td>{r.participant}</td>
                  {NET_KEYS.map(([k]) => (
                    <td key={k} className="text-right" title={r.net_change ? `change ${signed(r.net_change[k])}` : "no previous day stored"}>
                      {signed(r.net[k])}{r.net_change && <span className={cx("ml-1", r.net_change[k] > 0 ? "text-gain" : r.net_change[k] < 0 ? "text-loss" : "text-muted")}>({signed(r.net_change[k])})</span>}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-[11px] text-muted">Contracts long − short, {p.day}{p.previous_day ? ` (change since ${p.previous_day})` : ""}. Options are contracts, not delta.</p>
        </>
      )}
    </Tile>
  );
}

function Buildup({ b }: { b: MarketContext["buildup"] }) {
  const o = b.options;
  return (
    <div className="rounded-lg border border-border p-3 text-xs">
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-1">
        <p className="font-semibold">Open-interest build-up</p>
        <Ev e={b.evidence} />
      </div>
      <p className="mb-2 text-[11px] text-muted">{b.method}</p>
      {b.futures ? (
        <div className="mb-2 flex flex-wrap gap-2">
          {b.futures.rows.map((r) => (
            <span key={r.expiry} className="num inline-flex items-center gap-1">Future {day(r.expiry)}: price {signed(r.price_change)} · OI {signed(r.oi_change)} <Cell c={{ ...r }} /></span>
          ))}
          <span className="text-[11px] text-muted">{b.futures.source}, {b.futures.day}</span>
        </div>
      ) : <p className="mb-2 text-muted">Futures: no stored bhavcopy for this underlying.</p>}
      {!o || o.status === "unknown" ? <p className="text-muted">Options: {o?.note ?? "unknown"}</p> : (
        <div className="max-h-64 overflow-auto">
          <table className="num w-full min-w-[22rem] text-[11px]" aria-label="Option build-up by strike">
            <thead className="sticky top-0 bg-card text-muted"><tr><th className="text-left font-normal">Call</th><th className="font-normal">Strike</th><th className="text-right font-normal">Put</th></tr></thead>
            <tbody>
              {o.rows.map((r) => (
                <tr key={r.strike} className="border-b border-border/40">
                  <td className="py-0.5"><Cell c={r.call} /></td>
                  <td className="text-center font-medium">{r.strike.toLocaleString("en-IN")}</td>
                  <td className="text-right"><Cell c={r.put} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-[11px] text-muted">{o.source}</p>
        </div>
      )}
    </div>
  );
}
