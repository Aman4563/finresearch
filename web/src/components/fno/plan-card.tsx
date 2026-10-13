"use client";

import { ClipboardList, ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import Link from "next/link";

import { fmtINR } from "@/components/charts";
import { Badge, Callout, InfoTip, type Tone, cx } from "@/components/ui";
import { day } from "@/lib/api";

import { PlanActions } from "./plan-actions";
import type { ExitLevel, PlanAction, TradePlan } from "./plan-types";

const TONE: Record<PlanAction, Tone> = { ENTER: "gain", WAIT: "warn", AVOID: "loss" };
const ICON = { ENTER: ShieldCheck, WAIT: ShieldQuestion, AVOID: ShieldAlert } as const;
const rs = (x: number | null | undefined) => (x == null ? "unknown" : `${x < 0 ? "−" : ""}${fmtINR(Math.abs(Math.round(x)))}`);
const lvl = (x: number | null | undefined) => (x == null ? "—" : Math.round(x).toLocaleString("en-IN"));
const pct = (x: number | null | undefined) => (x == null ? "unknown" : `${(x * 100).toFixed(0)}%`);
const px = (x: number | null | undefined) => (x == null ? "—" : `₹${x.toFixed(2)}`);

function Row({ label, children, help }: { label: string; children: React.ReactNode; help?: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 border-b border-border/50 py-1.5 text-xs last:border-0">
      <span className="flex items-center gap-1 text-muted">{label}{help && <InfoTip>{help}</InfoTip>}</span>
      <span className="num min-w-0 text-right font-medium">{children}</span>
    </div>
  );
}

function Level({ x, credit, what }: { x: ExitLevel | null | undefined; credit: boolean; what: "stop" | "target" }) {
  if (!x) return <span className="text-muted">none for this position</span>;
  const now = [x.underlying_now.below, x.underlying_now.above].filter((v) => v != null).map(lvl).join(" / ");
  const exp = [x.underlying_at_expiry.below, x.underlying_at_expiry.above].filter((v) => v != null).map(lvl).join(" / ");
  const prem = x.premium_per_share == null ? "" : `${credit ? "buy back at" : "value"} ${px(x.premium_per_share)}/share · `;
  return (
    <span>
      {what === "stop" ? "−" : "+"}{rs(x.rupees)} · {prem}underlying {now || (what === "target" ? "by time decay, not a price level" : "—")}
      {exp && <span className="text-muted"> (at expiry {exp})</span>}
    </span>
  );
}

/** One trade plan: the verdict, why, and everything needed to act on it by hand (#317). Nothing is ordered. */
export function PlanCard({ plan, compact = false }: { plan: TradePlan; compact?: boolean }) {
  const Icon = ICON[plan.action];
  const ex = plan.exits;
  return (
    <article aria-label={`Trade plan: ${plan.title}`} className={cx("space-y-3 rounded-xl border p-3 sm:p-4", plan.action === "AVOID" ? "border-loss/30" : plan.action === "ENTER" ? "border-gain/30" : "border-warn/30")}>
      <header className="flex flex-wrap items-start gap-2">
        <span className={cx("grid size-8 shrink-0 place-items-center rounded-lg", plan.action === "ENTER" ? "bg-gain-soft text-gain" : plan.action === "WAIT" ? "bg-warn-soft text-warn" : "bg-loss-soft text-loss")}>
          <Icon className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">{plan.title}</p>
          <p className="text-[11px] text-muted">{plan.kind.replaceAll("_", " ")}{plan.days_to_expiry != null && ` · ${plan.days_to_expiry} days to expiry`}{plan.spot != null && ` · spot ${lvl(plan.spot)}`}</p>
        </div>
        <Badge tone={TONE[plan.action]}><span data-testid="plan-verdict">{plan.label}</span></Badge>
      </header>
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
      {!compact && plan.size && <PlanDetail plan={plan} />}
      {ex && (
        <div className="rounded-lg bg-background-subtle/60 px-3 py-1">
          <Row label="Stop-loss" help="Premium level and the underlying level that hit it: now = if the price got there today with IVs unchanged (a sell-off usually lifts put IVs, so the real stop comes sooner); at expiry = payoff only."><Level x={ex.stop} credit={ex.credit} what="stop" /></Row>
          <Row label="Profit target"><Level x={ex.target} credit={ex.credit} what="target" /></Row>
          <Row label="Time exit">{ex.time.text}</Row>
          {ex.notes.map((n) => <p key={n} className="py-1 text-[11px] text-warn">{n}</p>)}
          {ex.rule_of_thumb && <p className="py-1 text-[11px] text-muted">{ex.rule_of_thumb}</p>}
        </div>
      )}
      <Evidence plan={plan} />
      {plan.legs.length > 0 && plan.expiry && <PlanActions plan={plan} />}
    </article>
  );
}

function PlanDetail({ plan }: { plan: TradePlan }) {
  const s = plan.size!, e = plan.entry, p = plan.probability;
  return (
    <div className="grid gap-x-6 sm:grid-cols-2">
      <div>
        <Row label="Size" help="Lots from your profile's F&O budget: max-loss % × F&O capital, divided by the max loss plus entry costs of one set of these legs. Without a proven edge the plan uses half of it.">
          {s.suggested ? `${s.suggested}× these legs` : "none fits"}
        </Row>
        <p className="pb-1 text-[11px] text-muted">{s.reason}</p>
        <Row label="Max profit">{plan.max_profit == null ? "unlimited" : rs(plan.max_profit)}</Row>
        <Row label="Max loss">{plan.max_loss == null ? "unlimited" : rs(plan.max_loss)}</Row>
        <Row label="Breakevens">{plan.breakevens?.length ? plan.breakevens.map(lvl).join(" / ") : "none"}</Row>
        <Row label="Costs to expiry">{rs(plan.costs)}</Row>
      </div>
      <div>
        <Row label="Entry (limit)" help="Limit-price guidance from the live bid/ask: the mid is the patient price, the far side fills now. Unknown when the chain has no bid/ask (Groww's has none).">
          {e?.status === "ok" ? `net ${px(Math.abs(e.net_mid!))} ${e.net_mid! >= 0 ? "credit" : "debit"} (fill now ${px(Math.abs(e.net_fill_now!))})` : "unknown (no live bid/ask)"}
        </Row>
        {e?.legs.map((l) => (
          <p key={`${l.side}${l.right}${l.strike}`} className="num text-[11px] text-muted">
            {l.side} {l.strike} {l.right}: {l.status === "ok" ? `limit ${px(l.limit_from)}–${px(l.limit_to)} (bid ${px(l.bid)} / ask ${px(l.ask)})` : `no bid/ask; last ${px(l.last)}`}
          </p>
        ))}
        {e?.notes.map((n) => <p key={n} className="text-[11px] text-warn">{n}</p>)}
        <Row label="Chance of profit" help={p?.note}>{`real-world ${pct(p?.real_world)} · risk-neutral ${pct(p?.risk_neutral)}`}</Row>
      </div>
      {plan.adjustments && (
        <details className="mt-2 text-xs sm:col-span-2">
          <summary className="cursor-pointer text-muted">Adjustments, rolls and what invalidates the trade</summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">{plan.adjustments.rules.map((r) => <li key={r}>{r}</li>)}</ul>
          <p className="mt-1 text-[11px] text-muted">{plan.adjustments.note}</p>
          <p className="mt-2 font-medium">Invalidated if</p>
          <ul className="list-disc space-y-0.5 pl-5">{plan.invalidation?.map((r) => <li key={r}>{r}</li>)}</ul>
        </details>
      )}
    </div>
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
        <Link href={ev.url} className="text-brand hover:underline">backtests →</Link>
      </p>
      {pr && (
        <p className="text-muted">
          Your paper record: {pr.closed ? `${pr.closed} closed (${pr.wins} won · ${pr.losses} lost), net ${rs(pr.total_net)}` : "none closed"}{pr.open ? ` · ${pr.open} open` : ""}. {pr.note}
        </p>
      )}
      {plan.action === "AVOID" && <Callout tone="loss">AVOID: the plan is shown so you can see why. Paper-trade it if you want to test it; real money is at your own risk.</Callout>}
      {plan.disclaimer && <p className="text-[11px] text-muted">{plan.disclaimer}{plan.as_of && ` Chain as of ${day(plan.as_of)}.`}</p>}
    </div>
  );
}
