"use client";

import { BellOff, CheckCheck, CircleAlert, CircleCheck, Info, Zap } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { dayLabel, istDate, useNow } from "@/components/ipo/lib";
import { EmptyState, ErrorNote, cx } from "@/components/ui";
import { ALERTS_CHANGED, api, type AlertItem } from "@/lib/api";

const LEVEL = {
  action: { icon: Zap, dot: "bg-loss", ring: "text-loss bg-loss-soft", label: "Action" },
  warn: { icon: CircleAlert, dot: "bg-warn", ring: "text-warn bg-warn-soft", label: "Warning" },
  info: { icon: Info, dot: "bg-info", ring: "text-info bg-info-soft", label: "Info" },
  // a skip rule that stopped firing is good news: never draw it in the red "action" style (roadmap §B0.2)
  cleared: { icon: CircleCheck, dot: "bg-gain", ring: "text-gain bg-gain-soft", label: "Cleared" },
} as const;

const levelOf = (a: AlertItem) => (a.kind === "rule_change" && /\bnow clear\b/i.test(a.message) ? "cleared" : a.level);

const KIND: Record<string, string> = {
  rule_change: "Rule changed",
  rule_alert: "Alert rule",
  subscription: "Subscription",
  allotment: "Allotment",
  listing_open: "Listed (open)",
  listing_close: "Listed (close)",
  lockin: "Lock-in ends",
};

const kindLabel = (k: string) => KIND[k] ?? k.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());

/** The rule an alert came from, with its data source and a link to the instrument's page. */
function RuleSource({ data }: { data: Record<string, unknown> }) {
  const s = (k: string) => (data[k] == null ? "" : String(data[k]));
  return (
    <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted">
      <Link href={`/rules?kind=${encodeURIComponent(s("rule_kind") || "stock")}`} className="font-medium text-brand hover:underline">
        rule {s("rule_id")}
      </Link>
      {s("rule") && <span>“{s("rule")}”</span>}
      {s("source") && <span className="break-all">source: {s("source")}</span>}
      {s("path") && <Link href={s("path")} className="hover:text-brand">open {s("instrument")}</Link>}
    </p>
  );
}

/** Alerts grouped by IST day, coloured by level, with "mark read". `limit` shows the latest N (dashboard). */
export function AlertFeed({ alerts, onRead, limit, showSymbol = true, emptyHint, emptyTitle = "No alerts yet" }: {
  alerts: AlertItem[]; onRead: () => void; limit?: number; showSymbol?: boolean; emptyHint?: string; emptyTitle?: string;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const now = useNow(60000);
  const read = async (id: number) => {
    setBusy(id);
    try {
      await api(`/api/alerts/${id}/read`, { method: "POST" });
      setError(null);
      window.dispatchEvent(new Event(ALERTS_CHANGED));
      onRead();
    } catch (e) {
      setError(`Could not mark the alert read: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  };
  const shown = limit ? alerts.slice(0, limit) : alerts;
  if (!shown.length)
    return (
      <EmptyState icon={<BellOff className="size-5" />} title={emptyTitle}>
        {emptyHint ?? "Watch an IPO and FinResearch alerts you when subscription, your rules, allotment or listing change."}
      </EmptyState>
    );
  const groups = new Map<string, AlertItem[]>();
  for (const a of shown) {
    const k = a.created_at ? istDate(Date.parse(a.created_at)) : "unknown";
    groups.set(k, [...(groups.get(k) ?? []), a]);
  }
  const today = now == null ? "" : istDate(now);
  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      {[...groups.entries()].map(([d, items]) => (
        <section key={d}>
          <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted">
            {d === "unknown" ? "Undated" : d === today ? "Today" : dayLabel(d, { weekday: "long", day: "numeric", month: "short" })}
          </h3>
          <ul className="stagger space-y-2">
            {items.map((a) => {
              const L = LEVEL[levelOf(a) as keyof typeof LEVEL] ?? LEVEL.info;
              const Icon = L.icon;
              return (
                <li
                  key={a.id}
                  className={cx(
                    "relative flex items-start gap-3 rounded-lg border border-border bg-background-subtle/50 p-3 pl-4 transition hover:border-border-strong",
                    a.read_at && "opacity-60",
                  )}
                >
                  <span className={cx("absolute inset-y-2 left-0 w-1 rounded-r-full", L.dot)} aria-hidden />
                  <span className={cx("grid size-7 shrink-0 place-items-center rounded-lg", L.ring)} title={L.label} role="img" aria-label={L.label}>
                    <Icon className="size-3.5" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="flex flex-wrap items-center gap-x-2 text-xs text-muted">
                      <span className="font-medium text-foreground">{kindLabel(a.kind)}</span>
                      {showSymbol && (a.label ?? a.nse_symbol) && (
                        a.watch_id ? (
                          <Link href={`/monitor/${a.watch_id}`} className="num hover:text-brand">{a.label ?? a.nse_symbol}</Link>
                        ) : <span className="num">{a.label ?? a.nse_symbol}</span>
                      )}
                      {a.created_at && (
                        <span className="num">
                          {new Date(a.created_at).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit", timeZone: "Asia/Kolkata" })}
                        </span>
                      )}
                      {!a.read_at && <span className="size-1.5 rounded-full bg-brand" role="img" aria-label="unread" />}
                    </p>
                    <p className="mt-0.5 text-sm break-words">{a.message}</p>
                    {a.kind === "rule_alert" && a.data?.rule_id != null && <RuleSource data={a.data} />}
                  </div>
                  {!a.read_at && (
                    <button
                      type="button"
                      onClick={() => read(a.id)}
                      disabled={busy === a.id}
                      title="Mark read"
                      className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs text-muted transition hover:bg-card hover:text-foreground disabled:opacity-50"
                    >
                      <CheckCheck className="size-3.5" />
                      <span className="hidden sm:inline">Mark read</span>
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
