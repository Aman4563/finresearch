"use client";

// Historical stress replay (#345): today's holdings replayed day by day through dated market episodes. Loaded only on
// request: the first run reads each holding's prices for the windows (cached on disk for good afterwards).

import { History } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote, Table, cx } from "@/components/ui";
import { day, useApi } from "@/lib/api";

import { Loading, spct } from "./shared-analytics";
import { inr, signed } from "./types";

type Unmapped = { key: string; name: string; value: number; reason: string };
type ReplayScenario = {
  id: string; label: string; note: string; window: string[]; available: boolean; reason?: string;
  base?: string; index_source?: string; index?: { move: number | null; drawdown: number | null };
  mapped_value?: number; unmapped_value?: number; mapped_share?: number | null; unmapped: Unmapped[];
  loss?: { inr: number; pct: number; day: string } | null;
  worst_day?: { date: string; pct: number; inr: number } | null;
  drawdown?: { peak: string; trough: string; pct: number; inr: number } | null;
  recovery?: { date: string | null; days: number | null; searched_to: string; note: string } | null;
  contributors?: { key: string; name: string; inr: number; share: number | null }[];
  positions?: { key: string; name: string; value: number; how: string; move: number; drawdown_inr: number }[];
  vix?: { start: number; peak: number; peak_date: string } | null;
};
type Replay = { available: boolean; reason?: string; method: string; as_of?: string; scenarios: ReplayScenario[] };

function UnmappedList({ items }: { items: Unmapped[] }) {
  if (!items.length) return null;
  return (
    <div className="mt-2 rounded-md border border-warn/40 p-2">
      <p className="font-medium text-warn">Unmapped: not in the figures above (unknown, not 0 %)</p>
      <ul className="mt-1 space-y-0.5 text-muted">
        {items.map((u) => <li key={u.key}><span className="text-foreground">{u.name}</span> · {inr(u.value)} · {u.reason}</li>)}
      </ul>
    </div>
  );
}

function ScenarioDetail({ s }: { s: ReplayScenario }) {
  if (!s.available) return <><p className="text-muted">{s.reason}. {s.note}</p><UnmappedList items={s.unmapped} /></>;
  return (
    <>
      <p className="text-muted">{s.note} Market path: {s.index_source}, from the {day(s.base)} close. Index {spct(s.index?.move)} to the window&apos;s end, {spct(s.index?.drawdown)} peak to trough{s.vix ? `; India VIX ${s.vix.start} → ${s.vix.peak} (${day(s.vix.peak_date)})` : ""}.</p>
      <p className="mt-1 text-muted">Worst day {day(s.worst_day?.date)}: {spct(s.worst_day?.pct)} ({signed(s.worst_day?.inr)}). Peak {day(s.drawdown?.peak)} → trough {day(s.drawdown?.trough)}: {spct(s.drawdown?.pct)} ({signed(s.drawdown?.inr)}). Recovery: {s.recovery?.note}{s.recovery?.days != null ? ` (${s.recovery.days} days)` : ""}.</p>
      <Table label={`${s.label}: holdings`} className="mt-2">
        <thead><tr><th>Holding</th><th className="text-right">Move</th><th className="text-right">Peak → trough ₹</th><th>How</th></tr></thead>
        <tbody>{s.positions?.map((p) => (
          <tr key={p.key}><td className="max-w-[14rem] truncate">{p.name}</td><td className="num text-right">{spct(p.move)}</td>
            <td className={cx("num text-right", p.drawdown_inr < 0 ? "text-loss" : "text-muted")}>{signed(p.drawdown_inr)}</td><td className="text-muted">{p.how}</td></tr>
        ))}</tbody>
      </Table>
      <UnmappedList items={s.unmapped} />
    </>
  );
}

export function StressReplay({ refresh }: { refresh: number }) {
  const [run, setRun] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const { data, error, reload } = useApi<Replay>(run ? `/api/portfolio/analytics/stress-replay?r=${refresh}` : null);
  return (
    <Card title="Historical stress replay" icon={<History className="size-4" />}
      help="Today's holdings replayed day by day through past episodes: own prices where they exist, else a category index or beta × the Nifty 50, else listed as unmapped. Instructive, not a forecast.">
      {!run ? (
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          <p className="flex-1">Sep-Nov 2008, May-Aug 2013, Feb-Mar 2020, 3-5 Jun 2024 and Feb-Mar 2026. The first run reads each holding&apos;s prices for those windows, which can take a few minutes; they are kept for good.</p>
          <Button variant="secondary" onClick={() => setRun(true)}>Run the replay</Button>
        </div>
      ) : error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <Loading what="the stress replay" /> : !data.available ? (
        <p className="text-sm text-muted">{data.reason}</p>
      ) : (
        <div className="space-y-2">
          {data.scenarios.map((s) => (
            <div key={s.id} className="rounded-lg border border-border">
              <button type="button" onClick={() => setOpen(open === s.id ? null : s.id)} className="flex w-full flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-left">
                <span className="text-sm font-medium">{s.label}</span>
                <span className="flex flex-wrap items-center gap-3 text-xs">
                  {s.available && s.drawdown ? (
                    <span className={cx("num font-semibold", s.drawdown.pct < 0 ? "text-loss" : "text-gain")}>peak to trough {spct(s.drawdown.pct)} · {signed(s.drawdown.inr)}</span>
                  ) : <Badge tone="neutral">not available</Badge>}
                  {s.unmapped.length > 0 && <Badge tone="warn">{s.unmapped.length} unmapped · {inr(s.unmapped_value ?? s.unmapped.reduce((a, u) => a + u.value, 0))}</Badge>}
                </span>
              </button>
              {open === s.id && <div className="border-t border-border px-3 py-2.5 text-xs"><ScenarioDetail s={s} /></div>}
            </div>
          ))}
          <p className="text-[11px] text-muted">{data.method}</p>
        </div>
      )}
    </Card>
  );
}
