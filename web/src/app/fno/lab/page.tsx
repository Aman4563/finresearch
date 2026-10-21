"use client";

import { Beaker } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { rupees } from "@/components/fno/backtest";
import { Parts } from "@/components/fno/fit";
import { DEFAULT_SPEC, StrategyForm } from "@/components/fno/lab-form";
import { RunView } from "@/components/fno/lab-run";
import type { LabResponse, LabSpec, LabStrategy, PaperRule } from "@/components/fno/lab-types";
import { RiskBanner } from "@/components/fno/risk";
import { Badge, Button, Callout, Card, EmptyState, ErrorNote, PageHeader, Skeleton, Table } from "@/components/ui";
import { api } from "@/lib/api";

export default function StrategyLabPage() {
  const [data, setData] = useState<LabResponse | null>(null);
  const [paper, setPaper] = useState<PaperRule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [runId, setRunId] = useState<number | null>(null);
  const [editing, setEditing] = useState<LabStrategy | null>(null);
  const load = useCallback(async () => {
    try {
      const [d, p] = await Promise.all([api<LabResponse>("/api/fno/lab"), api<{ rules: PaperRule[] }>("/api/fno/lab/paper")]);
      setData(d);
      setPaper(p.rules);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);

  const send = async (path: string, body: unknown) => {
    setBusy(true);
    try {
      const got = await api<unknown>(path, { method: "POST", body: JSON.stringify(body) });
      setFormError(null);
      await load();
      return got;
    } catch (e) {
      setFormError((e as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  };
  const run = async (versionId: number, oos: boolean) => {
    const got = (await send(`/api/fno/lab/versions/${versionId}/runs`, { oos })) as { id: number } | null;
    if (got) setRunId(got.id);
  };

  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      <PageHeader icon={<Beaker className="size-5" />} eyebrow="Research" title="F&O strategy lab"
        description="Build your own strategy from building blocks, backtest it on NSE's end-of-day F&O history with real charges and slippage, and judge it the way the pre-registered library is judged. The out-of-sample years are locked when you create a strategy; every version you test on them is counted, and the bar rises with each."
        actions={<Link href="/fno/backtests" className="text-sm text-brand hover:underline">Pre-registered library →</Link>} />
      <RiskBanner />
      <ErrorNote error={error} onRetry={load} />
      {!data ? (!error && <Skeleton className="h-40 rounded-xl" />) : (
        <>
          <Callout tone="info" title={`${data.variants.n} variants tried so far: ${data.variants.library} pre-registered + ${data.variants.lab} from the lab`}>
            Tune freely with in-sample runs: they never read the out-of-sample years and are not counted. An out-of-sample run counts its spec once, for good, and raises the deflated-Sharpe bar for every strategy. Runs run in the background (at most {data.blocks.run_cap_minutes} minutes). Multiple-testing adjustment: {data.blocks.deflated_sharpe_source}
          </Callout>
          <Card title={editing ? `New version of ${editing.name}` : "New strategy"} subtitle={editing ? `Same underlying and the same locked split (out of sample from ${editing.split.oos_from})` : "Its in-sample / out-of-sample split is fixed when you save it"}>
            <StrategyForm key={editing?.id ?? "new"} blocks={data.blocks} underlyings={data.underlyings} busy={busy} withName={!editing}
              initial={editing ? editing.versions[editing.versions.length - 1].spec : DEFAULT_SPEC}
              submitLabel={editing ? "Save version" : "Save strategy"}
              onSubmit={async (spec: LabSpec, name: string) => {
                const ok = editing ? await send(`/api/fno/lab/strategies/${editing.id}/versions`, { spec }) : await send("/api/fno/lab/strategies", { name, spec });
                if (ok) setEditing(null);
              }} />
            {formError && <p role="alert" className="mt-2 text-sm text-loss">{formError}</p>}
            {editing && <Button variant="ghost" className="mt-2" onClick={() => setEditing(null)}>Cancel</Button>}
          </Card>
          {runId != null && <RunView key={runId} runId={runId} onChanged={load} />}
          {data.strategies.length === 0 ? (
            <EmptyState icon={<Beaker className="size-5" />} title="No strategy yet">Save one above, then run it in sample first.</EmptyState>
          ) : data.strategies.map((s) => (
            <Card key={s.id} title={<span className="flex min-w-0 flex-wrap items-center gap-2"><span className="line-clamp-2 min-w-0 break-words" title={s.name}>{s.name}</span><Badge tone="neutral">{s.underlying}</Badge>{s.oos_versions > 1 && <Badge tone="warn">{s.oos_versions} versions seen out of sample</Badge>}</span>}
              subtitle={`In sample ${s.split.is.join(" → ")} · out of sample from ${s.split.oos_from} (locked: ${s.split.rule}) · that window is ${s.split.oos_label ?? "already published, not out of sample"}`}>
              <Table label={`Versions of ${s.name}`}>
                <thead><tr><th>Version</th><th>Spec</th><th>Runs</th><th /></tr></thead>
                <tbody>{s.versions.map((v) => (
                  <tr key={v.id}>
                    <td>v{v.version}{v.counted_variant && <Badge tone="info">counted</Badge>}</td>
                    {/* a long spec reads as a paragraph instead of stretching the table to its full length */}
                    <td className="text-xs"><p className="w-64 whitespace-normal sm:w-80">{v.describe}</p></td>
                    <td className="text-xs">{v.runs.length === 0 ? "—" : v.runs.map((r) => (
                      <button key={r.id} type="button" className="mr-2 text-brand hover:underline" onClick={() => setRunId(r.id)}>
                        #{r.id} {r.oos ? "OOS" : "IS"} {r.status}{r.summary?.status ? ` · ${r.summary.status.replaceAll("_", " ")}` : ""}
                      </button>
                    ))}</td>
                    <td className="whitespace-nowrap">
                      <Button variant="secondary" disabled={busy} onClick={() => run(v.id, false)}>Run in sample</Button>{" "}
                      <Button variant="secondary" disabled={busy} onClick={() => run(v.id, true)}>Run out of sample</Button>
                    </td>
                  </tr>
                ))}</tbody>
              </Table>
              <Button variant="ghost" className="mt-2" onClick={() => setEditing(s)}>New version</Button>
            </Card>
          ))}
          <Card title="Paper rule sets" subtitle="Versions sent to paper trading: each signal day becomes a paper trade in the paper book">
            {paper.length === 0 ? <p className="text-sm text-muted">None yet: open a finished run and choose “Send to paper trading”.</p> : paper.map((r) => (
              <div key={r.id} className="space-y-2 border-b border-border/60 pb-3 last:border-0">
                <p className="text-sm font-medium break-words">Rule set #{r.id}: {r.strategy} v{r.version} <Badge tone={r.status === "active" ? "gain" : "neutral"}>{r.status}</Badge>{" "}
                  <span className="font-normal text-muted"><Parts parts={[`closed ${r.results.closed}`, `net ${rupees(r.results.total_net)}`, `last close checked ${r.last_day ?? "none yet"}`]} /></span></p>
                {r.note && <p className="text-xs text-muted">{r.note}</p>}
                {r.signals.length > 0 && (
                  <Table label={`Signals of rule set ${r.id}`}>
                    <thead><tr><th>Signal close</th><th>Status</th><th>Expiry</th><th>Lots</th><th>Paper trade</th><th>Net</th><th>Exit</th></tr></thead>
                    <tbody>{r.signals.map((g) => (
                      <tr key={g.id}><td>{g.day}</td><td>{g.status}</td><td>{g.expiry}</td><td className="num">{g.lots}</td>
                        <td>{g.paper_id ? <Link className="text-brand hover:underline" href="/fno/paper">#{g.paper_id} {g.paper_status}</Link> : (g.note ?? "—")}</td>
                        <td className="num">{rupees(g.net_pnl)}</td><td>{g.exit_reason ?? "—"}</td></tr>
                    ))}</tbody>
                  </Table>
                )}
                {r.status === "active" && <Button variant="ghost" onClick={() => send(`/api/fno/lab/paper/${r.id}/stop`, {})}>Stop new signals</Button>}
              </div>
            ))}
            <p className="mt-2 text-xs text-muted">{paper[0]?.method}</p>
          </Card>
        </>
      )}
    </div>
  );
}
