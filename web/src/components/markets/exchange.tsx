"use client";

// NSE / BSE on the stock pages: the exchange switch for dual-listed stocks, the live NSE-vs-BSE price comparison,
// and the exchange badge shown on search results.

import { ArrowLeftRight } from "lucide-react";
import Link from "next/link";

import { LiveStamp, useLive } from "@/components/live";
import { Metric, inr } from "@/components/markets/common";
import type { StockHit, StockListing, StockOverview } from "@/components/markets/types";
import { Badge, Card, cx } from "@/components/ui";

type LiveQuote = { quote: NonNullable<StockOverview["quote"]>; fetched_at: string; exchange: "NSE" | "BSE" };

/** "NSE + BSE" / "BSE only" / "NSE" for a search hit or a listing. */
export function ExchangeBadge({ exchange, group }: { exchange: StockHit["exchange"]; group?: string | null }) {
  if (exchange === "both") return <Badge tone="info">NSE + BSE</Badge>;
  if (exchange === "BSE") {
    const sme = ["M", "MT", "MS"].includes((group ?? "").toUpperCase());
    return <Badge tone="accent">{sme ? "BSE SME" : "BSE only"}</Badge>;
  }
  return <Badge tone="neutral">NSE</Badge>;
}

/** NSE | BSE links between the two views of a dual-listed stock (each view has its own URL). */
export function ExchangeSwitch({ listing, current }: { listing: StockListing | null | undefined; current: "NSE" | "BSE" }) {
  if (!listing || listing.exchange !== "both" || !listing.nse_symbol || !listing.bse_key) return null;
  const opts = [
    { ex: "NSE" as const, href: `/stocks/${encodeURIComponent(listing.nse_symbol)}` },
    { ex: "BSE" as const, href: `/stocks/${encodeURIComponent(listing.bse_key)}` },
  ];
  return (
    <span role="tablist" aria-label="Exchange" className="inline-flex rounded-md bg-background-subtle p-0.5 ring-1 ring-inset ring-border">
      {opts.map((o) => (
        <Link key={o.ex} href={o.href} role="tab" aria-selected={o.ex === current}
          className={cx("rounded px-2 py-0.5 text-[11px] font-semibold tracking-normal normal-case transition",
            o.ex === current ? "bg-card text-foreground shadow-sm" : "text-muted hover:text-foreground")}>
          {o.ex}
        </Link>
      ))}
    </span>
  );
}

/** Last price on NSE and on BSE side by side, and the gap between them. Refreshes every 30 s in market hours. */
export function PriceComparison({ listing }: { listing: StockListing }) {
  const nse = useLive<LiveQuote>(listing.nse_symbol ? `/api/stocks/${encodeURIComponent(listing.nse_symbol)}/quote` : null,
    { session: "equity", everyMs: 30000 });
  const bse = useLive<LiveQuote>(listing.bse_key ? `/api/stocks/${encodeURIComponent(listing.bse_key)}/quote` : null,
    { session: "equity", everyMs: 30000 });
  const n = nse.data?.quote.last_price != null ? Number(nse.data.quote.last_price) : null;
  const b = bse.data?.quote.last_price != null ? Number(bse.data.quote.last_price) : null;
  const gap = n != null && b != null ? b - n : null;
  const gapPct = gap != null && n ? (gap / n) * 100 : null;
  return (
    <Card title="NSE vs BSE" icon={<ArrowLeftRight className="size-4" />}
      help="The same shares trade on both exchanges. Prices usually sit within a few paise of each other; a wider gap means one side is thinly traded right now. Buy where the price is better after brokerage.">
      <dl className="grid [&>*]:min-w-0 grid-cols-3 gap-2">
        <Metric label="NSE" value={nse.error && !nse.data ? "—" : inr(n)} sub={nse.error && !nse.data ? "unavailable" : undefined} />
        <Metric label="BSE" value={bse.error && !bse.data ? "—" : inr(b)} sub={bse.error && !bse.data ? "unavailable" : undefined} />
        <Metric label="Spread" help="BSE's last price minus NSE's, in rupees and as a share of the NSE price."
          value={gap == null ? "—" : `${gap > 0 ? "+" : gap < 0 ? "−" : ""}${inr(Math.abs(gap))}`}
          sub={gapPct == null ? undefined : `${gapPct > 0 ? "+" : ""}${gapPct.toFixed(3)}%`} />
      </dl>
      <LiveStamp session="equity" live={nse.live} status={nse.status} updatedAt={bse.updatedAt ?? nse.updatedAt} everyMs={30000}
        asOf={bse.data?.quote.as_of} asOfLabel="BSE as of" onRefresh={() => { nse.reload(); bse.reload(); }} className="mt-3" />
    </Card>
  );
}
