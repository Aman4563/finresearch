"use client";

import { Briefcase } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { fmtINR } from "@/components/charts";
import { Badge, Card, ErrorNote, Skeleton, cx } from "@/components/ui";
import { api, day, when } from "@/lib/api";

import type { ExitRule, PaperBook, PositionGroup, PositionsView } from "./book-types";
import { ExitRules } from "./exit-rules";

const rs = (x: number | null | undefined) => (x == null ? "unknown" : `${x < 0 ? "−" : x > 0 ? "+" : ""}${fmtINR(Math.abs(Math.round(x)))}`);
const tone = (x: number | null | undefined) => (x == null ? "text-muted" : x > 0 ? "text-gain" : x < 0 ? "text-loss" : "");

function Group({ g, rules, onRules }: { g: PositionGroup; rules: ExitRule[]; onRules: () => void }) {
  return (
    <div className="rounded-xl border border-border p-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <p className="font-semibold">{g.underlying ?? "Contracts not in Groww's instruments file"}{g.expiry && <span className="ml-1 text-xs font-normal text-muted">expires {day(g.expiry)}</span>}</p>
        {g.days_to_expiry != null && <Badge tone={g.days_to_expiry <= 3 ? "warn" : "neutral"}>{g.days_to_expiry} days to expiry</Badge>}
        <span className={cx("num text-sm font-semibold", tone(g.unrealised_pnl))}>P&amp;L {rs(g.unrealised_pnl)}</span>
        <span className="num text-xs text-muted">max loss {g.max_loss != null ? rs(g.max_loss) : g.max_loss_note ?? "unknown"}</span>
        {g.net_greeks && <span className="num text-xs text-muted">Δ {g.net_greeks.delta.toFixed(1)} · Θ/day {g.net_greeks.theta.toFixed(0)} · vega {g.net_greeks.vega.toFixed(0)}</span>}
      </div>
      <div className="-mx-3 mt-2 overflow-x-auto px-3">
        <table className="num w-full min-w-[560px] text-xs [&_td]:py-1 [&_td]:pr-3 [&_th]:pr-3 [&_th]:text-left [&_th]:font-medium [&_th]:text-muted">
          <thead><tr><th>Contract</th><th>Qty</th><th>Avg</th><th>LTP</th><th>P&amp;L</th><th>IV</th><th>Delta</th></tr></thead>
          <tbody>
            {g.legs.map((x) => (
              <tr key={x.symbol} className="border-t border-border">
                <td>{x.symbol}{!x.direction_known && <span className="ml-1 text-warn" title={x.note ?? ""}>long/short unknown</span>}</td>
                <td>{x.qty ?? x.quantity ?? "—"}</td>
                <td>{x.avg_price ?? "—"}</td>
                <td>{x.ltp ?? "—"}</td>
                <td className={tone(x.unrealised_pnl)}>{rs(x.unrealised_pnl)}</td>
                <td>{x.iv != null ? `${(x.iv * 100).toFixed(1)}%` : "—"}</td>
                <td>{x.greeks ? x.greeks.delta.toFixed(1) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {g.key !== "unresolved" && <div className="mt-2"><ExitRules positionKey={g.key} rules={rules} onChange={onRules} /></div>}
    </div>
  );
}

/** "My positions": the Groww F&O positions read at the last sync / in-session poll, priced live (#308). */
export function MyPositions() {
  const [view, setView] = useState<PositionsView | null>(null);
  const [rules, setRules] = useState<ExitRule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const [v, b] = await Promise.all([api<PositionsView>("/api/fno/positions"), api<PaperBook>("/api/fno/paper")]);
      setView(v);
      setRules(b.rules);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);
  if (!view && !error) return <Skeleton className="h-24 rounded-xl" />;
  if (view && view.status !== "ok" && !view.groups.length) return null; // no Groww connection or nothing read yet
  return (
    <Card title="My positions" icon={<Briefcase className="size-4" />}
      subtitle={view ? `Groww F&O positions read ${view.read_at ? when(view.read_at) : "—"}; prices ${view.ltp_source ?? "not live (market closed or no Groww session)"}` : undefined}
      help="Read-only from your Groww account (positions, segment FNO). Greeks use the option chain's IV. A position whose long/short direction Groww's answer does not confirm shows no P&L or greeks.">
      <ErrorNote error={error} onRetry={load} />
      {view && view.groups.length === 0 && <p className="text-sm text-muted">No open F&amp;O positions at the last read.</p>}
      <div className="space-y-3">
        {view?.groups.map((g) => <Group key={g.key} g={g} rules={rules} onRules={load} />)}
      </div>
      {view?.notes.map((x) => <p key={x} className="mt-2 text-[11px] text-muted">{x}</p>)}
    </Card>
  );
}
