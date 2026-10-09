"use client";

// /brief: the morning brief (events, rules that fired, signal changes, data health), the weekly digest, the 45-day
// calendar (holdings' ex-dates and results dates, SIPs, lots turning long-term, tax dates with the advance-tax
// estimate) and the brief's delivery settings. Everything is computed by the local API from stored data (no LLM).

import {
  BellRing, CalendarClock, CalendarDays, Download, HeartPulse, Hourglass, Landmark, Radar, Repeat, Send, Settings2, Sunrise, TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Badge, Button, Callout, Card, EmptyState, ErrorNote, InfoTip, PageHeader, SkeletonRows, Stat, Table, cx } from "@/components/ui";
import { BriefDisclosuresCard } from "@/components/brief/disclosures";
import { API_URL, api, day, when, useApi } from "@/lib/api";

import {
  type AdvanceTax, type Brief, type BriefEvent, type BriefSettings, type CalendarView, type Channel, type Digest, type ElssUnlock, type LtLot, type Sip, inr,
} from "./types";

const KIND_LABEL: Record<string, string> = {
  ipo: "IPO", corporate_action: "Ex-date", results: "Results", board_meeting: "Board", sip: "SIP", long_term: "Long-term",
  tax: "Tax", fy_end: "Tax", itr: "Tax", advance_tax: "Tax", elss_unlock: "ELSS",
};
const KIND_TONE: Record<string, "brand" | "info" | "warn" | "accent" | "neutral" | "gain"> = {
  ipo: "brand", corporate_action: "info", results: "accent", board_meeting: "neutral", sip: "gain", long_term: "warn", tax: "warn", elss_unlock: "gain",
};
const ACTION_TONE: Record<string, "gain" | "loss" | "neutral"> = { BUY: "gain", ACCUMULATE: "gain", REDUCE: "loss", SELL: "loss", AVOID: "loss" };

const TERMS = {
  twr: "Time-weighted return: the portfolio's return with new money removed, so buying more or selling does not look like a gain or a loss. Each day: (value − that day's new money) ÷ previous value, chained.",
  lt: "Long-term: held for more than 12 months (listed equity and equity funds) or 24 months (other listed securities). Equity long-term gains are taxed at 12.5 % above the ₹1.25 lakh yearly exemption; short-term at 20 % (plus 4 % cess). FIFO: the oldest lot of the same account or folio sells first.",
  advance: "Advance tax: tax paid during the year in four instalments (15 %, 45 %, 75 %, 100 % by 15 June, September, December and March) when the year's tax is ₹10,000 or more. [unverified]: long-standing rules the app could not re-read from incometax.gov.in; verify each year.",
  elss: "ELSS (tax-saver) units are locked for 3 years from the allotment of each lot: every SIP instalment and IDCW reinvestment separately (ELSS Scheme 2005). They can be redeemed from the day after the third anniversary. The fund is recognised by AMFI's category, else by its name.",
  sip: "SIPs are inferred from regular monthly purchases in your imported transactions (the statement carries no mandate). 'Missed' = no instalment for 36–65 days; 'stopped' = more than 65.",
};

function Row({ e }: { e: BriefEvent }) {
  const body = (
    <span className="flex min-w-0 items-start gap-2.5">
      <span className="num w-14 shrink-0 pt-0.5 text-[11px] text-muted">{day(e.day)?.replace(/ \d{4}$/, "")}</span>
      <Badge tone={KIND_TONE[e.kind] ?? "neutral"}>{KIND_LABEL[e.kind] ?? e.kind}</Badge>
      <span className="min-w-0 text-sm">
        {e.title}
        {e.verified === false && <span className="ml-1 text-[11px] text-warn" title={e.note}>[unverified]</span>}
      </span>
    </span>
  );
  return (
    <li className="py-2">
      {e.path && !e.path.startsWith("/brief") ? <Link href={e.path} className="block rounded-md hover:bg-background-subtle">{body}</Link> : body}
    </li>
  );
}

function Events({ items, empty }: { items: BriefEvent[]; empty: string }) {
  if (!items.length) return <p className="py-3 text-sm text-muted">{empty}</p>;
  return <ul className="divide-y divide-border/70">{items.map((e, i) => <Row key={`${e.day}-${e.kind}-${i}`} e={e} />)}</ul>;
}

function AdvanceTaxCard({ at }: { at: AdvanceTax }) {
  return (
    <div className="space-y-3">
      {at.complete === false && <p className="text-xs font-medium text-warn">Incomplete: {at.unclassified?.detail}. The figures below leave them out.</p>}
      {at.rules_note && <p className="text-xs text-warn">{at.rules_note}</p>}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div><p className="text-[11px] text-muted">Tax on realised gains</p><p className="num text-sm font-medium">{inr(at.capital_gains_tax)}</p></div>
        <div><p className="text-[11px] text-muted">Tax on dividends (slab + cess)</p><p className="num text-sm font-medium">{inr(at.dividend_tax)}</p></div>
        <div><p className="text-[11px] text-muted">Total this year</p><p className="num text-sm font-medium">{inr(at.total)}</p></div>
        <div>
          <p className="text-[11px] text-muted">By the next instalment</p>
          <p className="num text-sm font-medium">{at.next ? `${inr(at.next.amount)} by ${day(at.next.due)}` : "none left this year"}</p>
        </div>
      </div>
      {at.below_threshold && <p className="text-xs text-muted">Below the ₹{at.threshold.toLocaleString("en-IN")} threshold from these alone: no advance tax is due on their account.</p>}
      <div className="flex flex-wrap gap-1.5">
        {at.schedule.map((s) => (
          <span key={s.due} className={cx("num rounded-md px-2 py-1 text-[11px] ring-1 ring-inset ring-border", s.past ? "text-muted line-through" : "")}>
            {s.cumulative_pct}% by {day(s.due)} · {inr(s.amount)}
          </span>
        ))}
      </div>
      <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-muted">
        {at.caveats.map((c) => <li key={c}>{c}</li>)}
        <li className="text-warn">{at.note}</li>
      </ul>
    </div>
  );
}

function LtTable({ lots }: { lots: LtLot[] }) {
  if (!lots.length) return <p className="py-3 text-sm text-muted">No lot in profit turns long-term in the next 30 days.</p>;
  return (
    <Table label="Lots turning long-term soon">
      <thead><tr><th>Holding</th><th className="text-right">Turns long-term</th><th className="text-right">Gain</th><th className="text-right">Tax if sold today</th><th className="text-right">Tax after</th><th className="text-right">Saved by waiting</th></tr></thead>
      <tbody>
        {lots.map((l) => (
          <tr key={`${l.holding_id}-${l.acquired}`}>
            <td><p className="font-medium">{l.name}</p><p className="text-[11px] text-muted">{l.account} · bought {day(l.acquired)}{l.older_lots ? ` · ${l.older_lots} older lot(s) sell first (FIFO)` : ""}</p>
              {(l.older_unknown ?? 0) > 0 && <p className="text-[11px] text-warn">{l.older_unknown} of the older lots have no date or cost: their tax is unknown and they sell first</p>}
              {l.estimate && <p className="text-[11px] text-warn">Estimate: this year&apos;s unclassified disposals are left out</p>}
              {l.rules_note && <p className="text-[11px] text-warn">{l.rules_note}</p>}</td>
            <td className="num text-right">{day(l.lt_date)} <span className="text-muted">({l.days}d)</span></td>
            <td className="num text-right">{inr(l.gain)}</td>
            <td className="num text-right">{inr(l.tax_now)}</td>
            <td className="num text-right">{inr(l.tax_later)}</td>
            <td className="num text-right font-medium">{inr(l.saved)}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function ElssTable({ items }: { items: ElssUnlock[] }) {
  if (!items.length) return <p className="py-3 text-sm text-muted">No ELSS units finish their lock-in this month.</p>;
  return (
    <Table label="ELSS units unlocking this month">
      <thead><tr><th>Holding</th><th className="text-right">Unlocks</th><th className="text-right">Units</th><th className="text-right">Value today</th></tr></thead>
      <tbody>
        {items.map((x) => (
          <tr key={`${x.holding_id}-${x.day}`}>
            <td><p className="font-medium">{x.name}</p><p className="text-[11px] text-muted">{x.account}{x.verified ? "" : " · ELSS by name [unverified]"}</p></td>
            <td className="num text-right">{day(x.day)} <span className="text-muted">({x.days}d)</span></td>
            <td className="num text-right">{x.units.toLocaleString("en-IN", { maximumFractionDigits: 3 })}</td>
            <td className="num text-right">{inr(x.value)}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function SipTable({ sips }: { sips: Sip[] }) {
  if (!sips.length) return <p className="py-3 text-sm text-muted">No SIP found: three or more regular monthly purchases of a fund are needed to recognise one.</p>;
  return (
    <Table label="SIP instalments">
      <thead><tr><th>Fund</th><th className="text-right">Instalment</th><th className="text-right">Last</th><th className="text-right">Next expected</th><th>Status</th></tr></thead>
      <tbody>
        {sips.map((s) => (
          <tr key={s.holding_id}>
            <td><p className="font-medium">{s.name}</p><p className="text-[11px] text-muted">{s.account} · {s.instalments} instalments, around day {s.day_of_month}</p></td>
            <td className="num text-right">{inr(s.amount)}</td>
            <td className="num text-right">{day(s.last)}</td>
            <td className="num text-right">{day(s.next_expected)}</td>
            <td><Badge tone={s.status === "on track" ? "gain" : "warn"}>{s.status}</Badge>{s.status !== "on track" && <p className="mt-0.5 text-[11px] text-muted">{s.days_since} days since the last one</p>}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function DigestCard() {
  const { data, error, reload } = useApi<Digest>("/api/brief/digest");
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <SkeletonRows rows={4} />;
  const v = data.value;
  return (
    <div className="space-y-4">
      {!v ? (
        <EmptyState icon={<TrendingUp className="size-5" />} title="Not enough daily valuations this week">
          The monitor values your portfolio after each close while <code>finresearch serve</code> runs. The digest compares the last seven days.
        </EmptyState>
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label={<>Return, new money removed <InfoTip>{TERMS.twr}</InfoTip></>} value={v.twr_pct} format={(n) => `${n > 0 ? "+" : ""}${n.toFixed(2)}%`}
            tone={v.twr_pct >= 0 ? "gain" : "loss"} hint={`${day(v.from)} → ${day(v.to)}`} />
          <Stat label="Market move" value={v.market} format={(n) => inr(n)} tone={v.market >= 0 ? "gain" : "loss"} hint="value change minus new money" />
          <Stat label="New money" value={v.new_money} format={(n) => inr(n)} tone="info" hint="buys minus sales" />
          <Stat label="Value now" value={v.end} format={(n) => inr(n)} tone="brand" hint={`from ${inr(v.start)}`} />
        </div>
      )}
      {data.contributors && (data.contributors.top.length > 0 || data.contributors.bottom.length > 0) && (
        <div className="grid gap-3 sm:grid-cols-2">
          {(["top", "bottom"] as const).map((k) => (
            <div key={k} className="min-w-0">
              <p className="mb-1 text-xs font-medium text-muted">{k === "top" ? "Added most" : "Took away most"}</p>
              <ul className="space-y-1 text-sm">
                {data.contributors![k].map((c) => (
                  <li key={c.name} className="flex min-w-0 justify-between gap-3">
                    <span className="min-w-0 truncate">{c.name}</span>
                    <span className={cx("num shrink-0", c.inr >= 0 ? "text-gain" : "text-loss")}>{inr(c.inr)} ({c.return_pct > 0 ? "+" : ""}{c.return_pct}%)</span>
                  </li>
                ))}
                {!data.contributors![k].length && <li className="text-muted">none</li>}
              </ul>
            </div>
          ))}
        </div>
      )}
      <p className="text-[11px] text-muted">{data.method} {data.benchmark}</p>
      <p className="text-[11px] text-muted">{data.behaviour_note}</p>
    </div>
  );
}

function SettingsCard({ s, onSaved }: { s: BriefSettings; onSaved: () => void }) {
  const { data: st } = useApi<BriefSettings & { channel_status: Record<Channel, string> }>("/api/brief/settings");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const save = async (patch: Partial<BriefSettings>) => {
    setBusy(true); setMsg(null);
    try { await api("/api/brief/settings", { method: "PUT", body: JSON.stringify(patch) }); onSaved(); }
    catch (e) { setMsg((e as Error).message); }
    finally { setBusy(false); }
  };
  const toggle = (key: "enabled" | "daily_performance" | "weekly_digest", label: string, hint: string) => (
    <label className="flex items-start gap-2.5 text-sm">
      <input type="checkbox" className="mt-1 accent-[var(--brand)]" checked={s[key]} disabled={busy} onChange={(e) => save({ [key]: e.target.checked })} />
      <span><span className="font-medium">{label}</span><span className="block text-xs text-muted">{hint}</span></span>
    </label>
  );
  return (
    <div className="space-y-3">
      {toggle("enabled", "Build the brief", "08:30 IST on trading days, kept here and in the alert feed.")}
      {toggle("weekly_digest", "Weekly digest", "Sunday 09:00 IST: the week's return with new money removed, and what moved it.")}
      {toggle("daily_performance", "Daily performance line (opt-in)", "Adds yesterday's change to the brief. Off by default: frequent P&L checks tend to raise loss aversion.")}
      <div>
        <p className="mb-1 text-xs font-medium text-muted">Also push it to</p>
        <div className="flex flex-wrap gap-3">
          {(["ntfy", "telegram", "macos"] as Channel[]).map((ch) => {
            const status = st?.channel_status?.[ch];
            return (
              <label key={ch} className="flex items-center gap-1.5 text-sm" title={status === "ready" ? "ready" : `not ready: ${status ?? "…"}`}>
                <input type="checkbox" className="accent-[var(--brand)]" checked={s.channels.includes(ch)} disabled={busy}
                  onChange={(e) => save({ channels: e.target.checked ? [...s.channels, ch] : s.channels.filter((c) => c !== ch) })} />
                {ch === "macos" ? "macOS" : ch === "ntfy" ? "ntfy" : "Telegram"}
                {status && status !== "ready" && <span className="text-[11px] text-muted">({status})</span>}
              </label>
            );
          })}
        </div>
        <p className="mt-1 text-[11px] text-muted">Channels are set up on <Link href="/profile" className="underline underline-offset-2">Profile → Notifications</Link>. Quiet hours hold a push until they end.</p>
      </div>
      {msg && <ErrorNote error={msg} />}
    </div>
  );
}

export function BriefPage() {
  const { data: b, error, reload } = useApi<Brief>("/api/brief", 5 * 60000);
  const cal = useApi<CalendarView>("/api/brief/calendar");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  const send = async () => {
    setSending(true); setSent(null);
    try { await api("/api/brief/send?kind=brief", { method: "POST" }); setSent("Sent: it is in the alert feed and on the channels you chose."); reload(); }
    catch (e) { setSent((e as Error).message); }
    finally { setSending(false); }
  };
  return (
    <div className="space-y-6">
      <PageHeader icon={<Sunrise className="size-5" />} eyebrow={b ? day(b.day) ?? undefined : undefined} title="Morning brief"
        description="What is dated, what fired and what changed on your holdings, from stored data. Performance is weekly by default."
        actions={<>
          <Button variant="secondary" icon={<Send className="size-3.5" />} onClick={send} disabled={sending}>{sending ? "Sending…" : "Send now"}</Button>
          <a href={`${API_URL}/api/export/all.zip`} className="inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-medium text-muted ring-1 ring-inset ring-border transition hover:text-foreground">
            <Download className="size-3.5" />Export my data
          </a>
        </>} />
      {sent && <Callout tone="info">{sent}</Callout>}
      <ErrorNote error={error} onRetry={reload} />
      {!b && !error && <SkeletonRows rows={6} />}
      {b && (
        <>
          <Callout tone={b.rules_fired.length || b.signal_changes.length || b.sip_missed.length ? "warn" : "gain"} title="Today">
            {b.headline}
          </Callout>
          {b.performance && (
            <p className="num text-sm text-muted">Since {day(b.performance.from)}: {b.performance.twr_pct > 0 ? "+" : ""}{b.performance.twr_pct}% with new money removed (market move {inr(b.performance.market)}).</p>
          )}
          <div className="grid gap-4 lg:grid-cols-2 [&>*]:min-w-0">
            <Card title="Next 7 days" icon={<CalendarDays className="size-4" />} subtitle="Holdings' ex-dates and results meetings, watched IPOs, SIPs, lots turning long-term, tax dates.">
              <Events items={b.events} empty="Nothing dated in the next 7 days." />
            </Card>
            <Card title="Alerts since the last brief" icon={<BellRing className="size-4" />} subtitle={`Your rules and the monitor, since ${when(b.since)}.`}
              actions={<Link href="/rules" className="text-xs font-medium text-brand hover:underline">Rules →</Link>}>
              {b.rules_fired.length ? (
                <ul className="divide-y divide-border/70">
                  {b.rules_fired.map((r) => (
                    <li key={r.id} className="py-2">
                      <Link href={r.path} className="block rounded-md text-sm hover:bg-background-subtle">
                        <span className="mr-2"><Badge tone={r.level === "action" ? "loss" : r.level === "warn" ? "warn" : "info"}>{r.kind === "rule_alert" ? "your rule fired" : r.kind.replaceAll("_", " ")}</Badge></span>
                        {r.message}
                        <span className="num ml-2 text-[11px] text-muted">{when(r.at)}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : <p className="py-3 text-sm text-muted">No rule fired. No action needed.</p>}
            </Card>
            <Card title="Signal changes on holdings" icon={<Radar className="size-4" />}
              help="Each holding's signal is computed once a day after the close. The stock signal showed no edge over an equal-weight Nifty 50 backtest, so it is informational: a stock's change is a change in its factor tilt, a prompt to review, not a buy or sell call. Scheduled checks never log forecasts.">
              {b.signal_changes.length ? (
                <ul className="space-y-1.5 text-sm">
                  {b.signal_changes.map((c) => (
                    <li key={c.instrument} className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{c.name}</span>
                      {c.informational ? <>
                        <Badge tone="neutral">informational</Badge>
                        <span className="text-muted" title={`${c.label ?? "Informational — no proven edge"}: a change in the factor tilt, not a buy or sell call`}>{c.from} → {c.to}</span>
                      </> : <><Badge tone={ACTION_TONE[c.from] ?? "neutral"}>{c.from}</Badge>→<Badge tone={ACTION_TONE[c.to] ?? "neutral"}>{c.to}</Badge></>}
                    </li>
                  ))}
                </ul>
              ) : <p className="py-3 text-sm text-muted">{b.has_portfolio ? "No holding's signal changed since the last brief." : "Import your holdings on the portfolio page to see their signals here."}</p>}
            </Card>
            <Card title="Data health" icon={<HeartPulse className="size-4" />} subtitle="What the numbers above could not see.">
              {b.health.length ? (
                <ul className="space-y-1.5 text-sm">
                  {b.health.map((h) => <li key={h.text} className={cx("flex gap-2", h.level === "warn" ? "text-warn" : "text-muted")}><span>•</span><span>{h.text}</span></li>)}
                </ul>
              ) : <p className="py-3 text-sm text-muted">{b.has_portfolio ? "Every holding is priced and the daily pass is current." : "No portfolio yet."}</p>}
            </Card>
          </div>

          <BriefDisclosuresCard brief={b} />

          {b.has_portfolio && (
            <Card title="Weekly digest" icon={<TrendingUp className="size-4" />} subtitle="The last seven days, new money removed. Sent on Sundays.">
              <DigestCard />
            </Card>
          )}

          <Card title="Calendar: next 45 days" icon={<CalendarClock className="size-4" />}
            subtitle={cal.data?.events_read ? `Exchange events read ${day(cal.data.events_read)} (NSE corporate actions and board meetings).` : "Exchange events are read in the monitor's daily pass."}>
            <ErrorNote error={cal.error} onRetry={cal.reload} />
            {cal.data ? <Events items={cal.data.items} empty="Nothing dated in the next 45 days." /> : !cal.error && <SkeletonRows rows={4} />}
            {cal.data?.bse_only.length ? <p className="mt-2 text-[11px] text-muted">Not covered (BSE-only): {cal.data.bse_only.join(", ")}.</p> : null}
          </Card>

          {b.has_portfolio && (
            <div className="space-y-4 [&>*]:min-w-0">
              <Card title={<>Lots turning long-term <InfoTip>{TERMS.lt}</InfoTip></>} icon={<Hourglass className="size-4" />}
                subtitle="Tax shown before any benefit: what a sale costs today versus after the date, at today's price.">
                <LtTable lots={b.long_term} />
              </Card>
              <Card title={<>ELSS unlocks this month <InfoTip>{TERMS.elss}</InfoTip></>} icon={<Hourglass className="size-4" />}
                subtitle="Tax-saver fund units that finish their 3-year lock-in by the end of the month. Unlocked is not a reason to sell.">
                <ElssTable items={b.elss_unlocks ?? []} />
              </Card>
              <Card title={<>SIP health <InfoTip>{TERMS.sip}</InfoTip></>} icon={<Repeat className="size-4" />}
                subtitle="A SIP is a discipline, not an edge: in rising markets a lump sum does better on average.">
                <SipTable sips={b.sip} />
              </Card>
            </div>
          )}

          <div id="tax" className="scroll-mt-24">
            <Card title={<>Tax calendar and advance tax <InfoTip>{TERMS.advance}</InfoTip></>} icon={<Landmark className="size-4" />}
              subtitle="From this year's realised gains and recorded dividends only. A planning aid, not a demand: verify with a chartered accountant.">
              {b.advance_tax ? <AdvanceTaxCard at={b.advance_tax} /> : <p className="text-sm text-muted">No portfolio yet: import holdings to estimate.</p>}
              <div className="mt-4 border-t border-border pt-3">
                <Events items={b.tax_calendar} empty="No tax date in the next 120 days." />
              </div>
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2 [&>*]:min-w-0">
            <Card title="Brief settings" icon={<Settings2 className="size-4" />}>
              <SettingsCard s={b.settings} onSaved={reload} />
            </Card>
            <Card title="Earlier briefs" icon={<Sunrise className="size-4" />}>
              {b.history.length ? (
                <ul className="divide-y divide-border/70">
                  {b.history.slice(0, 10).map((h) => (
                    <li key={h.id} className="py-2 text-sm">
                      <details>
                        <summary className="cursor-pointer"><Badge tone={h.kind === "weekly_digest" ? "accent" : "brand"}>{h.kind === "weekly_digest" ? "digest" : "brief"}</Badge> <span className="num text-xs text-muted">{when(h.at)}</span></summary>
                        <pre className="mt-2 whitespace-pre-wrap font-sans text-xs text-muted">{h.message}</pre>
                      </details>
                    </li>
                  ))}
                </ul>
              ) : <p className="py-3 text-sm text-muted">None sent yet. The first arrives at 08:30 IST on the next trading day while the app runs.</p>}
            </Card>
          </div>

          <p className="text-[11px] text-muted">{b.method} {b.behaviour_note}</p>
          <p className="text-[11px] text-muted">{b.disclaimer}</p>
        </>
      )}
    </div>
  );
}
