"use client";

// Live refresh while the market is open. The API decides the session (NSE trading days and holidays): equities
// 09:15–15:30 IST, IPO bidding 10:00–17:00 IST. In session a page polls its data; outside it the page keeps the last
// figures and says the market is closed. <LiveStamp> shows when the data last updated.

import { RefreshCw } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";

import { cx } from "@/components/ui";
import { useApi } from "@/lib/api";

export type MarketStatus = {
  now: string;
  trading_day: boolean;
  holiday: string | null;
  /** false: NSE's holiday list for this year is not loaded, so a holiday can read as a trading day (`warning`) */
  holidays_known?: boolean;
  warning?: string | null;
  equity: { open: boolean; hours: string; next_open: string | null; closes_at: string | null };
  ipo_bidding: { open: boolean; hours: string };
};

export type Session = "equity" | "ipo";

export function useMarketStatus() {
  return useApi<MarketStatus>("/api/market/status", 60000);
}

export function sessionOpen(st: MarketStatus | null | undefined, session: Session) {
  if (!st) return false;
  return session === "equity" ? st.equity.open : st.ipo_bidding.open;
}

/** `useApi` that polls every `everyMs` while the session is open (and `active`); outside it every `idleMs`, or not at
 * all when `idleMs` is unset. */
export function useLive<T>(path: string | null, { session, everyMs, idleMs, active = true }: {
  session: Session; everyMs: number; idleMs?: number; active?: boolean;
}) {
  const status = useMarketStatus();
  const live = active && sessionOpen(status.data, session);
  const r = useApi<T>(path, live ? everyMs : idleMs);
  return { ...r, live, status: status.data };
}

function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

const time = (d: Date | string) =>
  new Date(d).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" });

function ago(ms: number) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
}

/** "about 50 s behind" / "about 1 min behind": how old the exchange's own timestamp was when the app fetched it. */
export function behind(ms: number) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 5) return "real time";
  return s < 90 ? `about ${s} s behind` : `about ${Math.round(s / 60)} min behind`;
}

function nextOpenLabel(st: MarketStatus) {
  if (!st.equity.next_open) return "";
  const d = new Date(st.equity.next_open);
  const today = new Date(st.now).toDateString() === d.toDateString();
  return today ? `opens ${time(d).replace(/:00(?=\s)/, "")}` : `opens ${d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", timeZone: "Asia/Kolkata" })}, 9:15 am`;
}

/**
 * "● Live · updates every 30s · Updated 3:31:05 pm (12s ago) · NSE as of 3:30 pm  ⟳"
 * or "○ Market closed (opens Wed, 9:15 am) · Updated …". `asOf` is the exchange's own timestamp when it has one.
 */
export function LiveStamp({ session, live, status, updatedAt, everyMs, asOf, asOfLabel = "NSE as of", onRefresh, className, autoRefresh = true }: {
  session: Session; live: boolean; status: MarketStatus | null | undefined; updatedAt: Date | null; everyMs: number;
  asOf?: string | null; asOfLabel?: string; onRefresh?: () => void; className?: string;
  /** false: the session is open but the viewer turned auto-refresh off (profile → Time frames & watch). */ autoRefresh?: boolean;
}) {
  const now = useNow();
  const every = everyMs >= 60000 ? `${Math.round(everyMs / 60000)} min` : `${Math.round(everyMs / 1000)}s`;
  const closed = !status
    ? "Checking market hours…"
    : session === "equity"
      ? status.holiday ? `Market holiday: ${status.holiday}` : `Market closed · ${nextOpenLabel(status)}`
      : status.holiday ? `Market holiday: ${status.holiday}` : "Bidding hours closed (10:00 am–5:00 pm on bidding days)";
  return (
    <div className={cx("flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted", className)} aria-live="polite">
      <span className={cx("inline-flex items-center gap-1.5 font-medium", live ? "text-gain" : "text-muted")}>
        <span className={cx("size-1.5 rounded-full", live ? "bg-gain text-gain animate-pulse-ring" : "bg-muted")} />
        {live ? (autoRefresh ? `Live · updates every ${every}` : "Market open · auto-refresh off") : closed}
      </span>
      {updatedAt && (
        <span title={updatedAt.toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })}>
          Updated <span className="num text-foreground/80">{time(updatedAt)}</span> ({ago(now - updatedAt.getTime())})
        </span>
      )}
      {asOf && (
        <span>
          {asOfLabel} <span className="num text-foreground/80">{time(asOf)}</span>
          {live && updatedAt && <> ({behind(updatedAt.getTime() - new Date(asOf).getTime())})</>}
        </span>
      )}
      {status?.warning && <span className="text-warn" title={status.warning}>Holiday list not loaded</span>}
      {onRefresh && (
        <button type="button" onClick={onRefresh} title="Refresh now" aria-label="Refresh now"
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 transition hover:bg-background-subtle hover:text-foreground">
          <RefreshCw className="size-3" /> Refresh
        </button>
      )}
    </div>
  );
}

/** A small link to the exact API/CSV request behind a figure (machine-readable, not a page to read). */
export function DataRequest({ href, label = "data request" }: { href: string; label?: string }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" title={href}
      className="text-[10px] uppercase tracking-wide text-muted underline decoration-dotted underline-offset-2 hover:text-foreground">
      {label}
    </a>
  );
}

/**
 * Where a price came from and how fresh it is. Intraday: "NSE quote-page chart, 1-minute price samples · last price
 * 12:51:10 · about 10 s behind · refreshes every 30 s". EOD: "NSE daily bars (end of day) · last bar 29 Sep 26
 * (today's bar is added after the close)".
 */
export function Freshness({ mode, sourceLabel, sourceHref, requestHref, asOf, fetchedAt, delayS, live, refresh, className }: {
  mode: "intraday" | "eod"; sourceLabel: string; sourceHref?: string | null; asOf?: string | null; fetchedAt?: string | null;
  /** the exact data request (JSON/CSV), linked small as "data request"; sourceHref stays a page a person opens */
  requestHref?: string | null;
  delayS?: number | null; live?: boolean; refresh?: string; className?: string;
}) {
  const src = sourceHref ? (
    <a href={sourceHref} target="_blank" rel="noreferrer" className="underline decoration-dotted underline-offset-2 hover:text-foreground">{sourceLabel}</a>
  ) : sourceLabel;
  const parts: ReactNode[] = [<span key="s">Source: {src}{requestHref && <> (<DataRequest href={requestHref} />)</>}</span>];
  if (mode === "intraday") {
    if (asOf) {
      const d = new Date(asOf);
      const stamp = d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" });
      parts.push(<span key="a">{live ? "last price" : "session ended"} <span className="num text-foreground/80">{live ? time(d) : stamp}</span></span>);
    }
    if (live && delayS != null) parts.push(<span key="d">{behind(delayS * 1000)} when fetched</span>);
    if (!live) parts.push(<span key="c">market closed, showing the last session</span>);
    else if (refresh) parts.push(<span key="r">{refresh}</span>);
    if (fetchedAt && !live) parts.push(<span key="f">fetched {time(fetchedAt)}</span>);
  } else if (asOf) {
    const today = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
    parts.push(<span key="a">last bar <span className="num text-foreground/80">{new Date(`${asOf.slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "2-digit", timeZone: "UTC" })}</span></span>);
    if (asOf.slice(0, 10) !== today) parts.push(<span key="n">today&apos;s bar is added after the close</span>);
  }
  return (
    <p className={cx("flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] text-muted", className)}>
      {parts.map((p, i) => (
        <span key={i} className="inline-flex items-center gap-1.5">{i > 0 && <span aria-hidden>·</span>}{p}</span>
      ))}
    </p>
  );
}
