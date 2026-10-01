"use client";

// Exchange disclosures (finresearch.disclosures): surveillance and pledge red flags, insider / SAST / bulk and block deals,
// credit-rating actions and SEBI orders. Every part shows its source and as-of date; a source that could not be read
// says "unavailable", never "none" (DATA-004).

import { ExternalLink, Gavel, ShieldAlert, Users } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { crore, inr } from "@/components/markets/common";
import { Badge, Card, cx, InfoTip, SkeletonRows, Table } from "@/components/ui";
import { day, useApi, useRetryApi, when } from "@/lib/api";

type Tone = "neutral" | "brand" | "gain" | "loss" | "warn" | "info" | "accent";

export type FeedState = {
  state: "ok" | "unavailable"; source: string; source_url: string | null; as_of: string | null; fetched_at: string | null;
  reason?: string | null; error?: string | null;
};
export type Flag = { kind: string; label: string; tone: Tone; as_of: string | null; source_url: string | null; stale?: boolean; code?: string | null };
type SurvEntry = { framework: string; symbol: string; term?: string | null; stage?: string | null; code?: string | null; ibc?: boolean; esm?: string | null };
type PledgeLatest = {
  quarter_end: string | null; pct_of_promoter: string | null; pct_of_equity: string | null; promoter_pct: string | null;
  encumbered_shares: string | null; disclosed_shares: string | null; disclosed_pct_reported: string | null; mismatch: string | null;
  broadcast_at: string | null;
};
type Net = { start: string; end: string; buy_value: string; sell_value: string; net_value: string; n_counted: number;
  by_group: Record<string, string>; excluded: Record<string, number>; missing_value: number };
type Trade = { day: string; person: string | null; category: string | null; mode: string | null; side: string | null;
  quantity: string | null; value_inr: string | null; counted: boolean; excluded_why: string | null; filing_url: string | null };
type Sast = { acquirer: string | null; kind: string | null; regulation: string | null; promoter: boolean | null; mode: string | null;
  day: string | null; shares: string | null; pct: string | null; pct_after: string | null; attachment: string | null };
type Deal = { kind: string; day: string | null; client: string | null; side: string | null; quantity: string | null; price: string | null };
export type RatingRow = { isin: string | null; company: string | null; agency: string | null; rating: string | null; outlook: string | null;
  watch: string | null; action: string; action_raw: string | null; adverse: boolean; rating_day: string | null; earlier_rating: string | null;
  derived: string | null; scope?: string };
type Order = { title: string; link: string; day: string | null; kind: string; matched_name: string };

export type StockDisclosures = {
  symbol: string; covered: boolean; note?: string; isin?: string | null; flags?: Flag[]; stage?: string | null; unavailable?: string[];
  surveillance?: { asm: FeedState & { entries: SurvEntry[] }; gsm: FeedState & { entries: SurvEntry[] };
    fno_ban: FeedState & { trade_date: string | null; in_ban: boolean | null } };
  pledge?: FeedState & { latest: PledgeLatest | null; no_record: boolean; change_pp: string | null; prev_quarter: string | null;
    history: { quarter_end: string; pct_of_promoter: string | null; pct_of_equity: string | null }[]; basis: string };
  insider?: FeedState & { complete: boolean; problems: string[]; window: [string, string]; net: Net | null; partial_net: Net | null; trades: Trade[]; basis: string };
  sast?: FeedState & { rows: Sast[] };
  deals?: FeedState & { rows: Deal[]; recent_n: number };
  ratings?: FeedState & { actions: RatingRow[]; latest_adverse: RatingRow | null; issuer_code: string | null; basis: string };
  sebi?: FeedState & { orders: Order[]; basis: string };
};

const ACTION_TONE: Record<string, Tone> = {
  downgrade: "loss", default: "loss", not_cooperating: "loss", watch_negative: "loss", outlook_negative: "warn",
  upgrade: "gain", outlook_positive: "gain", watch_positive: "gain", withdrawn: "neutral", reaffirm: "neutral", new: "info",
  other: "neutral", watch_developing: "warn",
};
export const actionLabel = (a: string) => a.replaceAll("_", " ");
const num = (x: string | null | undefined) => (x == null || x === "" ? null : Number(x));
const pct2 = (x: string | null | undefined) => (num(x) == null ? "—" : `${num(x)!.toFixed(2)}%`);
const qty = (x: string | null | undefined) => (num(x) == null ? "—" : num(x)!.toLocaleString("en-IN"));

/** "As of <date> · source" with a link, or the reason a source is unavailable. */
export function SourceLine({ st, what }: { st: FeedState | undefined; what?: string }) {
  if (!st) return null;
  const link = st.source_url ? (
    <a href={st.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-brand hover:underline">
      {st.source}<ExternalLink className="size-3" />
    </a>
  ) : st.source;
  if (st.state !== "ok") {
    return (
      <p role="status" className="text-[11px] text-warn">
        {what ? `${what}: ` : ""}unavailable ({st.reason ?? "not read"}). Not the same as &ldquo;none&rdquo;. Source: {link}
      </p>
    );
  }
  return (
    <p className="text-[11px] text-muted">
      As of {st.as_of ? day(st.as_of) : "—"} · read {when(st.fetched_at)} · {link}
      {st.error ? <span className="text-warn"> · the latest refresh failed ({st.error.slice(0, 80)})</span> : null}
    </p>
  );
}

export function FlagBadges({ flags, empty }: { flags: Flag[]; empty?: ReactNode }) {
  if (!flags.length) return <>{empty ?? null}</>;
  return (
    <span className="inline-flex flex-wrap gap-1.5">
      {flags.map((f) => (
        <span key={`${f.kind}-${f.label}`} title={`${f.code ? `${f.code} · ` : ""}as of ${f.as_of ?? "?"}${f.stale ? " (stale)" : ""}`}>
          <Badge tone={f.tone}>{f.label}{f.stale ? " · stale" : ""}</Badge>
        </span>
      ))}
    </span>
  );
}

function Section({ title, help, children }: { title: ReactNode; help?: ReactNode; children: ReactNode }) {
  return (
    <div className="min-w-0 space-y-2">
      <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted">{title}{help && <InfoTip>{help}</InfoTip>}</h3>
      {children}
    </div>
  );
}

function RedFlagsBlock({ d }: { d: StockDisclosures }) {
  const s = d.surveillance!;
  const p = d.pledge!;
  const latest = p.latest;
  const okAll = s.asm.state === "ok" && s.gsm.state === "ok";
  return (
    <Section title="Red flags" help="NSE's Additional (ASM) and Graded (GSM) Surveillance Measures put a stock on higher margins or trade-for-trade settlement, which makes an exit costlier; IBC marks an insolvency process. A stock in the F&O ban can only have positions reduced. Promoter pledges can force sales if the price falls. Screening flags, not advice.">
      <FlagBadges flags={d.flags ?? []} empty={okAll && s.fno_ban.state === "ok" && p.state === "ok"
        ? <Badge tone="gain">No surveillance, ban or pledge flag</Badge>
        : <span className="text-xs text-warn">Some sources are unavailable: no flag shown does not mean none.</span>} />
      <div className="grid gap-1 text-xs sm:grid-cols-2">
        <div><span className="text-muted">ASM:</span> {s.asm.state !== "ok" ? <span className="text-warn">unavailable</span> : s.asm.entries.length ? s.asm.entries.map((e) => `${e.term} Stage ${e.stage}`).join(", ") : "not listed"}</div>
        <div><span className="text-muted">GSM:</span> {s.gsm.state !== "ok" ? <span className="text-warn">unavailable</span> : s.gsm.entries.length ? s.gsm.entries.map((e) => e.code).join(", ") : "not listed"}</div>
        <div><span className="text-muted">F&amp;O ban:</span> {s.fno_ban.in_ban == null ? <span className="text-warn">unavailable for today</span> : s.fno_ban.in_ban ? `in ban for ${day(s.fno_ban.trade_date)}` : `not in ban (${day(s.fno_ban.trade_date)})`}</div>
        <div>
          <span className="text-muted">Promoter pledge:</span>{" "}
          {p.state !== "ok" ? <span className="text-warn">unavailable</span> : p.no_record || !latest ? "no NSE record" : (
            <>
              <span className="num">{pct2(latest.pct_of_promoter)}</span> of promoter holding (<span className="num">{pct2(latest.pct_of_equity)}</span> of equity), quarter to {day(latest.quarter_end)}
              {p.change_pp != null ? <span className={cx("num", Number(p.change_pp) > 0 ? "text-warn" : "text-muted")}> · {Number(p.change_pp) > 0 ? "+" : ""}{Number(p.change_pp).toFixed(2)} pp vs {day(p.prev_quarter)}</span> : <span className="text-muted"> · change: needs two recorded quarters</span>}
              {latest.mismatch && <span className="text-warn"> · NSE&apos;s own % differs ({latest.mismatch})</span>}
            </>
          )}
        </div>
      </div>
      <div className="space-y-0.5">
        <SourceLine st={s.asm} what="ASM" /><SourceLine st={s.gsm} what="GSM" /><SourceLine st={s.fno_ban} what="F&O ban" /><SourceLine st={p} what="Pledge" />
      </div>
    </Section>
  );
}

function InsiderBlock({ d }: { d: StockDisclosures }) {
  const ins = d.insider!;
  const n = ins.net ?? ins.partial_net;
  return (
    <Section title="Insider trades (90 days)" help={ins.basis}>
      {ins.state === "ok" && n ? (
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
          <span>Net <span className={cx("num font-semibold", Number(n.net_value) > 0 ? "text-gain" : Number(n.net_value) < 0 ? "text-loss" : "")}>{crore(n.net_value, 2)}</span>{!ins.complete && <span className="text-warn"> (incomplete)</span>}</span>
          <span className="text-xs text-muted">bought {crore(n.buy_value, 2)} · sold {crore(n.sell_value, 2)} · {n.n_counted} open-market trade{n.n_counted === 1 ? "" : "s"}</span>
          <span className="text-xs text-muted">promoters {crore(n.by_group.promoter, 2)} · directors/KMP {crore(n.by_group.director_kmp, 2)}</span>
          {Object.keys(n.excluded).length > 0 && <span className="text-xs text-muted">excluded: {Object.entries(n.excluded).map(([k, v]) => `${v} ${k}`).join(", ")}</span>}
        </div>
      ) : null}
      {ins.problems.length > 0 && <p className="text-xs text-warn">{ins.problems.join("; ")}: the net figure is not final.</p>}
      {ins.state === "ok" && ins.trades.length === 0 && ins.complete && <p className="text-xs text-muted">No insider filings in the window.</p>}
      {ins.trades.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted hover:text-foreground">{ins.trades.length} disclosed transaction{ins.trades.length === 1 ? "" : "s"}</summary>
          <Table className="mt-2">
            <thead><tr><th>Date</th><th>Who</th><th>Mode</th><th className="text-right">Shares</th><th className="text-right">Value</th><th>Counted</th></tr></thead>
            <tbody>
              {ins.trades.map((t, i) => (
                <tr key={`${t.day}-${i}`}>
                  <td className="num">{day(t.day)}</td>
                  <td><span className="font-medium">{t.person ?? "—"}</span><span className="block text-[11px] text-muted">{t.category}</span></td>
                  <td>{t.mode} {t.side ? <span className="text-muted">({t.side})</span> : null}</td>
                  <td className="num text-right">{qty(t.quantity)}</td>
                  <td className="num text-right">{inr(t.value_inr, 0)}</td>
                  <td>{t.counted ? <Badge tone="info">yes</Badge> : <span className="text-muted">no: {t.excluded_why}</span>}{t.filing_url && <a href={t.filing_url} target="_blank" rel="noreferrer" className="ml-1 text-brand hover:underline">filing</a>}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </details>
      )}
      <SourceLine st={ins} what="Insider filings" />
    </Section>
  );
}

function DealsBlock({ d }: { d: StockDisclosures }) {
  const sa = d.sast!, dl = d.deals!;
  return (
    <Section title="Substantial acquisitions and deals" help="SAST Regulation 29: a holder crossing 5 %, or a 5 %+ holder changing by 2 %. Bulk deals: a client trading over 0.5 % of the shares in a day; block deals: large trades in the block window. Client names are as NSE publishes them.">
      {sa.state === "ok" && sa.rows.length === 0 && <p className="text-xs text-muted">No SAST Reg 29 disclosure in the last year.</p>}
      {sa.rows.length > 0 && (
        <ul className="space-y-1 text-xs">
          {sa.rows.slice(0, 6).map((r, i) => (
            <li key={i} className="flex flex-wrap gap-x-2">
              <span className="num text-muted">{day(r.day)}</span>
              <Badge tone={r.kind === "Sale" ? "warn" : "info"}>{r.kind ?? "?"}</Badge>
              <span className="font-medium">{r.acquirer}</span>{r.promoter ? <Badge tone="accent">promoter</Badge> : null}
              <span className="num">{qty(r.shares)} sh ({pct2(r.pct)}; {pct2(r.pct_after)} after)</span>
              <span className="text-muted">{r.mode} · {r.regulation}</span>
              {r.attachment && <a href={r.attachment} target="_blank" rel="noreferrer" className="text-brand hover:underline">filing</a>}
            </li>
          ))}
        </ul>
      )}
      <SourceLine st={sa} what="SAST" />
      {dl.state === "ok" && dl.rows.length === 0 && <p className="text-xs text-muted">No bulk or block deal in the last 90 days.</p>}
      {dl.rows.length > 0 && (
        <Table className="mt-1">
          <thead><tr><th>Date</th><th>Kind</th><th>Client</th><th>Side</th><th className="text-right">Shares</th><th className="text-right">Price</th></tr></thead>
          <tbody>
            {dl.rows.slice(0, 12).map((r, i) => (
              <tr key={i}>
                <td className="num">{day(r.day)}</td><td>{r.kind}</td><td>{r.client}</td>
                <td><Badge tone={r.side === "BUY" ? "gain" : "loss"}>{r.side}</Badge></td>
                <td className="num text-right">{qty(r.quantity)}</td><td className="num text-right">{inr(r.price)}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <SourceLine st={dl} what="Bulk/block deals" />
    </Section>
  );
}

export function RatingTable({ rows }: { rows: RatingRow[] }) {
  return (
    <Table>
      <thead><tr><th>Rated</th><th>Agency</th><th>Rating</th><th>Action</th><th>Outlook</th><th>Instrument</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={`${r.isin}-${r.rating_day}-${i}`}>
            <td className="num">{day(r.rating_day)}</td>
            <td>{r.agency}</td>
            <td>{r.rating ?? "—"}{r.earlier_rating && r.earlier_rating !== r.rating ? <span className="block text-[11px] text-muted">was {r.earlier_rating}</span> : null}</td>
            <td><Badge tone={ACTION_TONE[r.action] ?? "neutral"}>{actionLabel(r.action)}</Badge>{r.derived ? <span className="block text-[11px] text-muted">{r.derived}</span> : r.action_raw ? <span className="block text-[11px] text-muted">filed: {r.action_raw}</span> : null}</td>
            <td>{r.watch ? `watch ${r.watch.toLowerCase()}` : r.outlook ?? "—"}</td>
            <td className="num text-[11px]">{r.isin}{r.scope ? <span className="block text-muted">{r.scope}</span> : null}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function OrdersList({ orders }: { orders: Order[] }) {
  return (
    <ul className="space-y-1 text-xs">
      {orders.map((o) => (
        <li key={o.link} className="flex flex-wrap gap-x-2">
          <span className="num text-muted">{day(o.day)}</span><Badge tone="warn">possible match</Badge>
          <a href={o.link} target="_blank" rel="noreferrer" className="min-w-0 text-brand hover:underline">{o.title}</a>
        </li>
      ))}
    </ul>
  );
}

function RegulatoryBlock({ d }: { d: StockDisclosures }) {
  const r = d.ratings!, sb = d.sebi!;
  return (
    <Section title="Credit ratings and SEBI orders" help={`${r.basis} ${sb.basis}`}>
      {r.state === "ok" && !r.issuer_code && <p className="text-xs text-muted">No ISIN known for this stock, so rating filings cannot be matched.</p>}
      {r.state === "ok" && r.issuer_code && r.actions.length === 0 && <p className="text-xs text-muted">No rating filing by this issuer in the last year.</p>}
      {r.actions.length > 0 && <RatingTable rows={r.actions.slice(0, 8)} />}
      <SourceLine st={r} what="Rating filings" />
      {sb.orders.length > 0 ? <OrdersList orders={sb.orders} /> : sb.state === "ok" ? <p className="text-xs text-muted">No SEBI order in SEBI&apos;s latest feed names this company.</p> : null}
      <SourceLine st={sb} what="SEBI orders" />
    </Section>
  );
}

/** The stock page card: red flags, insider and deal activity, ratings and SEBI orders. */
export function StockDisclosuresCard({ symbol, isin }: { symbol: string; isin?: string | null }) {
  const q = isin ? `?isin=${encodeURIComponent(isin)}` : "";
  const { data, error, retry } = useRetryApi<StockDisclosures>(`/api/stocks/${encodeURIComponent(symbol)}/disclosures${q}`);
  if (data && !data.covered) {
    return <Card title="Exchange disclosures" icon={<ShieldAlert className="size-4" />}><p className="text-sm text-muted">{data.note}</p></Card>;
  }
  return (
    <Card title="Red flags, insiders and regulatory" icon={<ShieldAlert className="size-4" />}
      subtitle="NSE surveillance and pledge data, insider and SAST filings, bulk/block deals, rating filings and SEBI orders"
      actions={data?.unavailable?.length ? <button type="button" onClick={retry} className="text-xs font-medium text-brand hover:underline">Retry</button> : null}>
      {error ? <p className="text-sm text-warn">Disclosures unavailable: {error}</p> : !data ? <SkeletonRows rows={5} /> : (
        <div className="grid gap-6 lg:grid-cols-2 [&>*]:min-w-0">
          <RedFlagsBlock d={data} />
          <InsiderBlock d={data} />
          <DealsBlock d={data} />
          <RegulatoryBlock d={data} />
        </div>
      )}
    </Card>
  );
}

/** The bond page card: rating actions on the bond and its issuer, SEBI orders that may name the issuer. */
export function BondRatingActionsCard({ isin }: { isin: string }) {
  const { data, error } = useApi<{ isin: string; ratings: NonNullable<StockDisclosures["ratings"]>; sebi: NonNullable<StockDisclosures["sebi"]> }>(
    `/api/bonds/${encodeURIComponent(isin)}/rating-actions`);
  return (
    <Card title="Rating actions" icon={<Gavel className="size-4" />}
      subtitle="The agency's action and outlook from the issuer's rating filings on NSE (this bond and the issuer's other instruments)"
      help={data?.ratings.basis}>
      {error ? <p className="text-sm text-warn">Rating filings unavailable: {error}</p> : !data ? <SkeletonRows rows={3} /> : (
        <div className="space-y-2">
          {data.ratings.latest_adverse && (
            <p className="text-sm text-loss">Latest adverse action: {data.ratings.latest_adverse.agency}, {actionLabel(data.ratings.latest_adverse.action)} to {data.ratings.latest_adverse.rating ?? "—"} ({day(data.ratings.latest_adverse.rating_day)})</p>
          )}
          {data.ratings.actions.length ? <RatingTable rows={data.ratings.actions.slice(0, 10)} />
            : data.ratings.state === "ok" ? <p className="text-sm text-muted">No rating filing for this issuer in NSE&apos;s recent list (it covers listed issuers&apos; filings; history builds up daily).</p> : null}
          <SourceLine st={data.ratings} what="Rating filings" />
          {data.sebi.orders.length > 0 && <OrdersList orders={data.sebi.orders} />}
          <SourceLine st={data.sebi} what="SEBI orders" />
        </div>
      )}
    </Card>
  );
}

type Tracked = {
  stocks: { key: string; name: string | null; held: boolean; watched: boolean; flags: Flag[]; states: Record<string, string>;
    insider_net_90d: string | null; insider_complete: boolean; deals_recent: number; rating_adverse: RatingRow | null;
    sebi_orders: Order[] }[];
  bonds: { isin: string; name: string | null; held: boolean; tracked: boolean; latest_adverse: RatingRow | null; rating_state: string }[];
  unavailable: string[]; bse_only: string[];
};

/** Red flags across holdings ("held") or the watchlist ("watched"), from the monitor's daily refresh (no network). */
export function TrackedRedFlags({ only }: { only: "held" | "watched" }) {
  const { data } = useApi<Tracked>("/api/disclosures/tracked");
  if (!data) return null;
  const rows = data.stocks.filter((x) => x[only]);
  const bonds = only === "held" ? data.bonds.filter((b) => b.held && b.latest_adverse) : [];
  const flagged = rows.filter((x) => x.flags.length || x.rating_adverse || x.sebi_orders.length);
  const gaps = rows.filter((x) => Object.values(x.states).some((s) => s !== "ok"));
  if (!rows.length && !bonds.length) return null;
  return (
    <div className={cx("rounded-lg border px-3 py-2 text-xs", flagged.length || bonds.length ? "border-warn/40 bg-warn-soft/40" : "border-border")}>
      <p className="mb-1 flex items-center gap-1.5 font-medium"><Users className="size-3.5" /> Red flags on {only === "held" ? "holdings" : "the watchlist"}
        <InfoTip>NSE ASM/GSM surveillance, the F&amp;O ban list, promoter pledge (rising), adverse rating actions and SEBI orders that may name the company. Refreshed by the monitor after each close and before the open.</InfoTip>
      </p>
      {flagged.length === 0 && bonds.length === 0 ? (
        <p className="text-muted">{gaps.length ? `No flag found, but ${gaps.length} stock(s) have an unavailable source: not the same as none.` : "No flag on any of them."}</p>
      ) : (
        <ul className="space-y-1">
          {flagged.map((x) => (
            <li key={x.key} className="flex flex-wrap items-center gap-1.5">
              <Link href={`/stocks/${encodeURIComponent(x.key)}`} className="font-medium hover:underline">{x.key}</Link>
              <FlagBadges flags={x.flags} />
              {x.rating_adverse && <Badge tone="loss">{x.rating_adverse.agency?.split(" ")[0]}: {actionLabel(x.rating_adverse.action)}</Badge>}
              {x.sebi_orders.length > 0 && <Badge tone="warn">SEBI order (possible match)</Badge>}
            </li>
          ))}
          {bonds.map((b) => (
            <li key={b.isin} className="flex flex-wrap items-center gap-1.5">
              <Link href={`/bonds/${b.isin}`} className="font-medium hover:underline">{b.name ?? b.isin}</Link>
              <Badge tone="loss">{b.latest_adverse!.agency?.split(" ")[0]}: {actionLabel(b.latest_adverse!.action)}</Badge>
            </li>
          ))}
        </ul>
      )}
      {data.unavailable.length > 0 && <p className="mt-1 text-warn">Unavailable: {data.unavailable.join(", ")}.</p>}
    </div>
  );
}
