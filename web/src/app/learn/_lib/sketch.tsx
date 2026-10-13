"use client";

import Link from "next/link";
import { useMemo } from "react";

import { PayoffChart } from "@/components/charts";
import { Badge } from "@/components/ui";

import { sketchCurve } from "./bs";
import type { Sketch, StrategyCard } from "./types";

const inr = (v: number) => `₹${Math.round(v).toLocaleString("en-IN")}`;

function legText(l: Sketch["legs"][number], premium: number | null) {
  const what = l.right === "stock" ? "the stock" : l.right === "future" ? "a future" : `${l.strike} ${l.right}`;
  const later = l.days != null ? ` (${l.days}-day expiry)` : "";
  return `${l.side} ${l.lots && l.lots > 1 ? `${l.lots} × ` : ""}${what}${later}${premium != null ? ` @ ${premium.toFixed(1)}` : ""}`;
}

/** Illustrative payoff for a strategy: Black-Scholes premiums at a flat IV, drawn with the app's PayoffChart. */
export function StrategySketch({ sketch, card }: { sketch: Sketch; card?: StrategyCard }) {
  const c = useMemo(() => sketchCurve(sketch), [sketch]);
  const unbounded = (side: "profit" | "loss") => sketch.unlimited === side || sketch.unlimited === "both";
  return (
    <div className="my-3 space-y-3 rounded-xl border border-border bg-background-subtle/50 p-3">
      <p className="text-xs text-muted">
        Illustration, not a quote: spot <span className="num">{sketch.spot.toLocaleString("en-IN")}</span>, lot {sketch.lot},{" "}
        {sketch.days} days to expiry, IV {Math.round(sketch.iv * 100)} %, premiums from Black-Scholes. Legs:{" "}
        {c.premiums.map((p, i) => <span key={i} className="num">{i ? "; " : ""}{legText(p.leg, p.premium)}</span>)}.
      </p>
      <PayoffChart points={c.points} spot={sketch.spot} breakevens={c.breakevens} height={220} />
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge tone="gain">best in range {unbounded("profit") ? "unlimited" : inr(c.max)}</Badge>
        <Badge tone="loss">worst in range {unbounded("loss") ? "unlimited" : inr(c.min)}</Badge>
        {c.breakevens.length > 0 && <Badge tone="warn">breakeven {c.breakevens.map((b) => b.toLocaleString("en-IN")).join(" / ")}</Badge>}
        <span className="text-muted">Before costs; drawn on the {sketch.legs.some((l) => l.days != null) ? "first " : ""}expiry day.</span>
      </div>
      {card && (
        <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
          {([["View", card.view], ["When it fits", card.when], ["Max profit", card.maxProfit], ["Max loss", card.maxLoss],
            ["Greeks", card.greeks], ["Typical management", card.manage]] as const).map(([k, v]) => (
            <div key={k}><dt className="text-[11px] font-medium uppercase tracking-wide text-muted">{k}</dt><dd>{v}</dd></div>
          ))}
          <div className="sm:col-span-2">
            <dt className="text-[11px] font-medium uppercase tracking-wide text-muted">In our backtest</dt>
            <dd>
              {card.backtest
                ? <>Pre-registered as <Link className="text-brand underline" href={`/fno/backtests#${card.backtest.key}`}>{card.backtest.label}</Link>: see whether it passed, after costs and out of sample (results change with each run, so none are quoted here).</>
                : <>Not one of the eight pre-registered variants, so there is no backtest evidence for it in the app.</>}
              {card.builder && <> Try it on a live chain: <Link className="text-brand underline" href="/fno">F&amp;O strategy builder</Link> → preset “{card.builder}”.</>}
            </dd>
          </div>
        </dl>
      )}
    </div>
  );
}
