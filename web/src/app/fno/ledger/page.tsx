"use client";

import { Scale } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Fig } from "@/components/fno/fit";
import type { CardLedger, LedgerSection, OverrideGroup } from "@/components/fno/ledger-types";
import { pnlTone, rs } from "@/components/fno/paper-trade";
import { Card, ErrorNote, PageHeader, Skeleton, Stat, cx } from "@/components/ui";
import { api } from "@/lib/api";

const num = (x: number | null | undefined, d = 3) => (x == null ? "unknown" : x.toFixed(d));
const pct = (x: number | null | undefined, d = 0) => (x == null ? "unknown" : `${(x * 100).toFixed(d)}%`);
const money = (x: number | null | undefined) => (x == null ? "unknown" : rs(x));
const HALVES = [["after", "After registration"], ["before", "Before registration"], ["pooled", "Pooled"]] as const;
const GROUPS: [keyof CardLedger["overrides"]["pnl"], string][] = [
  ["traded_against", "Traded against the card"], ["traded_with", "Traded with an ENTER card"],
  ["skipped_enter", "Skipped an ENTER card"], ["skipped_other", "Skipped a WAIT, AVOID or no-edge card"],
];

function Section({ s }: { s: LedgerSection }) {
  const b = s.bss, c = s.cohort;
  return (
    <>
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Stat label="PoP skill vs risk-neutral" display={<Fig>{num(b.bss)}</Fig>}
          hint={`${b.n} paired independent events${b.lo == null ? "" : ` · 95% ${num(b.lo)} to ${num(b.hi)}`}`}
          help="Primary 1: 1 − Brier(real-world PoP) ÷ Brier(risk-neutral PoP) on the first card per underlying, expiry and structure. Above 0: the card's PoP beat the option market's own probability." />
        <Stat label="Cohort delta per set" display={<Fig className={pnlTone(c.delta)}>{money(c.delta)}</Fig>}
          hint={`t ${num(c.t, 2)} · p ${num(c.p, 3)} · ${c.clusters} expiries`}
          help="Primary 2: mean net P&L per set of ENTER or NO PROVEN EDGE cards minus WAIT or AVOID cards, with an expiry-clustered t." />
        <Stat label="Go cards (ENTER, no edge)" display={<Fig>{money(c.mean_pass)}</Fig>} hint={`${c.n_pass} independent events`} />
        <Stat label="Stop cards (WAIT, AVOID)" display={<Fig>{money(c.mean_fail)}</Fig>} hint={`${c.n_fail} events · ${c.unknown_class} unknown verdict`} />
      </div>
      <Card title="Each model" subtitle={`${s.events} resolved cards, ${s.independent} independent`}>
        <div className="overflow-x-auto"><table className="w-full text-sm">
          <thead><tr className="text-left text-muted"><th>Model</th><th>n</th><th>Brier</th><th>Log score</th><th>Mean p</th><th>Happened</th><th>Below p10</th><th>PIT (10 bins)</th></tr></thead>
          <tbody>{s.models.map((m) => (
            <tr key={m.model}><td>{m.label}</td><td className="num">{m.n}</td><td className="num">{num(m.brier)}</td><td className="num">{num(m.log)}</td>
              <td className="num">{pct(m.mean_p)}</td><td className="num">{pct(m.base_rate)}</td>
              <td className="num">{m.p10.n ? `${pct(m.p10.share)} of ${m.p10.n} · p ${num(m.p10.p)}` : "unknown (no dots)"}</td>
              <td className="num">{m.pit.n ? m.pit.bins.join(" ") : "unknown"}{m.pit.clipped ? ` · ${m.pit.clipped} outside` : ""}</td></tr>))}</tbody>
        </table></div>
      </Card>
    </>
  );
}

function groupLine(g: OverrideGroup) {
  return `${g.decisions} decisions · ${g.resolved} resolved, ${g.open} open, ${g.void} void, ${g.no_forecast} without a forecast`;
}

export default function CardLedgerPage() {
  const [v, setV] = useState<CardLedger | null>(null);
  const [half, setHalf] = useState<(typeof HALVES)[number][0]>("after");
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setV(await api<CardLedger>("/api/fno/card-ledger"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);
  const o = v?.overrides, p = v?.panel;

  return (
    <div className="space-y-6">
      <PageHeader icon={<Scale className="size-5" />} eyebrow="Research" title="F&O card ledger"
        description="Every logged F&O card scored as a forecast, by the rules registered before its outcomes were read: did the card's PoP beat the option market's, and did ENTER cards do better than WAIT and AVOID ones? Also your recorded overrides and a fixed probability panel logged every session. Local only; nothing is ever ordered." />
      <ErrorNote error={error} onRetry={load} />
      {!v ? (!error && <Skeleton className="h-40 rounded-xl" />) : (
        <>
          <ul className="space-y-1 text-sm">{v[half].claims.length ? v[half].claims.map((c) => (
            <li key={c.underlying}>{c.underlying}: {c.n_independent} of {v.claim_min} independent events · {c.claim_allowed ? "enough for a calibration claim" : "no calibration claim yet: every number below is descriptive"}</li>
          )) : <li>No resolved card yet: nothing is scored, and no calibration claim is made.</li>}</ul>
          <div role="tablist" className="flex flex-wrap gap-2 text-sm">{HALVES.map(([k, label]) => (
            <button key={k} role="tab" aria-selected={half === k} onClick={() => setHalf(k)}
              className={cx("rounded-lg border px-3 py-1", half === k ? "border-brand text-brand" : "border-border text-muted")}>{label}</button>))}</div>
          <p className="text-xs text-muted">Registered {v.registered}: cards logged before it are shown apart. Cards in the ledger: {Object.entries(v.cards).map(([k, n]) => `${n} ${k.replace("_", " ")}`).join(", ")}.</p>
          <Section s={v[half]} />
          <Card title="Your overrides" subtitle={`${o!.overrides} of ${o!.decisions} recorded decisions went against the card · rate ${pct(o!.rate)}${o!.unknown_class ? ` · ${o!.unknown_class} with an unknown verdict` : ""}`}
            help="Recorded only when you mark a card traded or skipped on the card itself; never inferred from your trades. P&L is the card's simulated net P&L per set, not your fills.">
            <ul className="space-y-1 text-sm">{GROUPS.map(([k, label]) => (
              <li key={k}>{label}: <Fig className={pnlTone(o!.pnl[k].mean_pnl)}>{money(o!.pnl[k].mean_pnl)}</Fig> per set · <span className="text-muted">{groupLine(o!.pnl[k])}</span></li>))}</ul>
            {o!.recent.length > 0 && <p className="mt-2 text-xs text-muted">Latest {o!.recent.length} of {o!.rows}: {o!.recent.slice(0, 5).map((r) => `card ${r.card_log_id}#${r.card_index} ${r.decision} (${r.verdict ?? "unknown verdict"})`).join(" · ")}</p>}
          </Card>
          <Card title="Fixed probability panel" subtitle={p!.rows ? `${p!.sessions} sessions logged, ${p!.first_session} to ${p!.last_session}` : `Logged from ${p!.start}: no session yet`}
            help="NIFTY and BANKNIFTY, near and monthly expiry, every stored session: P(S_T beyond the 10/16/25-delta strikes), the ±1σ band and each registered recipe's PoP, under each model. The first session per expiry is the scored event.">
            {p!.groups.length ? (
              <div className="overflow-x-auto"><table className="w-full text-sm">
                <thead><tr className="text-left text-muted"><th>Underlying</th><th>Expiry</th><th>Event</th><th>Model</th><th>n</th><th>Brier</th><th>Mean p</th><th>Happened</th><th>Waiting</th></tr></thead>
                <tbody>{p!.groups.map((g) => (
                  <tr key={`${g.underlying}:${g.expiry_kind}:${g.event}:${g.model}`}><td>{g.underlying}</td><td>{g.expiry_kind}</td><td>{g.event}</td><td>{g.model}</td>
                    <td className="num">{g.n}</td><td className="num">{num(g.brier)}</td><td className="num">{pct(g.mean_p)}</td><td className="num">{pct(g.base_rate)}</td>
                    <td className="num">{g.open} open · {g.void} void</td></tr>))}</tbody>
              </table></div>
            ) : <p className="text-sm text-muted">Nothing logged yet: the monitor logs the panel after each session&apos;s bhavcopy is stored.</p>}
          </Card>
          <p className="text-xs text-muted">Rules: {v.prereg} and {v.addendum}.</p>
        </>
      )}
      <p className="text-xs text-muted"><Link className="text-brand hover:underline" href="/fno/review">Monthly review →</Link></p>
    </div>
  );
}
