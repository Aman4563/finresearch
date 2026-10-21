"use client";

import { Compass, Loader2, NotebookPen, Scale } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Badge, Button, Callout, Card, ErrorNote, InfoTip, Segmented, Skeleton, type Tone, cx } from "@/components/ui";
import { api } from "@/lib/api";

import { CalibrationChart, type RelBin } from "./direction-calibration";
import { PlanCard } from "./plan-card";
import type { SuggestResponse, TradePlan } from "./plan-types";

type EventKey = "up" | "up_k" | "down_k";
type EventProb = {
  implied: number | null; base_rate: number | null; logistic: number | null; shown: number | null; shown_model: string;
  status: "accepted" | "no_edge" | "not_evaluated"; label: string; train_n: number; text: string;
};
type Horizon = { h: number; sigma_pct: number | null; threshold_pct: number | null; events: Record<EventKey, EventProb> };
type ModelStats = { n: number; base_rate: number | null; brier: number | null; log_loss: number | null; hit_rate: number | null; auc: number | null;
  calibration_slope: number | null; reliability: RelBin[] };
type Test = { brier_skill: number | null; log_loss_skill: number | null; dm: number | null; p: number | null; p_holm?: number | null; lag: number };
type Window = { n: number; from: string | null; to: string | null; models: Record<"base_rate" | "implied" | "logistic", ModelStats>; tests: Record<string, Test> };
type RunCell = { symbol: string; horizon: number; event: EventKey; status?: string; reasons?: string[]; is: Window; oos: Window | null };
type Candidate = { key: string; label: string; view: "up" | "down"; status: "passes" | "fails" | "unavailable"; why: string[];
  ev_model?: number; ev_implied?: number; edge?: number; costs?: number; max_loss?: number | null; pop_model?: number;
  /** #356: EV with each leg at its own IV (≈ −costs) and what the ATM-IV market EV adds to it: a pricing residual, not edge */
  ev_fair?: number | null; pricing_residual?: number | null;
  legs?: { right: string; strike: number; side: string; price: number; iv: number }[] };
type Decision = { status: "ok" | "unavailable"; why?: string; action?: "TRADE" | "NO TRADE"; label?: string; chosen?: Candidate | null;
  candidates?: Candidate[]; horizon?: number; expiry?: string; horizon_day?: string; held_to_expiry?: boolean; lot_size?: number;
  plan?: TradePlan | null; notes?: string[]; probabilities_as_of?: string; method?: string;
  non_directional?: { text: string; suggest_api: string; href: string; evidence_href: string } };
type LiveRow = { method: string; event_kind: string; h: number; event: EventKey; model: string; logged: number; open: number; resolved: number;
  independent: number; void: number; summary: { n: number; brier: number | null; brier_skill: number | null; hit_rate: number | null; hit_rate_ci?: [number, number] | null; base_rate: number | null } };
type DirectionResponse = {
  symbol: string; as_of: string; spot: number | null; iv_30: number | null; horizons: Horizon[]; accepted: string[];
  model_status: "accepted" | "no_edge"; headline: { h: number; p_up: number | null; label: string; call_or_put: "CALL" | "PUT" | null };
  run: null | { id: number; created_at: string; verdict: string; source: string; registration_hash: string; windows: { oos_start: string };
    criteria: { min_oos_n: number; alpha: number; slope_low: number; slope_high: number }; cells: RunCell[] };
  live_accuracy: LiveRow[]; decision: Decision | null; decision_horizon: number; evidence_note: string;
  registration: { hash: string; registered_on: string; matches_spec: boolean; matches_code?: boolean; hash_covers: string; doc: string };
};

const EVENTS: EventKey[] = ["up", "up_k", "down_k"];
const EV_LABEL: Record<EventKey, string> = { up: "P(up)", up_k: "P(up > ½σ)", down_k: "P(down > ½σ)" };
const pct = (x: number | null | undefined, d = 0) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const num = (x: number | null | undefined, d = 3) => (x == null ? "—" : x.toFixed(d));
const inr = (x: number | null | undefined) => (x == null ? "—" : `${x < 0 ? "−" : ""}₹${Math.abs(Math.round(x)).toLocaleString("en-IN")}`);
const STATUS: Record<string, { tone: Tone; text: string }> = {
  accepted: { tone: "gain", text: "accepted out of sample" },
  no_edge: { tone: "warn", text: "no edge" },
  not_evaluated: { tone: "neutral", text: "not evaluated" },
};

/** The CALL / PUT gauge for one horizon: the model's (or the market's) split of the move into down > ½σ, small move,
 * up > ½σ, with P(up) marked. */
function Gauge({ hz }: { hz: Horizon }) {
  const e = hz.events;
  const dk = e.down_k.shown ?? 0, uk = e.up_k.shown ?? 0, up = e.up.shown ?? 0.5;
  const mid = Math.max(0, 1 - dk - uk);
  return (
    <div className="space-y-1.5">
      <div className="relative flex h-6 w-full overflow-hidden rounded-md text-[10px] font-medium" role="img"
        aria-label={`Down more than half sigma ${pct(dk)}, smaller move ${pct(mid)}, up more than half sigma ${pct(uk)}; P(up) ${pct(up)}`}>
        <div className="flex items-center justify-center bg-loss-soft text-loss" style={{ width: `${dk * 100}%` }}>{dk > 0.08 ? pct(dk) : ""}</div>
        <div className="flex items-center justify-center bg-background-subtle text-muted" style={{ width: `${mid * 100}%` }}>{mid > 0.12 ? `small move ${pct(mid)}` : ""}</div>
        <div className="flex items-center justify-center bg-gain-soft text-gain" style={{ width: `${uk * 100}%` }}>{uk > 0.08 ? pct(uk) : ""}</div>
        {/* P(up) marker: notches at the top and bottom edges, so it never covers the labels */}
        <div className="absolute top-0 h-1.5 w-0.5 bg-foreground" style={{ left: `calc(${(1 - up) * 100}% - 1px)` }} />
        <div className="absolute bottom-0 h-1.5 w-0.5 bg-foreground" style={{ left: `calc(${(1 - up) * 100}% - 1px)` }} title={`P(up) ${pct(up, 1)}: the marker splits down (left) from up (right)`} />
      </div>
      <div className="flex justify-between text-[11px] text-muted">
        <span>PUT side: P(down) {pct(1 - up, 1)}</span>
        <span>CALL side: P(up) {pct(up, 1)}</span>
      </div>
    </div>
  );
}

function Probabilities({ hz }: { hz: Horizon }) {
  return (
    <div tabIndex={0} role="region" aria-label="Probabilities by event, scrollable" className="-mx-1 overflow-x-auto">
      <table className="num w-full min-w-[520px] text-xs [&_td]:px-1.5 [&_td]:py-1 [&_th]:px-1.5 [&_th]:py-1 [&_th]:text-left [&_th]:font-medium [&_th]:text-muted [&_th]:whitespace-nowrap [&_tr>*:first-child]:sticky [&_tr>*:first-child]:left-0 [&_tr>*:first-child]:z-[1] [&_tr>*:first-child]:bg-card">
        <thead><tr><th>Event ({hz.h} session{hz.h > 1 ? "s" : ""})</th><th>Shown</th><th>Market-implied</th><th>Base rate</th><th>Logistic</th><th>Status</th></tr></thead>
        <tbody>
          {EVENTS.map((k) => {
            const v = hz.events[k];
            const st = STATUS[v.status] ?? STATUS.no_edge;
            return (
              <tr key={k} className="border-t border-border">
                <td><span className="font-medium">{EV_LABEL[k]}</span> <InfoTip label="The event">{v.text}{k !== "up" && hz.threshold_pct != null ? ` (±${hz.threshold_pct.toFixed(2)}% now)` : ""}</InfoTip></td>
                <td className="font-semibold">{pct(v.shown, 1)}</td>
                <td>{pct(v.implied, 1)}</td>
                <td>{pct(v.base_rate, 1)}</td>
                <td className={cx(v.status !== "accepted" && "text-muted")}>{pct(v.logistic, 1)}{v.status !== "accepted" && v.logistic != null ? " (shadow)" : ""}</td>
                <td><Badge tone={st.tone}>{st.text}</Badge></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function OosTable({ cells, crit }: { cells: RunCell[]; crit: NonNullable<DirectionResponse["run"]>["criteria"] }) {
  return (
    <div tabIndex={0} role="region" aria-label="Out-of-sample accuracy, scrollable" className="-mx-1 overflow-x-auto">
      <table className="num w-full min-w-[640px] text-xs [&_td]:px-1.5 [&_td]:py-1 [&_th]:px-1.5 [&_th]:py-1 [&_th]:text-left [&_th]:font-medium [&_th]:text-muted [&_th]:whitespace-nowrap [&_tr>*:first-child]:sticky [&_tr>*:first-child]:left-0 [&_tr>*:first-child]:z-[1] [&_tr>*:first-child]:bg-card">
        <thead>
          <tr><th>Event</th><th>Days</th><th>Happened</th><th>Hit rate</th><th>AUC</th>
            <th>Brier skill vs market <InfoTip label="Brier skill">1 − Brier(model) ÷ Brier(reference) on the same days. Above 0 = better. p is one-sided Diebold-Mariano, Holm-adjusted over 24 cells.</InfoTip></th>
            <th>vs base rate</th><th>Calib. slope</th><th>Verdict</th></tr>
        </thead>
        <tbody>
          {cells.map((c) => {
            const o = c.oos;
            if (!o) return null;
            const li = o.tests.logistic_vs_implied, lb = o.tests.logistic_vs_base_rate, m = o.models.logistic;
            return (
              <tr key={c.event} className="border-t border-border align-top">
                <td className="font-medium">{EV_LABEL[c.event]}</td>
                <td>{o.n}</td>
                <td>{pct(m.base_rate, 1)}</td>
                <td>{pct(m.hit_rate, 1)}</td>
                <td>{num(m.auc, 2)}</td>
                <td className={cx((li.brier_skill ?? 0) > 0 ? "text-gain" : "text-loss")}>{num(li.brier_skill, 4)} <span className="text-muted">p {num(li.p_holm ?? li.p, 2)}</span></td>
                <td className={cx((lb.brier_skill ?? 0) > 0 ? "text-gain" : "text-loss")}>{num(lb.brier_skill, 4)} <span className="text-muted">p {num(lb.p_holm ?? lb.p, 2)}</span></td>
                <td>{num(m.calibration_slope, 2)}</td>
                <td><Badge tone={c.status === "accepted" ? "gain" : "warn"}>{c.status === "accepted" ? "accepted" : "no edge"}</Badge>
                  {c.reasons && c.status !== "accepted" && <InfoTip label="Why">{c.reasons.join("; ")}</InfoTip>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-1 text-[11px] text-muted">
        Accepted needs ≥ {crit.min_oos_n} days, Brier skill &gt; 0 against both the market and the base rate with Holm-adjusted p &lt; {crit.alpha},
        and a calibration slope of {crit.slope_low}–{crit.slope_high}. Hit rate is the share of days where the forecast was on the right side of 50%;
        for the ½σ events most forecasts are below 50%, so it mostly counts quiet days.
      </p>
    </div>
  );
}

function LiveTable({ rows }: { rows: LiveRow[] }) {
  if (!rows.length) return <p className="text-xs text-muted">No live forecast logged yet for this horizon. The nightly job logs one per event after the bhavcopy loads; you can also log now.</p>;
  return (
    <div tabIndex={0} role="region" aria-label="Live accuracy, scrollable" className="-mx-1 overflow-x-auto">
      <table className="num w-full min-w-[560px] text-xs [&_td]:px-1.5 [&_td]:py-1 [&_th]:px-1.5 [&_th]:py-1 [&_th]:text-left [&_th]:font-medium [&_th]:text-muted [&_th]:whitespace-nowrap [&_tr>*:first-child]:sticky [&_tr>*:first-child]:left-0 [&_tr>*:first-child]:z-[1] [&_tr>*:first-child]:bg-card">
        <thead><tr><th>Event</th><th>Model</th><th>Logged</th><th>Open</th><th>Resolved</th><th>Independent <InfoTip label="Independent events">Non-overlapping windows: overlapping h-session windows share most of their path, so only these count as separate outcomes.</InfoTip></th><th>Hit rate <InfoTip label="Hit rate interval">Wilson 95% interval over the independent events: the range the true rate plausibly lies in.</InfoTip></th><th>Brier</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.event_kind}-${r.method}`} className="border-t border-border">
              <td className="font-medium">{EV_LABEL[r.event] ?? r.event}</td>
              <td>{r.model === "logistic" ? "logistic" : "market-implied"}</td>
              <td>{r.logged}</td><td>{r.open}</td><td>{r.resolved}</td><td>{r.independent}</td>
              {/* a hit rate on a handful of independent events means little without its interval (#354 h) */}
              <td className="whitespace-nowrap">{r.summary.n ? `${pct(r.summary.hit_rate, 0)}${r.summary.hit_rate_ci ? ` (95% CI ${pct(r.summary.hit_rate_ci[0], 0).replace("%", "")}–${pct(r.summary.hit_rate_ci[1], 0)})` : ""}` : "—"}</td>
              <td>{num(r.summary.brier, 3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Candidates({ rows }: { rows: Candidate[] }) {
  return (
    <div tabIndex={0} role="region" aria-label="Trade candidates, scrollable" className="-mx-1 overflow-x-auto">
      <table className="num w-full min-w-[600px] text-xs [&_td]:px-1.5 [&_td]:py-1 [&_th]:px-1.5 [&_th]:py-1 [&_th]:text-left [&_th]:font-medium [&_th]:text-muted [&_th]:whitespace-nowrap [&_tr>*:first-child]:sticky [&_tr>*:first-child]:left-0 [&_tr>*:first-child]:z-[1] [&_tr>*:first-child]:bg-card">
        <thead><tr><th>Trade (1 lot set)</th><th>EV, model</th><th>EV, market</th><th>Edge</th><th>Costs</th><th>Max loss</th><th>Gate</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-t border-border align-top">
              <td><span className="font-medium">{r.label}</span>{r.legs && <span className="block text-[11px] text-muted">{r.legs.map((l) => `${l.side} ${l.right} ${l.strike} @ ${l.price}`).join(", ")}</span>}</td>
              {/* #356: green only for a trade that passed the registered gate (an accepted model); otherwise a positive
                  model EV is the pricing residual of the market's own probabilities, not edge */}
              <td data-testid="direction-ev-model" className={cx(r.status === "passes" ? "text-gain" : (r.ev_model ?? 0) < 0 ? "text-loss" : "")}>{inr(r.ev_model)}</td>
              <td>{inr(r.ev_implied)}{r.pricing_residual != null && <span className="block text-[11px] text-muted">incl. pricing residual {inr(r.pricing_residual)} (not edge)</span>}</td>
              <td>{inr(r.edge)}</td>
              <td>{inr(r.costs)}</td>
              <td>{inr(r.max_loss)}</td>
              <td>{r.status === "passes" ? <Badge tone="gain">passes</Badge> : <span className="inline-flex items-center gap-1"><Badge tone="neutral">{r.status === "unavailable" ? "n/a" : "fails"}</Badge>{r.why.length > 0 && <InfoTip label="Why not">{r.why.join("; ")}</InfoTip>}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const RANK: Record<string, number> = { ENTER: 0, PAPER: 1, WAIT: 2, AVOID: 3 };

function NonDirectional({ symbol, nd }: { symbol: string; nd: NonNullable<Decision["non_directional"]> }) {
  const [card, setCard] = useState<TradePlan | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = async () => {
    setBusy(true);
    try {
      const r = await api<SuggestResponse>(`/api/fno/${encodeURIComponent(symbol)}/suggest`);
      const nondir = r.cards.filter((c) => c.kind === "iron_condor" && c.legs.length > 0).sort((a, b) => (RANK[a.action] ?? 3) - (RANK[b.action] ?? 3));
      setCard(nondir[0] ?? null);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted">{nd.text} <Link className="text-brand underline-offset-2 hover:underline" href={nd.evidence_href}>Backtest evidence</Link></p>
      <Button variant="ghost" onClick={load} disabled={busy} icon={busy ? <Loader2 className="size-3.5 animate-spin" /> : <Scale className="size-3.5" />}>
        Best non-directional plan
      </Button>
      <ErrorNote error={error} onRetry={load} />
      {card === null && <p className="text-xs text-muted">No iron-condor recipe can be built on today&apos;s chain; see Suggest setups below.</p>}
      {card && <PlanCard plan={card} />}
    </div>
  );
}

/** CALL or PUT? (#327) The pre-registered direction model's probabilities with their out-of-sample and live accuracy,
 * and the trade it supports, or NO TRADE when no model passed. */
export function DirectionPanel({ symbol }: { symbol: string }) {
  const [h, setH] = useState<number | null>(null);
  const [data, setData] = useState<DirectionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [calEvent, setCalEvent] = useState<EventKey>("up");
  const [logMsg, setLogMsg] = useState<string | null>(null);
  const [logging, setLogging] = useState(false);
  const load = useCallback(async (hz: number | null) => {
    try {
      setData(await api<DirectionResponse>(`/api/fno/direction/${encodeURIComponent(symbol)}${hz ? `?horizon=${hz}` : ""}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [symbol]);
  useEffect(() => {
    const t = setTimeout(() => load(h), 0);
    return () => clearTimeout(t);
  }, [load, h]);
  const logNow = async () => {
    setLogging(true);
    try {
      const r = await api<{ logged: unknown[]; skipped: string | null; as_of: string }>(`/api/fno/direction/${encodeURIComponent(symbol)}/log`, { method: "POST" });
      setLogMsg(r.skipped ? `Not logged: ${r.skipped}` : `Logged ${r.logged.length} forecasts from the ${r.as_of} close.`);
      load(h);
    } catch (e) {
      setLogMsg((e as Error).message);
    } finally {
      setLogging(false);
    }
  };
  const subtitle = `${symbol}: the probability of an up or down move, how accurate it has been, and whether any trade has an edge`;
  if (error) return <Card title="CALL or PUT?" icon={<Compass className="size-4" />} subtitle={subtitle}><ErrorNote error={error} onRetry={() => load(h)} /></Card>;
  if (!data) return <Card title="CALL or PUT?" icon={<Compass className="size-4" />} subtitle={subtitle}><Skeleton className="h-56 rounded-xl" /></Card>;
  const hz = data.horizons.find((x) => x.h === data.decision_horizon) ?? data.horizons[0];
  const cells = (data.run?.cells ?? []).filter((c) => c.horizon === hz.h);
  const cal = cells.find((c) => c.event === calEvent)?.oos;
  const live = data.live_accuracy.filter((r) => r.h === hz.h);
  const dec = data.decision;
  const accepted = data.model_status === "accepted";
  return (
    <Card title="CALL or PUT?" icon={<Compass className="size-4" />} subtitle={subtitle}
      actions={<Button variant="ghost" onClick={logNow} disabled={logging} icon={logging ? <Loader2 className="size-3.5 animate-spin" /> : <NotebookPen className="size-3.5" />}>Log today&apos;s forecast</Button>}>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge tone={accepted ? "gain" : "warn"}>{accepted ? `model accepted: ${data.accepted.join(", ")}` : "no edge: showing market-implied probabilities"}</Badge>
          <span className="text-muted">as of the {data.as_of} close · IV30 {data.iv_30 == null ? "—" : `${data.iv_30.toFixed(1)}%`}</span>
          <span className="ml-auto"><Segmented label="Horizon" value={String(hz.h)} onChange={(v) => setH(Number(v))}
            options={data.horizons.map((x) => ({ value: String(x.h), label: `${x.h}d` }))} /></span>
        </div>
        {logMsg && <Callout tone="info">{logMsg}</Callout>}
        <Gauge hz={hz} />
        <Probabilities hz={hz} />

        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Decision ({hz.h} sessions)</h3>
          {!dec ? null : dec.status !== "ok" ? (
            <Callout tone="warn">Decision unavailable: {dec.why}</Callout>
          ) : (
            <>
              <Callout tone={dec.action === "TRADE" ? "gain" : "warn"} title={dec.label}>
                {dec.action === "TRADE"
                  ? `Expected value after costs under the accepted model's probabilities, exit ${dec.held_to_expiry ? "at expiry" : `on ${dec.horizon_day}`} (expiry ${dec.expiry}). Evidence, not a guarantee.`
                  : accepted
                    ? "No candidate clears the pre-registered gate (EV above the market's and above the margin)."
                    : "No model passed the pre-registered out-of-sample test, so the probabilities are the market's own: at fair prices every trade's expected value is minus its costs."}
              </Callout>
              {dec.candidates && <Candidates rows={dec.candidates} />}
              {dec.plan && <PlanCard plan={dec.plan} />}
              {dec.non_directional && <NonDirectional symbol={symbol} nd={dec.non_directional} />}
              {dec.notes && dec.notes.length > 0 && <p className="text-[11px] text-muted">{dec.notes.join(" ")}</p>}
            </>
          )}
        </section>

        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Out-of-sample accuracy ({hz.h} sessions)</h3>
          {!data.run ? (
            <Callout tone="info">No evaluation is stored yet: run <code>finresearch fno direction run</code>. Until then every cell counts as no edge.</Callout>
          ) : (
            <>
              <p className="text-xs text-muted">Walk-forward from {data.run.windows.oos_start}, monthly refits, on {data.run.source}: {data.run.verdict}.</p>
              <OosTable cells={cells} crit={data.run.criteria} />
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted">Calibration of</span>
                <Segmented label="Event" value={calEvent} onChange={(v) => setCalEvent(v as EventKey)} options={EVENTS.map((k) => ({ value: k, label: EV_LABEL[k] }))} />
              </div>
              {cal ? (
                <CalibrationChart series={[
                  { name: "Logistic", color: "var(--chart-1)", bins: cal.models.logistic.reliability },
                  { name: "Market-implied", color: "var(--chart-2)", bins: cal.models.implied.reliability },
                ]} />
              ) : <p className="text-xs text-muted">No out-of-sample bins for this event.</p>}
            </>
          )}
        </section>

        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Live accuracy ({hz.h} sessions)</h3>
          <LiveTable rows={live} />
        </section>

        <p className="text-[11px] text-muted">
          {data.evidence_note} Registration {data.registration.hash.slice(0, 12)} ({data.registration.registered_on}
          {data.registration.matches_spec ? ", matches spec" : ", SPEC CHANGED SINCE"}; the hash covers {data.registration.hash_covers}).
        </p>
      </div>
    </Card>
  );
}
