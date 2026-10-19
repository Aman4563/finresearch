"use client";

import { useState } from "react";

import { fmtINR } from "@/components/charts";
import { Badge, Button, cx } from "@/components/ui";
import { api, day, when } from "@/lib/api";

import type { ExitRule, PaperTrade } from "./book-types";
import { ExitRules } from "./exit-rules";

export const rs = (x: number | null | undefined) => (x == null ? "unknown" : `${x < 0 ? "−" : x > 0 ? "+" : ""}${fmtINR(Math.abs(Math.round(x)))}`);
export const pnlTone = (x: number | null | undefined) => (x == null ? "text-muted" : x > 0 ? "text-gain" : x < 0 ? "text-loss" : "");
const paise = (x: number) => `₹${x.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const STATUS_TONE = { open: "info", awaiting_settlement: "warn", closed: "neutral" } as const;

/** One paper trade: legs with entry, last and exit prices; MTM or the settled result; its exit rules. */
export function PaperTradeCard({ t, rules, onChange }: { t: PaperTrade; rules: ExitRule[]; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const close = async () => {
    if (!window.confirm(`Square off paper trade #${t.id} at the live prices? It stays in the book as closed.`)) return;
    setBusy(true);
    try {
      await api(`/api/fno/paper/${t.id}/close`, { method: "POST" });
      setError(null);
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const shown = t.status === "closed" ? t.net_pnl : t.mtm_net_if_closed;
  return (
    <div id={String(t.id)} className="rounded-xl border border-border p-3 text-xs">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {/* a long label is cut to one line with the full text on hover, so the result stays next to the trade's name (#326) */}
        <p className="flex min-w-0 max-w-full items-baseline gap-1 text-sm font-semibold">
          <span className="shrink-0 whitespace-nowrap">#{t.id} {t.symbol} <span className="font-normal text-muted">{day(t.expiry)}</span></span>
          {t.label && <span className="min-w-0 truncate font-normal" title={t.label}>· {t.label}</span>}
        </p>
        <Badge tone={STATUS_TONE[t.status]}>{t.status.replaceAll("_", " ")}</Badge>
        {t.status === "awaiting_settlement" ? <span className="text-sm font-semibold text-warn">net: waiting for the settlement price</span>
          : <span className={cx("num text-sm font-semibold", pnlTone(shown))}>{t.status === "closed" ? "net" : "net if closed now"} <span className="whitespace-nowrap">{rs(shown)}</span></span>}
        {t.status !== "closed" && <span className="num text-muted"><span className="whitespace-nowrap">MTM {rs(t.mtm_gross)}</span> {t.mtm_at ? `· ${when(t.mtm_at)} (${t.mtm_source})` : "· not marked yet"}</span>}
        {t.status === "open" && <Button variant="ghost" className="ml-auto" onClick={close} disabled={busy}>Square off (paper)</Button>}
      </div>
      <div tabIndex={0} role="region" aria-label={`Legs of paper trade ${t.id}`} className="-mx-3 mt-2 overflow-x-auto px-3">
        <table className="num w-full min-w-[520px] [&_td]:py-0.5 [&_td]:pr-3 [&_td]:whitespace-nowrap [&_th]:pr-3 [&_th]:text-left [&_th]:font-medium [&_th]:text-muted [&_tr>*:first-child]:sticky [&_tr>*:first-child]:left-0 [&_tr>*:first-child]:bg-card">
          <thead><tr><th>Leg</th><th>Units</th><th>LTP at entry</th><th>Fill</th><th>Last</th><th>Exit</th></tr></thead>
          <tbody>
            {t.legs.map((x) => (
              <tr key={x.id} className="border-t border-border">
                <td className={x.side === "buy" ? "text-gain" : "text-loss"}>{x.side} {x.right} {x.right !== "future" ? x.strike : ""}</td>
                <td>{x.qty}</td><td>{x.ref_price}</td><td>{x.entry_price}</td><td>{x.last_price ?? "—"}</td><td>{x.exit_price ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-1 text-muted">
        Opened {when(t.opened_at)} at {t.price_source} + {t.slippage_pct}% slippage · entry costs <span className="num whitespace-nowrap">{paise(t.entry_costs)}</span>
        {t.exit_costs != null && <> · exit costs <span className="num whitespace-nowrap">{paise(t.exit_costs)}</span></>}
        {t.settlement_price != null && ` · settled at ${t.settlement_price} (${t.settlement_source})`}
        {t.gross_pnl != null && <> · gross <span className="num whitespace-nowrap">{rs(t.gross_pnl)}</span></>}
      </p>
      {typeof t.exit_detail?.physical_settlement === "string" && <p className="text-warn">{t.exit_detail.physical_settlement}</p>}
      {t.note && <p className="text-warn">{t.note}</p>}
      {error && <p role="alert" className="text-loss">{error}</p>}
      {t.status === "open" && <div className="mt-2"><ExitRules paperId={t.id} rules={rules} onChange={onChange} /></div>}
    </div>
  );
}
