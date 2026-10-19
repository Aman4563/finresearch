"use client";

import { ClipboardList, ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import Link from "next/link";

import { fmtINR } from "@/components/charts";
import { Badge, Callout, InfoTip, type Tone, cx } from "@/components/ui";
import { day } from "@/lib/api";

import { Fig, Parts } from "./fit";
import { PlanActions } from "./plan-actions";
import type { ExitLevel, PlanAction, TradePlan } from "./plan-types";

const VERDICT: Record<PlanAction, string> = { ENTER: "bg-gain-soft text-gain ring-gain/30", WAIT: "bg-warn-soft text-warn ring-warn/30", AVOID: "bg-loss-soft text-loss ring-loss/30" };
const ICON = { ENTER: ShieldCheck, WAIT: ShieldQuestion, AVOID: ShieldAlert } as const;
const rs = (x: number | null | undefined) => (x == null ? "unknown" : `${x < 0 ? "−" : ""}${fmtINR(Math.abs(Math.round(x)))}`);
const lvl = (x: number | null | undefined) => (x == null ? "—" : Math.round(x).toLocaleString("en-IN"));
const pct = (x: number | null | undefined) => (x == null ? "unknown" : `${(x * 100).toFixed(0)}%`);
const px = (x: number | null | undefined) => (x == null ? "—" : `₹${x.toFixed(2)}`);

function Row({ label, children, help }: { label: string; children: React.ReactNode; help?: string }) {
  // label above the value on a phone, beside it from sm up; a value that is a sentence reads left to right
  return (
    <div className="grid gap-x-3 gap-y-0.5 border-b border-border/50 py-1.5 text-xs last:border-0 sm:grid-cols-[8.5rem_minmax(0,1fr)]">
      <span className="flex items-center gap-1 text-muted">{label}{help && <InfoTip>{help}</InfoTip>}</span>
      <span className="min-w-0 font-medium">{children}</span>
    </div>
  );
}

const N = ({ children }: { children: React.ReactNode }) => <span className="num whitespace-nowrap">{children}</span>;

function Level({ x, credit, what }: { x: ExitLevel | null | undefined; credit: boolean; what: "stop" | "target" }) {
  if (!x) return <span className="text-muted">none for this position</span>;
  const now = [x.underlying_now.below, x.underlying_now.above].filter((v) => v != null).map(lvl).join(" / ");
  const exp = [x.underlying_at_expiry.below, x.underlying_at_expiry.above].filter((v) => v != null).map(lvl).join(" / ");
  return (
    <span>
      <N>{what === "stop" ? "−" : "+"}{rs(x.rupees)}</N>
      {x.premium_per_share != null && <> · <span className="whitespace-nowrap">{credit ? "buy back at" : "value"} <N>{px(x.premium_per_share)}</N>/share</span></>}
      {" · "}<span className="whitespace-nowrap">underlying <N>{now || (what === "target" ? "by time decay, not a price level" : "—")}</N></span>
      {exp && <span className="whitespace-nowrap text-muted"> (at expiry <N>{exp}</N>)</span>}
    </span>
  );
}

/** One key number of the plan: label, the figure (never split, shrunk to fit) and an optional note under it. */
function Key({ label, value, note, tone, help }: { label: string; value: string; note?: React.ReactNode; tone?: "gain" | "loss"; help?: string }) {
  return (
    <div className="min-w-0 rounded-lg bg-background-subtle/70 px-2.5 py-2">
      <p className="flex items-center gap-1 text-[11px] text-muted">{label}{help && <InfoTip>{help}</InfoTip>}</p>
      <p className={cx("mt-0.5 text-sm font-semibold", tone === "gain" ? "text-gain" : tone === "loss" ? "text-loss" : "")}><Fig title={value}>{value}</Fig></p>
      {note && <p className="mt-0.5 text-[11px] leading-snug text-muted">{note}</p>}
    </div>
  );
}

/** Max loss, max profit, chance of profit, stop, target and breakevens at a glance (#326). */
function KeyNumbers({ plan }: { plan: TradePlan }) {
  const ex = plan.exits, p = plan.probability;
  if (plan.max_loss === undefined && plan.max_profit === undefined && !p && !ex) return null;
  return (
    <div data-testid="plan-key-numbers" className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
      <Key label="Max loss" value={plan.max_loss == null ? "unlimited" : rs(plan.max_loss)} tone="loss" />
      <Key label="Max profit" value={plan.max_profit == null ? "unlimited" : rs(plan.max_profit)} tone="gain" />
      <Key label="Chance of profit" value={pct(p?.real_world)} note={`real-world · risk-neutral ${pct(p?.risk_neutral)}`} help={p?.note} />
      <Key label="Stop" value={ex?.stop ? `−${rs(ex.stop.rupees)}` : "none"} tone={ex?.stop ? "loss" : undefined}
        note={ex?.stop?.underlying_now.below != null || ex?.stop?.underlying_now.above != null ? `underlying ${[ex!.stop!.underlying_now.below, ex!.stop!.underlying_now.above].filter((v) => v != null).map(lvl).join(" / ")}` : undefined} />
      <Key label="Target" value={ex?.target ? `+${rs(ex.target.rupees)}` : "none"} tone={ex?.target ? "gain" : undefined}
        note={ex?.time ? `or exit by ${day(ex.time.by)}` : undefined} />
      <div className="min-w-0 rounded-lg bg-background-subtle/70 px-2.5 py-2">
        <p className="text-[11px] text-muted">Breakevens</p>
        {/* several levels wrap between the levels, never inside one */}
        <p className="num mt-0.5 text-sm font-semibold">{plan.breakevens?.length ? <Parts parts={plan.breakevens.map(lvl)} sep=" /" /> : "none"}</p>
      </div>
    </div>
  );
}

/** One trade plan: the verdict, why, and everything needed to act on it by hand (#317). Nothing is ordered. The verdict
 * and the key numbers come first; the sizing arithmetic, limit prices per leg and adjustments fold away (#326). */
export function PlanCard({ plan, compact = false }: { plan: TradePlan; compact?: boolean }) {
  const Icon = ICON[plan.action];
  const ex = plan.exits;
  const s = plan.size, e = plan.entry;
  return (
    <article aria-label={`Trade plan: ${plan.title}`} className={cx("min-w-0 space-y-3 rounded-xl border p-3 sm:p-4", plan.action === "AVOID" ? "border-loss/30" : plan.action === "ENTER" ? "border-gain/30" : "border-warn/30")}>
      <header className="flex items-start gap-2.5">
        <span className={cx("grid size-8 shrink-0 place-items-center rounded-lg", plan.action === "ENTER" ? "bg-gain-soft text-gain" : plan.action === "WAIT" ? "bg-warn-soft text-warn" : "bg-loss-soft text-loss")}>
          <Icon className="size-4" />
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          {/* the verdict leads; a long qualifier wraps inside its pill instead of squeezing the title */}
          <span className={cx("inline-flex max-w-full rounded-lg px-2 py-0.5 text-xs font-semibold ring-1 ring-inset", VERDICT[plan.action])}>
            <span data-testid="plan-verdict">{plan.label}</span>
          </span>
          <p className="line-clamp-2 text-sm font-semibold break-words" title={plan.title}>{plan.title}</p>
          <p className="text-[11px] text-muted">
            <Parts parts={[plan.kind.replaceAll("_", " "), plan.days_to_expiry != null && `${plan.days_to_expiry} days to expiry`, plan.spot != null && `spot ${lvl(plan.spot)}`].filter(Boolean)} />
          </p>
        </div>
      </header>
      <KeyNumbers plan={plan} />
      {s && (
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span className="whitespace-nowrap"><span className="text-muted">Size </span><span className="num font-semibold">{s.suggested ? `${s.suggested}× these legs` : "none fits"}</span></span>
          {e?.status === "ok" && <span className="whitespace-nowrap"><span className="text-muted">Entry </span><span className="num font-medium">{`net ${px(Math.abs(e.net_mid!))} ${e.net_mid! >= 0 ? "credit" : "debit"}`}</span></span>}
          {plan.costs != null && <span className="whitespace-nowrap"><span className="text-muted">Costs to expiry </span><span className="num">{rs(plan.costs)}</span></span>}
        </p>
      )}
      <ul className="list-disc space-y-1 pl-5 text-xs text-foreground/90">
        {plan.reasons.map((r) => <li key={r}>{r}</li>)}
      </ul>
      {plan.legs.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {plan.legs.map((l) => (
            <Badge key={`${l.side}${l.right}${l.strike}`} tone={l.side === "buy" ? "gain" : "loss"}>
              <span className="num">{l.side.toUpperCase()} {l.lots}× {l.strike.toLocaleString("en-IN")} {l.right === "call" ? "CE" : l.right === "put" ? "PE" : "FUT"}</span>
            </Badge>
          ))}
        </div>
      )}
      {ex && (
        <div className="rounded-lg bg-background-subtle/60 px-3 py-1">
          <Row label="Stop-loss" help="Premium level and the underlying level that hit it: now = if the price got there today with IVs unchanged (a sell-off usually lifts put IVs, so the real stop comes sooner); at expiry = payoff only."><Level x={ex.stop} credit={ex.credit} what="stop" /></Row>
          <Row label="Profit target"><Level x={ex.target} credit={ex.credit} what="target" /></Row>
          <Row label="Time exit">{ex.time.text}</Row>
          {ex.notes.map((n) => <p key={n} className="py-1 text-[11px] text-warn">{n}</p>)}
          {ex.rule_of_thumb && <p className="py-1 text-[11px] text-muted">{ex.rule_of_thumb}</p>}
        </div>
      )}
      {!compact && s && <PlanDetail plan={plan} />}
      <Evidence plan={plan} />
      {plan.legs.length > 0 && plan.expiry && <PlanActions plan={plan} />}
    </article>
  );
}

function PlanDetail({ plan }: { plan: TradePlan }) {
  const s = plan.size!, e = plan.entry, p = plan.probability;
  return (
    <details className="group rounded-lg border border-border/70 px-3 py-1.5 text-xs">
      <summary className="cursor-pointer py-0.5 font-medium text-muted hover:text-foreground">Details: sizing, limit prices, adjustments and what invalidates the trade</summary>
      <div className="mt-1 grid gap-x-6 lg:grid-cols-2">
        <div className="min-w-0">
          <Row label="Sizing" help="Lots from your profile's F&O budget: max-loss % × F&O capital, divided by the max loss plus entry costs of one set of these legs. Without a proven edge the plan uses half of it.">
            <span className="font-normal">{s.reason}</span>
          </Row>
          <Row label="Max profit"><N>{plan.max_profit == null ? "unlimited" : rs(plan.max_profit)}</N></Row>
          <Row label="Max loss"><N>{plan.max_loss == null ? "unlimited" : rs(plan.max_loss)}</N></Row>
          <Row label="Breakevens">{plan.breakevens?.length ? plan.breakevens.map((b, i) => <span key={b}>{i > 0 && " / "}<N>{lvl(b)}</N></span>) : "none"}</Row>
          <Row label="Chance of profit" help={p?.note}><Parts parts={[`real-world ${pct(p?.real_world)}`, `risk-neutral ${pct(p?.risk_neutral)}`]} /></Row>
        </div>
        <div className="min-w-0">
          <Row label="Entry (limit)" help="Limit-price guidance from the live bid/ask: the mid is the patient price, the far side fills now. Groww's chain has no bid/ask, so it is read from NSE's chain (named below with its time); unknown when neither has it.">
            {e?.status === "ok" ? <Parts parts={[`net ${px(Math.abs(e.net_mid!))} ${e.net_mid! >= 0 ? "credit" : "debit"}`, `fill now ${px(Math.abs(e.net_fill_now!))}`]} /> : "unknown (no live bid/ask)"}
          </Row>
          {e?.status === "ok" && e.source && <p className="py-0.5 text-[11px] text-muted">{e.source}{e.slippage_inr != null ? <> · filling now instead of at the mid costs <N>{rs(e.slippage_inr)}</N></> : ""}</p>}
          {e?.legs.map((l) => (
            <p key={`${l.side}${l.right}${l.strike}`} className="num py-0.5 text-[11px] text-muted">
              <Parts parts={[`${l.side} ${l.strike} ${l.right}:`, ...(l.status === "ok" ? [`limit ${px(l.limit_from)}–${px(l.limit_to)}`, `(bid ${px(l.bid)} / ask ${px(l.ask)})`] : ["no bid/ask;", `last ${px(l.last)}`])]} sep=" " />
            </p>
          ))}
          {e?.notes.map((n) => <p key={n} className="py-0.5 text-[11px] text-warn">{n}</p>)}
        </div>
        {plan.adjustments && (
          <div className="mt-2 lg:col-span-2">
            <p className="font-medium">Adjustments and rolls</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5">{plan.adjustments.rules.map((r) => <li key={r}>{r}</li>)}</ul>
            <p className="mt-1 text-[11px] text-muted">{plan.adjustments.note}</p>
            <p className="mt-2 font-medium">Invalidated if</p>
            <ul className="list-disc space-y-0.5 pl-5">{plan.invalidation?.map((r) => <li key={r}>{r}</li>)}</ul>
          </div>
        )}
      </div>
    </details>
  );
}

function Evidence({ plan }: { plan: TradePlan }) {
  const ev = plan.evidence, pr = plan.paper;
  const tone: Tone = ev.level === "proven" ? "gain" : ev.level === "negative" ? "loss" : "neutral";
  return (
    <div className="space-y-1 text-xs">
      <p className="flex flex-wrap items-center gap-1.5">
        <ClipboardList className="size-3.5 text-muted" />
        <span className="text-muted">Evidence</span>
        <Badge tone={tone}>{ev.level === "none" ? "no proven edge" : ev.level}</Badge>
        <span data-testid="plan-evidence">{ev.text}</span>
        <Link href={ev.url} className="whitespace-nowrap text-brand hover:underline">backtests →</Link>
      </p>
      {pr && (
        <p className="text-muted">
          Your paper record: {pr.closed ? <>{pr.closed} closed ({pr.wins} won · {pr.losses} lost), net <N>{rs(pr.total_net)}</N></> : "none closed"}{pr.open ? ` · ${pr.open} open` : ""}. {pr.note}
        </p>
      )}
      {plan.action === "AVOID" && <Callout tone="loss">AVOID: the plan is shown so you can see why. Paper-trade it if you want to test it; real money is at your own risk.</Callout>}
      {plan.disclaimer && <p className="text-[11px] text-muted">{plan.disclaimer}{plan.as_of && ` Chain as of ${day(plan.as_of)}.`}</p>}
    </div>
  );
}
