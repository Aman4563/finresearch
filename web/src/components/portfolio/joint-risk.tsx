"use client";

// One risk view across the investment portfolio and the F&O book (#397): D_total (the NIFTY-equivalent ₹ delta of
// both), L(m) with F&O's share, single-day historical replays and a pre-trade check. Loaded only on request: the first
// run prices the open book on today's chains and reads each holding's prices on the replay days. Anything unknown (a
// holding with no beta, an unpriced leg) leaves the combined figure unknown with what is missing, never 0.

import { Combine } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote, Table } from "@/components/ui";
import { day, useApi } from "@/lib/api";

import type { JointRisk, Loss } from "./joint-risk-types";
import { JointPretrade } from "./joint-pretrade";
import { Loading, pct, spct } from "./shared-analytics";
import { inr, signed } from "./types";

export function Reasons({ items, label }: { items: string[]; label: string }) {
  if (!items.length) return null;
  return (
    <ul aria-label={label} className="list-disc space-y-0.5 pl-5 text-xs text-warn">
      {[...new Set(items)].map((r) => <li key={r}>{r}</li>)}
    </ul>
  );
}

function Share({ x }: { x: Loss }) {
  return x.fno_share == null ? <span className="text-muted">—</span> : <>{pct(x.fno_share)}</>;
}

function Body({ d }: { d: JointRisk }) {
  const missing = [...d.d_total.reasons, ...d.losses.flatMap((x) => x.reasons)];
  const ht = d.hedge_trigger;
  return (
    <div className="space-y-3">
      <Table label="NIFTY-equivalent delta">
        <thead><tr><th>Book</th><th className="text-right">₹ per 1 % NIFTY move</th></tr></thead>
        <tbody>
          <tr><td>Holdings (β × value)</td><td className="num text-right">{signed(d.d_total.per_pct.holdings)}</td></tr>
          <tr><td>F&amp;O book ({d.book.known ? `${d.book.legs} open legs` : "unknown"})</td><td className="num text-right">{signed(d.d_total.per_pct.fno)}</td></tr>
          <tr className="font-medium"><td>Both (D_total)</td><td className="num text-right">{d.d_total.total == null ? "unknown" : signed(d.d_total.per_pct.total)}</td></tr>
        </tbody>
      </Table>
      <Table label="Joint stress L(m)">
        <thead><tr><th>NIFTY move</th><th className="text-right">Holdings</th><th className="text-right">F&amp;O</th><th className="text-right">Both</th><th className="text-right">F&amp;O share of loss</th></tr></thead>
        <tbody>{d.losses.map((x) => (
          <tr key={x.move}><td className="num">{spct(x.move, 0)}</td><td className="num text-right">{signed(x.holdings)}</td>
            <td className="num text-right">{signed(x.fno)}</td><td className="num text-right font-medium">{x.total == null ? "unknown" : signed(x.total)}</td>
            <td className="num text-right"><Share x={x} /></td></tr>
        ))}</tbody>
      </Table>
      <Reasons items={missing} label="What is missing" />
      <p className="text-xs text-muted">
        {ht.fired == null ? <Badge tone="neutral">hedge trigger unknown</Badge> : ht.fired ? <Badge tone="warn">F&amp;O ≥ {pct(ht.threshold, 0)} of the −10 % loss</Badge> : <Badge tone="neutral">F&amp;O below {pct(ht.threshold, 0)} of the −10 % loss</Badge>}{" "}
        {ht.share != null && <>F&amp;O is {pct(ht.share)} of L(−10 %). </>}{ht.note}
      </p>
      <Table label="Historical days on both books">
        <thead><tr><th>Day</th><th className="text-right">NIFTY</th><th className="text-right">IV30</th><th className="text-right">Holdings</th><th className="text-right">F&amp;O</th><th className="text-right">Both</th></tr></thead>
        <tbody>{d.replays.map((r) => (
          <tr key={r.day}><td>{r.label}</td><td className="num text-right">{spct(r.nifty_move)}</td><td className="num text-right">+{r.iv30_change.toFixed(1)}</td>
            <td className="num text-right">{signed(r.holdings)}</td><td className="num text-right">{signed(r.fno)}</td>
            <td className="num text-right font-medium" title={r.reasons.join("; ")}>{r.total == null ? "unknown" : signed(r.total)}</td></tr>
        ))}</tbody>
      </Table>
      <p className="text-xs text-muted">Forward test (pre-registered): {d.forward_test.status}, since {day(d.forward_test.since)}.</p>
      {d.notes.map((n) => <p key={n} className="text-xs text-muted">{n}</p>)}
      <p className="text-[11px] text-muted">{d.method} {d.beta_note} {d.holdings_reason ? "" : `Holdings valued at ${inr(d.holdings.reduce((a, h) => a + h.value, 0))}. `}{d.privacy}</p>
    </div>
  );
}

export function JointRiskCard({ refresh }: { refresh: number }) {
  const [run, setRun] = useState(false);
  const [beta1, setBeta1] = useState(false);
  const q = `?assume_beta1=${beta1}&r=${refresh}`;
  const { data, error, reload } = useApi<JointRisk>(run ? `/api/portfolio/analytics/joint-risk${q}` : null);
  return (
    <Card title="Both books together" icon={<Combine className="size-4" />}
      help="Your holdings and your F&O book under the same NIFTY move: the NIFTY-equivalent delta of both (D_total), the joint loss at each move with how much of it comes from F&O, and a few historical days. Short index puts on top of long equity add to the same fall. Analysis only, not advice.">
      {!run ? (
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          <p className="flex-1">Prices the open F&amp;O book on today&apos;s chains and reads each holding&apos;s prices on the replay days; the first run can take a minute.</p>
          <Button variant="secondary" onClick={() => setRun(true)}>Combine both books</Button>
        </div>
      ) : error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <Loading what="the joint view" /> : (
        <div className="space-y-3">
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={beta1} onChange={(e) => setBeta1(e.target.checked)} />
            Assume beta 1 for equity with under a year of prices (labelled on each holding)
          </label>
          <Body d={data} />
          <JointPretrade beta1={beta1} />
        </div>
      )}
    </Card>
  );
}
