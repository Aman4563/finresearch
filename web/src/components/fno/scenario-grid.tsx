"use client";

// The book's spot × vol grid as a heatmap (#394): one table per horizon, NIFTY moves across, extra vol shifts down.
// Diverging fill on the gain/loss tokens, its strength by |P&L| over the horizon's largest |P&L|; the figure itself
// stays in text ink (never the series colour). An unknown cell reads "unknown", never ₹0. Hover or focus a cell for
// its clock, legs at intrinsic, floored IVs and which end of each beta band was used.

import { useState } from "react";

import { rs } from "@/components/fno/paper-trade";
import type { ScenarioCell } from "@/components/fno/scenario-types";
import { ScrollArea, Segmented, cx } from "@/components/ui";
import { when } from "@/lib/api";

export const HORIZONS: { value: string; label: string }[] = [
  { value: "1d", label: "1 session" }, { value: "5d", label: "5 sessions" }, { value: "expiry", label: "To nearest expiry" },
];
const pct = (m: number) => `${m > 0 ? "+" : m < 0 ? "−" : ""}${Math.abs(m * 100).toFixed(0)}%`;
const pts = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v)}`;

function fill(pnl: number | null, max: number): string | undefined {
  if (pnl == null || pnl === 0 || max <= 0) return undefined;
  const a = Math.round(10 + 55 * Math.min(1, Math.abs(pnl) / max));
  return `color-mix(in oklab, var(${pnl < 0 ? "--loss" : "--gain"}) ${a}%, transparent)`;
}

function tip(c: ScenarioCell): string {
  const ends = Object.entries(c.ends).map(([u, e]) => `${u} at its band's ${e === "hi" ? "upper" : "lower"} end`);
  return [`NIFTY ${pct(c.move)}, vol ${pts(c.dvol)} pts, at ${when(c.at)}`, c.pnl == null ? "P&L unknown" : `P&L ${rs(c.pnl)}`,
    c.intrinsic ? `${c.intrinsic} leg(s) at intrinsic (expired)` : "", c.floored ? `${c.floored} IV(s) floored at 1 vol point` : "", ...ends]
    .filter(Boolean).join(" · ");
}

export function ScenarioHeatmap({ cells, moves, vols }: { cells: ScenarioCell[]; moves: number[]; vols: number[] }) {
  const [h, setH] = useState("1d");
  const here = cells.filter((c) => c.horizon === h);
  const max = Math.max(0, ...here.map((c) => Math.abs(c.pnl ?? 0)));
  const at = (m: number, v: number) => here.find((c) => c.move === m && c.dvol === v);
  return (
    <div className="space-y-3">
      <Segmented label="Horizon" value={h} onChange={setH} options={HORIZONS.filter((o) => cells.some((c) => c.horizon === o.value))} />
      <ScrollArea focusable aria-label="Scenario grid">
        <table aria-label="P&L by NIFTY move and vol shift" className="min-w-max border-separate border-spacing-0.5 text-xs">
          <thead>
            <tr><th scope="col" className="px-2 py-1 text-left font-medium text-muted">Vol shift ↓ · NIFTY →</th>
              {moves.map((m) => <th key={m} scope="col" className="num px-1.5 py-1 text-right font-medium text-muted">{pct(m)}</th>)}</tr>
          </thead>
          <tbody>
            {vols.map((v) => (
              <tr key={v}>
                <th scope="row" className="num whitespace-nowrap px-2 py-1 text-left font-medium text-muted">{pts(v)} pts</th>
                {moves.map((m) => {
                  const c = at(m, v);
                  return (
                    <td key={m} title={c ? tip(c) : "not computed"} tabIndex={0} style={{ background: fill(c?.pnl ?? null, max) }}
                      className={cx("num whitespace-nowrap rounded px-1.5 py-1.5 text-right", c?.pnl == null && "text-muted italic")}>
                      {c?.pnl == null ? "unknown" : rs(c.pnl)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollArea>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
        <span className="inline-flex items-center gap-1"><span className="size-3 rounded-sm" style={{ background: fill(-1, 1) }} aria-hidden /> loss</span>
        <span className="inline-flex items-center gap-1"><span className="size-3 rounded-sm" style={{ background: fill(1, 1) }} aria-hidden /> gain</span>
        <span>Stronger fill = larger |P&amp;L| at this horizon. Hover or focus a cell for its details.</span>
      </p>
    </div>
  );
}
