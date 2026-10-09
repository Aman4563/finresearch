"use client";

// The dashboard's portfolio strip: value, the week's return with new money removed, LTCG headroom and the latest
// portfolio alert. It reads /api/dashboard/portfolio, which uses stored data only (no quotes are fetched and no
// snapshot is written by opening the dashboard). No daily P&L, by design (see the brief's behaviour note).

import { BellRing, Briefcase, Landmark, Sunrise, TrendingUp } from "lucide-react";
import Link from "next/link";

import { type Strip, inr } from "@/components/brief/types";
import { istDate } from "@/components/ipo/lib";
import { InfoTip, Skeleton, cx } from "@/components/ui";
import { day, useApi } from "@/lib/api";

/** One linked cell. The link is stretched under the content rather than wrapped around it, so the (?) buttons and
 * the net-worth link inside stay valid HTML (no <a>/<button> inside <a>, which React reports as a hydration error). */
function Cell({ icon, label, name, children, hint, href }: { icon: React.ReactNode; label: React.ReactNode; name: string; children: React.ReactNode; hint?: React.ReactNode; href: string }) {
  return (
    <div className="group relative min-w-0 rounded-xl px-3 py-2.5 transition hover:bg-background-subtle">
      <Link href={href} className="absolute inset-0 rounded-xl"><span className="sr-only">{name}</span></Link>
      <div className="pointer-events-none relative [&_a]:pointer-events-auto [&_button]:pointer-events-auto">
        <p className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-muted [&_svg]:size-3.5">{icon}{label}</p>
        <div className="mt-1 truncate text-lg font-semibold tracking-tight">{children}</div>
        {/* up to two lines: on a phone the cell is half the screen wide and one line cut the hint mid-word */}
        {hint && <p className="line-clamp-2 text-[11px] text-muted">{hint}</p>}
      </div>
    </div>
  );
}

export function PortfolioStrip() {
  const { data, error } = useApi<Strip>("/api/dashboard/portfolio", 5 * 60000);
  if (error) return null; // the rest of the dashboard does not depend on it
  if (!data) return <Skeleton className="h-[84px] rounded-2xl" />;
  if (!data.has_portfolio) {
    return (
      <Link href="/portfolio" className="flex items-center gap-3 rounded-2xl border border-dashed border-border bg-card/60 px-4 py-3 text-sm text-muted transition hover:border-border-strong hover:text-foreground">
        <Briefcase className="size-4 text-brand" />
        Import your holdings (CAS or tradebook) to see your portfolio here, with a daily valuation, portfolio alerts and a morning brief.
      </Link>
    );
  }
  const w = data.week;
  return (
    <section aria-label="Your portfolio" className="grid grid-cols-2 gap-1 rounded-2xl border border-border bg-card p-1.5 shadow-card animate-fade-up lg:grid-cols-4">
      <Cell href="/portfolio" icon={<Briefcase />} label="Portfolio" name="Portfolio"
        // always say when the value is from: it is the last stored valuation, which can differ from the Portfolio
        // page's live-priced total
        hint={<>
          {data.as_of ? `valued ${day(data.as_of)}${data.complete ? "" : " · some holdings unpriced"}` : "not valued yet"}
          {data.net_worth && <> · net worth <Link href="/wealth" className="num hover:underline">{inr(data.net_worth.net_worth)}</Link></>}
        </>}>
        <span className="num">{inr(data.value)}</span>
      </Cell>
      <Cell href="/brief" icon={<TrendingUp />} name="This week"
        label={<>This week <InfoTip>Return over the last seven days with new money removed (time-weighted), from the daily valuations. Daily P&amp;L is not shown by default: frequent checks tend to raise loss aversion.</InfoTip></>}
        hint={w ? `market move ${inr(w.market)} · new money ${inr(w.new_money)}` : data.week_why ?? "needs two daily valuations"}>
        {w ? <span className={cx("num", w.twr_pct >= 0 ? "text-gain" : "text-loss")}>{w.twr_pct > 0 ? "+" : ""}{w.twr_pct.toFixed(2)}%</span>
          : <span className="text-sm text-muted" title={data.week_why ?? undefined}>{data.week_why ? "unavailable" : "—"}</span>}
      </Cell>
      <Cell href="/portfolio" icon={<Landmark />} name="LTCG headroom"
        label={<>LTCG headroom <InfoTip>How much of this financial year&apos;s ₹1.25 lakh exemption on equity long-term gains is still unused.</InfoTip></>}
        hint={data.ltcg_headroom === null ? "tax incomplete: a sale can't be classified (Tax tab)" : data.ltcg_limit ? `of ${inr(data.ltcg_limit)} this year` : undefined}>
        {data.ltcg_headroom === null ? <span className="text-warn">unknown</span> : <span className="num">{inr(data.ltcg_headroom)}</span>}
      </Cell>
      <Cell href={data.top_alert ? "/monitor" : "/brief"} name={data.top_alert ? "Your rule fired" : "Alerts"} icon={data.top_alert ? <BellRing /> : <Sunrise />} label={data.top_alert ? "Your rule fired" : "Alerts"}
        hint={data.top_alert ? day(istDate(Date.parse(data.top_alert.at))) : data.unpriced ? `${data.unpriced} holding(s) without a fresh price` : "morning brief →"}>
        <span className={cx("line-clamp-2 block text-sm font-medium whitespace-normal", data.top_alert ? "text-warn" : "text-muted")} title={data.top_alert?.message}>
          {data.top_alert ? data.top_alert.message.replace(/^Portfolio: /, "") : "No portfolio rule fired this week"}
        </span>
      </Cell>
    </section>
  );
}
