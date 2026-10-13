"use client";

// One Strategy Lab run (#318): progress while the background worker runs, then the overfitting warnings first, the
// evidence (reusing the library's StrategyCard), the library beside it, the trade list and "Send to paper trading".

import { AlertTriangle, NotebookPen } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Badge, Button, Callout, Card, Progress, Table } from "@/components/ui";
import { api } from "@/lib/api";

import { STATUS, StrategyCard, rupees } from "./backtest";
import type { Brief, LabRunView, LabTrade } from "./lab-types";

const num = (x: number | null | undefined, d = 2) => (x == null ? "—" : x.toFixed(d));
const ACTIVE = new Set(["queued", "running"]);

function BriefCells({ b }: { b: Brief | null }) {
  if (!b) return <><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></>;
  return (
    <>
      <td className="num">{b.trades}</td><td className="num">{rupees(b.expectancy)}</td><td className="num">{num(b.sharpe)}</td>
      <td className="num">{b.max_drawdown_pct == null ? "—" : `${b.max_drawdown_pct.toFixed(1)}%`}</td><td className="num">{num(b.deflated_sharpe ?? null, 3)}</td>
    </>
  );
}

export function RunView({ runId, onChanged }: { runId: number; onChanged?: () => void }) {
  const [v, setV] = useState<LabRunView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [trades, setTrades] = useState<LabTrade[] | null>(null);
  const [paperMsg, setPaperMsg] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setV(await api<LabRunView>(`/api/fno/lab/runs/${runId}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [runId]);
  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);
  const active = !!v && ACTIVE.has(v.status);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [active, load]);
  useEffect(() => {
    if (v && !ACTIVE.has(v.status)) onChanged?.();
  }, [v?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <p role="alert" className="text-sm text-loss">{error}</p>;
  if (!v) return <p className="text-sm text-muted">Loading run {runId}…</p>;
  const head = `Run ${v.id} · ${v.strategy.name} v${v.strategy.version} · ${v.oos ? "in + out of sample" : "in sample only"}`;
  if (active) {
    return (
      <Card title={head} subtitle="Running in the background: you can leave this page and come back.">
        <Progress value={v.progress} label={`${Math.round(v.progress * 100)}% · ${v.progress_note ?? v.status}`} />
      </Card>
    );
  }
  if (v.status !== "done" || !v.result) {
    return <Callout tone="loss" title={`${head}: ${v.status.replace("_", " ")}`}>{v.error ?? "No result."}</Callout>;
  }
  const r = v.result;
  const st = STATUS[r.status] ?? { tone: "neutral" as const, label: r.status };
  const sendToPaper = async () => {
    try {
      const got = await api<{ id: number }>(`/api/fno/lab/versions/${v.version_id}/paper`, { method: "POST" });
      setPaperMsg(`Paper rule set #${got.id} started: signals from the next stored close on, filled at live prices in market hours.`);
      onChanged?.();
    } catch (e) {
      setPaperMsg((e as Error).message);
    }
  };
  const lib = v.library?.strategies ?? [];
  return (
    <div className="space-y-4">
      <Callout tone={r.warnings.length ? "warn" : "info"} icon={<AlertTriangle className="size-4" />}
        title={<span className="flex flex-wrap items-center gap-2">{head}<Badge tone={st.tone}>{st.label}</Badge></span>}>
        {r.warnings.length ? <ul className="list-disc space-y-1 pl-5">{r.warnings.map((w) => <li key={w}>{w}</li>)}</ul> : "No overfitting warning for this run."}
        {v.bar_now && v.oos && (
          <p className="mt-2 text-xs">Deflated Sharpe at run time (N = {v.n_trials}): {num(r.windows?.oos?.deflated_sharpe ?? null, 3)}; with today&apos;s N = {v.bar_now.n_trials}: {num(v.bar_now.deflated_sharpe, 3)}. {v.bar_now.note}</p>
        )}
      </Callout>
      <StrategyCard r={r} capital={r.book.capital} />
      <Card title="Against the pre-registered library" subtitle={v.library ? `Library run ${v.library.run_id}, ${v.library.variants_tried} variants; windows may differ from this strategy's` : "No library evaluation saved yet"}>
        {lib.length > 0 && (
          <Table label="Library comparison">
            <thead><tr><th>Strategy</th><th>Status</th><th>OOS trades</th><th>₹/trade</th><th>Sharpe</th><th>Max DD</th><th>DSR</th></tr></thead>
            <tbody>
              <tr className="font-medium"><td>{r.name} (this run)</td><td>{st.label}</td><BriefCells b={r.windows?.oos ?? r.windows?.is ?? null} /></tr>
              {lib.map((x) => <tr key={x.key}><td>{x.name}</td><td>{x.status}</td><BriefCells b={x.oos ?? x.is} /></tr>)}
            </tbody>
          </Table>
        )}
      </Card>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="secondary" onClick={async () => setTrades((await api<{ trades: LabTrade[] }>(`/api/fno/lab/runs/${v.id}/trades`)).trades)}>Show the {v.n_trades} trades</Button>
        <Button onClick={sendToPaper} icon={<NotebookPen className="size-3.5" />}>Send to paper trading</Button>
        {paperMsg && <p role="status" className="text-sm">{paperMsg}</p>}
      </div>
      {trades && (
        <Card title="Trades" subtitle="Entry and exit at the day's close with the backtest's slippage; net after charges and STT">
          <Table label="Trades">
            <thead><tr><th>Entry</th><th>Exit</th><th>Expiry</th><th>Lots</th><th>Legs</th><th>Premium</th><th>Net</th><th>Why</th></tr></thead>
            <tbody>{trades.map((t, i) => (
              <tr key={i}><td>{t.entry_day}</td><td>{t.exit_day}</td><td>{t.expiry}</td><td className="num">{t.lots}×{t.lot}</td>
                <td className="text-xs">{t.legs.map((l) => `${l.side} ${l.strike} ${l.right}`).join(", ")}</td>
                <td className="num">{rupees(t.premium)}</td><td className="num">{rupees(t.net)}</td><td>{t.reason.replaceAll("_", " ")}</td></tr>
            ))}</tbody>
          </Table>
        </Card>
      )}
    </div>
  );
}
