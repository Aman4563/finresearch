"use client";

// The Monitor page's "How monitoring works" box. Every time comes from GET /api/monitor/schedule (the scheduler's
// constants plus the profile's watch windows: monitor/scheduler.py schedule_json), so it never drifts from the code.

import { Info } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { Callout, Skeleton } from "@/components/ui";
import { type MonitorSchedule, useApi } from "@/lib/api";
import { refreshLabel, useTimeFrames } from "@/lib/timeframes";

function list(xs: string[]) {
  return xs.length <= 1 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`;
}

/** Times in the number font, joined in plain text ("10:30, 12:00 and 13:30"). */
function Times({ xs }: { xs: string[] }) {
  return <>{xs.map((x, i) => <span key={x}>{i > 0 && (i === xs.length - 1 ? " and " : ", ")}<T>{x}</T></span>)}</>;
}

function T({ children }: { children: ReactNode }) {
  return <span className="num font-medium text-foreground">{children}</span>;
}

function Row({ head, children }: { head: string; children: ReactNode }) {
  return (
    <li className="ml-0.5">
      <span className="font-medium text-foreground">{head}: </span>
      {children}
    </li>
  );
}

export function HowMonitoringWorks() {
  const { data: s, error } = useApi<MonitorSchedule>("/api/monitor/schedule");
  const bookS = useTimeFrames().tf.ipo_book_refresh_s;
  const settings = <Link href="/profile" className="text-brand hover:underline">Profile → Time frames &amp; watch</Link>;
  return (
    <Callout tone="info" icon={<Info className="size-4" />} title="How monitoring works">
      {!s ? (
        error ? <p className="text-xs">The schedule could not be loaded ({error}).</p> : <div className="space-y-1.5"><Skeleton className="h-3" /><Skeleton className="h-3 w-4/5" /><Skeleton className="h-3 w-3/5" /></div>
      ) : (
        <div className="space-y-2 text-xs leading-relaxed">
          <p>
            Checks run inside <code className="num">finresearch serve</code> (or <code className="num">finresearch monitor run</code>),
            one pass every <T>{s.tick_s} s</T>, on NSE/BSE trading days. Times are IST. Your check times and quiet hours come from {settings}.
            {s.running === false && <> This API was started without the monitor, so checks only run if <code className="num">finresearch monitor run</code> is running separately.</>}
          </p>
          <ul className="list-disc gap-x-8 pl-4 marker:text-muted lg:columns-2 [&>li]:mb-1.5 [&>li]:break-inside-avoid">
            <Row head="IPOs (NSE, or BSE SME)">
              subscription at <Times xs={s.ipo.check_times} /> each bidding day, with a final check at <T>{s.ipo.final_check}</T> for
              the closing book. Allotment is checked at <T>{s.ipo.allotment_time}</T> on T+1. Listing open (<T>{s.ipo.listing_times.open}</T>)
              and close (<T>{s.ipo.listing_times.close}</T>) on T+3, retried until the stock lists on its exchange. Anchor lock-ins
              ({list(s.ipo.anchor_lockin_days.map((d) => `${d}`))} days) and the six-month unlock are checked at <T>{s.ipo.lockin_time}</T>. Dates after the close are expected
              dates until the exchange confirms them.
            </Row>
            <Row head="Live book">
              during bidding hours (<T>{s.ipo.bidding_hours[0]}–{s.ipo.bidding_hours[1]}</T>), a watched IPO&apos;s page reloads the book
              {bookS > 0 ? <> every <T>{refreshLabel(bookS)}</T></> : " only when you ask (auto-refresh is off)"}; each new exchange timestamp is saved.
            </Row>
            <Row head="Book archive">
              the category book of every open NSE issue, watched or not, at <Times xs={s.ipo.archive_times} /> (each within {s.ipo.archive_window_min} minutes).
              BSE-only SME issues are not archived.
            </Row>
            <Row head="Stocks">
              one check a trading day at <T>{s.stock.daily_time}</T> from the stock&apos;s own exchange (NSE, or BSE for a BSE-only stock):
              results filings, corporate actions and ex-dates, promoter holding changes and large moves.
            </Row>
            <Row head="After the close">
              from <T>{s.intraday.from}</T> the day&apos;s 1-minute prices of watched stocks (NSE or BSE), {list(s.intraday.indices)} and
              anything viewed today are saved for multi-day charts (up to {s.intraday.max_tries} tries, {s.intraday.retry_min} minutes apart).
              From <T>{s.iv.from}</T> the at-the-money implied volatility of {list(s.iv.indices)} and watched NSE F&amp;O stocks is recorded
              (BSE-only stocks have no options).
            </Row>
            {s.portfolio && (
              <Row head="Portfolio">
                on trading days at <T>{s.portfolio.close_pass}</T> your holdings are valued (the daily snapshot behind the portfolio alerts),
                each holding&apos;s signal is computed (never logged in the forecast ledger) and its corporate actions, results dates and fund
                TER are read (at most {s.portfolio.max_instruments} instruments); funds are re-valued at <T>{s.portfolio.nav_pass}</T> with the
                day&apos;s NAVs. The <a href="/brief" className="underline underline-offset-2">morning brief</a> is built at <T>{s.portfolio.brief}</T> and
                the weekly digest on {s.portfolio.digest_day} at <T>{s.portfolio.digest}</T>.
              </Row>
            )}
            {s.lookthrough && (
              <Row head="Fund look-through">
                fund houses publish each month-end portfolio within {s.lookthrough.sebi_days} days (SEBI); from day {s.lookthrough.from_day} to
                day {s.lookthrough.to_day} of the month, once a day after <T>{s.lookthrough.after}</T>, the latest file of every held fund whose
                fund house the app can read is fetched (nothing once it is stored); the log says why for each other fund.
              </Row>
            )}
            <Row head="Forecasts">
              signals and verdicts logged in the forecast ledger are scored once their date has passed, checked every {s.forecasts.every_min} minutes
              after <T>{s.forecasts.after}</T>.
            </Row>
            <Row head="Quiet hours">
              {s.quiet.start && s.quiet.end
                ? <><T>{s.quiet.start}–{s.quiet.end}</T>: information alerts are still recorded but don&apos;t ring the bell; warnings and actions still do.</>
                : "off: every unread alert rings the bell."}
            </Row>
          </ul>
          <p className="text-muted">A check more than {s.grace_hours} hours late (the monitor was off) is marked missed rather than run with late data.</p>
        </div>
      )}
    </Callout>
  );
}
