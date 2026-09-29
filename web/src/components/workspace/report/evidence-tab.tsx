"use client";

// Evidence tab: every claim in the run's ledger, filterable by stream, status and importance, searchable, grouped by
// research stream. Each claim opens its source (document lines or URL) in the evidence panel.

import { ChevronDown, FileSearch, Search } from "lucide-react";
import { useDeferredValue, useMemo, useState } from "react";

import { Badge, Card, EmptyState, ErrorNote, SkeletonRows, cx, inputClass } from "@/components/ui";
import { type OpenClaim, STATUS_LABEL } from "@/components/workspace/report/shared";
import { QualityBar } from "@/components/workspace/report/summary";
import type { Citation, Claim } from "@/lib/api";

const STATUSES = ["verified", "needs_review", "unverified", "contradicted", "unsupported"] as const;
const streamName = (s: string) => s.replace(/^(stock|fund|bond)_/, "").replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()).replace("News30", "News (30 days)");

function source(c: Citation | undefined) {
  if (!c) return "no citation";
  if (c.url) return /^https?:\/\//.test(c.url) ? (() => { try { return new URL(c.url).hostname; } catch { return c.url.slice(0, 40); } })() : "fincalc calculation";
  return `${c.document_title ?? "document"} p${c.page ?? "?"}`;
}

function ClaimRow({ c, onOpen, cited }: { c: Claim; onOpen: OpenClaim; cited: boolean }) {
  return (
    <li className="py-2.5">
      <button type="button" onClick={() => onOpen(c.id)} className="group grid w-full grid-cols-[auto_1fr] gap-x-3 text-left">
        <span className="num mt-0.5 rounded border border-border px-1 text-[10px] text-muted group-hover:border-brand group-hover:text-brand">C{c.id}</span>
        <span className="min-w-0">
          <span className="block text-[13px] leading-snug [overflow-wrap:anywhere] group-hover:text-foreground">{c.statement}</span>
          <span className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted [overflow-wrap:anywhere]">
            <Badge status={c.status}>{STATUS_LABEL[c.status] ?? c.status}</Badge>
            {c.value && (
              <span className="num text-foreground">
                {c.metric}: <span title={c.value}>{Number.isFinite(Number(c.value)) ? Number(c.value).toLocaleString("en-IN", { maximumFractionDigits: 6 }) : c.value}</span> {c.unit}
              </span>
            )}
            {c.period && <span>{c.period}</span>}
            {c.importance === "high" && <Badge tone="info">high importance</Badge>}
            {cited && <Badge tone="brand">cited in report</Badge>}
            <span className="truncate">· {source(c.citations[0])}</span>
          </span>
          {c.verifier_note && c.status !== "verified" && (
            <span className="mt-1 line-clamp-2 block text-[11px] italic text-muted [overflow-wrap:anywhere]">Verifier: {c.verifier_note}</span>
          )}
        </span>
      </button>
    </li>
  );
}

export function EvidenceTab({ claims, error, reload, onOpen, cited }: {
  claims: Claim[] | null; error: string | null; reload: () => void; onOpen: OpenClaim; cited: Set<number>;
}) {
  const [q, setQ] = useState("");
  const query = useDeferredValue(q.trim().toLowerCase());
  const [stream, setStream] = useState("all");
  const [status, setStatus] = useState<Set<string>>(new Set());
  const [imp, setImp] = useState("all");
  const [onlyCited, setOnlyCited] = useState(false);
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [more, setMore] = useState<Set<string>>(new Set());

  const streams = useMemo(() => [...new Set((claims ?? []).map((c) => c.stream))].sort(), [claims]);
  const counts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const c of claims ?? []) m[c.status] = (m[c.status] ?? 0) + 1;
    return m;
  }, [claims]);
  const shown = useMemo(
    () =>
      (claims ?? []).filter(
        (c) =>
          (stream === "all" || c.stream === stream) &&
          (!status.size || status.has(c.status)) &&
          (imp === "all" || c.importance === imp) &&
          (!onlyCited || cited.has(c.id)) &&
          (!query || `c${c.id} ${c.statement} ${c.metric ?? ""} ${c.period ?? ""} ${c.verifier_note ?? ""}`.toLowerCase().includes(query)),
      ),
    [claims, stream, status, imp, onlyCited, cited, query],
  );
  const groups = useMemo(() => {
    const g = new Map<string, Claim[]>();
    for (const c of shown) g.set(c.stream, [...(g.get(c.stream) ?? []), c]);
    return [...g.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [shown]);

  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!claims) return <Card><SkeletonRows rows={10} /></Card>;
  const flip = (set: Set<string>, v: string) => {
    const n = new Set(set);
    if (n.has(v)) n.delete(v);
    else n.add(v);
    return n;
  };

  return (
    <div className="space-y-4">
      <Card padded={false} className="sticky top-[7.25rem] z-10 p-3 sm:p-4">
        <div className="flex flex-wrap items-center gap-2">
          <label className="relative min-w-60 flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search claims, metrics, periods, C123…" className={cx(inputClass, "pl-8")} aria-label="Search claims" />
          </label>
          <select value={stream} onChange={(e) => setStream(e.target.value)} className={cx(inputClass, "w-auto")} aria-label="Stream">
            <option value="all">All streams</option>
            {streams.map((s) => <option key={s} value={s}>{streamName(s)}</option>)}
          </select>
          <select value={imp} onChange={(e) => setImp(e.target.value)} className={cx(inputClass, "w-auto")} aria-label="Importance">
            <option value="all">Any importance</option>
            <option value="high">High importance</option>
            <option value="normal">Normal</option>
            <option value="low">Low</option>
          </select>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {STATUSES.filter((s) => counts[s]).map((s) => (
            <button key={s} type="button" onClick={() => setStatus((x) => flip(x, s))} aria-pressed={status.has(s)}
              className={cx("rounded-full transition", status.size > 0 && !status.has(s) && "opacity-45")}>
              <Badge status={s}>{STATUS_LABEL[s]} <span className="num">{counts[s]}</span></Badge>
            </button>
          ))}
          <label className="ml-auto flex items-center gap-1.5 text-xs text-muted">
            <input type="checkbox" checked={onlyCited} onChange={(e) => setOnlyCited(e.target.checked)} className="accent-[var(--brand)]" />
            cited in the report only ({cited.size})
          </label>
        </div>
        <p className="mt-2 text-[11px] text-muted">
          Showing <span className="num text-foreground">{shown.length}</span> of <span className="num">{claims.length}</span> claims. Contradicted and unsupported claims are shown for transparency; the report never uses them as fact.
        </p>
      </Card>

      {groups.length === 0 ? (
        <EmptyState icon={<FileSearch className="size-5" />} title="No claims match">Clear a filter or change the search.</EmptyState>
      ) : (
        groups.map(([s, list]) => {
          const by: Record<string, number> = {};
          for (const c of list) by[c.status] = (by[c.status] ?? 0) + 1;
          const open = !closed.has(s);
          const limit = more.has(s) ? list.length : 40;
          return (
            <Card key={s} padded={false} className="animate-fade-up">
              <button type="button" onClick={() => setClosed((x) => flip(x, s))} aria-expanded={open} className="flex w-full items-center gap-3 px-4 py-3 text-left sm:px-5">
                <ChevronDown className={cx("size-4 text-muted transition-transform", !open && "-rotate-90")} />
                <span className="font-semibold">{streamName(s)}</span>
                <span className="num text-xs text-muted">{list.length}</span>
                <QualityBar counts={by} total={list.length} className="ml-auto w-24 sm:w-40" />
              </button>
              {open && (
                <ul className="divide-y divide-border/60 border-t border-border/60 px-4 sm:px-5">
                  {list.slice(0, limit).map((c) => <ClaimRow key={c.id} c={c} onOpen={onOpen} cited={cited.has(c.id)} />)}
                  {list.length > limit && (
                    <li className="py-2">
                      <button type="button" onClick={() => setMore((x) => flip(x, s))} className="text-xs font-medium text-brand hover:underline">
                        Show {list.length - limit} more
                      </button>
                    </li>
                  )}
                </ul>
              )}
            </Card>
          );
        })
      )}
    </div>
  );
}
