"use client";

import { NotebookPen } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import type { PaperBook } from "@/components/fno/book-types";
import { Fig } from "@/components/fno/fit";
import { PaperTradeCard, pnlTone, rs } from "@/components/fno/paper-trade";
import { RiskBanner } from "@/components/fno/risk";
import { Card, EmptyState, ErrorNote, PageHeader, Skeleton, Stat } from "@/components/ui";
import { api } from "@/lib/api";

export default function PaperBookPage() {
  const [book, setBook] = useState<PaperBook | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setBook(await api<PaperBook>("/api/fno/paper"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    const t = setTimeout(load, 0);
    const i = setInterval(() => !document.hidden && load(), 60_000); // the monitor marks open trades every 5 minutes
    return () => {
      clearTimeout(t);
      clearInterval(i);
    };
  }, [load]);
  const r = book?.results;
  const open = book?.trades.filter((t) => t.status !== "closed") ?? [];
  const closed = book?.trades.filter((t) => t.status === "closed").reverse() ?? [];

  return (
    <div className="space-y-6">
      <PageHeader icon={<NotebookPen className="size-5" />} eyebrow="Research" title="F&O paper book"
        description="Strategies paper-traded from the builder, marked to market with live prices and settled at expiry, after costs. Every trade is kept: none can be deleted, so the results cannot be cherry-picked. No orders are ever placed." />
      <RiskBanner />
      <ErrorNote error={error} onRetry={load} />
      {!book ? (
        !error && <Skeleton className="h-40 rounded-xl" />
      ) : book.counts.all === 0 ? (
        <EmptyState icon={<NotebookPen className="size-5" />} title="No paper trades yet">
          Build a strategy on the <Link className="text-brand hover:underline" href="/fno">F&amp;O page</Link> and press &quot;Paper trade this&quot; (market hours only).
        </EmptyState>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <Stat label="Closed trades" value={r!.closed} hint={`${book.counts.open} open · ${book.counts.awaiting_settlement} awaiting settlement`} />
            <Stat label="Win rate" display={<Fig>{r!.win_rate == null ? "—" : `${(r!.win_rate * 100).toFixed(0)}%`}</Fig>} hint={`${r!.wins} won · ${r!.losses} lost`}
              help="Closed trades with net P&L above zero ÷ all closed trades (after costs)." />
            <Stat label="Expectancy" display={<Fig className={pnlTone(r!.expectancy)}>{r!.expectancy == null ? "—" : rs(r!.expectancy)}</Fig>}
              help="Mean net P&L per closed trade, after entry and exit costs." />
            <Stat label="Total net" display={<Fig className={pnlTone(r!.total_net)}>{rs(r!.total_net)}</Fig>} />
            <Stat label="Max drawdown" display={<Fig className="text-loss">{r!.max_drawdown ? `−₹${Math.round(r!.max_drawdown).toLocaleString("en-IN")}` : "₹0"}</Fig>}
              help="The largest fall of the cumulative net P&L from its running peak, starting from zero." />
            <Stat label="Avg win / loss" display={<span className="block space-y-0.5 text-sm"><Fig className={pnlTone(r!.avg_win)}>{r!.avg_win == null ? "—" : rs(r!.avg_win)}</Fig><Fig className={pnlTone(r!.avg_loss)}>{r!.avg_loss == null ? "—" : rs(r!.avg_loss)}</Fig></span>} />
          </div>
          <p className="text-[11px] text-muted">{book.method}</p>
          <p className="text-[11px] text-muted">Default slippage for new paper trades: <span className="num">{book.default_slippage_pct}%</span>{book.default_slippage_source ? ` (${book.default_slippage_source})` : ""}.</p>
          <Card title={`Open (${open.length})`} subtitle="Marked every 5 minutes in market hours; settled automatically after expiry at the underlying's close">
            {open.length ? <div className="space-y-3">{open.map((t) => <PaperTradeCard key={t.id} t={t} rules={book.rules} onChange={load} />)}</div>
              : <p className="text-sm text-muted">No open paper trades.</p>}
          </Card>
          <Card title={`Closed (${closed.length})`} subtitle="Newest first; all counted in the results above">
            {closed.length ? <div className="space-y-3">{closed.map((t) => <PaperTradeCard key={t.id} t={t} rules={book.rules} onChange={load} />)}</div>
              : <p className="text-sm text-muted">No closed paper trades yet.</p>}
          </Card>
        </>
      )}
      <p className="text-xs text-muted">Learn more: <Link className="text-brand hover:underline" href="/learn/process#pipeline">backtest → paper → small size</Link> · <Link className="text-brand hover:underline" href="/learn/risk#stops">exit and stop types</Link></p>
    </div>
  );
}
