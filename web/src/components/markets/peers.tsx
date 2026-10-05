"use client";

// Stock peer table (issue #178): the company against up to 15 listed peers of its NSE industry, with the peers'
// median, IQR and the company's percentile per metric. Peers come from the nightly build (GET /api/stocks/{symbol}/peers);
// only the company's own row is fetched live. No metric is labelled better or worse.

import { ArrowDown, ArrowUp, ArrowUpDown, Info, Users } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { crore, inr, pctOf, signedPct } from "@/components/markets/common";
import type { PeerMetricKey, PeerRow, PeerSummary, PeerValue, StockPeers } from "@/components/markets/types";
import { Badge, Callout, Card, ErrorNote, Skeleton, Table, cx } from "@/components/ui";
import { day, useApi } from "@/lib/api";

const LABEL: Record<PeerMetricKey, string> = {
  price: "Price", market_cap: "Mkt cap", pe: "P/E", pb: "P/B", roe: "ROE", revenue_growth: "Rev growth",
  pat_growth: "PAT growth", pat_margin: "PAT margin", return_1y: "1y return",
};

const HELP: Record<PeerMetricKey, string> = {
  price: "Last traded price, or the official close after the session.",
  market_cap: "Issued shares × price.",
  pe: "Price ÷ trailing EPS (sum of the latest four quarters' basic EPS, one basis). n/m when that EPS is negative or zero.",
  pb: "Market cap ÷ equity attributable to owners on the latest filed balance sheet.",
  roe: "Trailing twelve months' profit to owners ÷ average owners' equity at the start and end of those months (to the latest balance-sheet date).",
  revenue_growth: "Trailing twelve months' revenue vs the twelve months before.",
  pat_growth: "Trailing twelve months' profit to owners vs the twelve months before; n/m when the earlier figure was a loss.",
  pat_margin: "Trailing twelve months' profit to owners ÷ revenue.",
  return_1y: "Price change over a year, adjusted for splits and bonuses; dividends not added.",
};

/** One metric value as text ("n/m" when not meaningful, "—" when missing). */
export function fmtPeer(m: PeerMetricKey, v: number | null | undefined): string {
  if (v == null) return "—";
  if (m === "price") return inr(v);
  if (m === "market_cap") return crore(v);
  if (m === "pe" || m === "pb") return `${v.toFixed(1)}×`;
  if (m === "revenue_growth" || m === "pat_growth" || m === "return_1y") return signedPct(v, 1);
  return pctOf(v, 1);
}

function cell(m: PeerMetricKey, x: PeerValue | undefined): { text: string; title: string } {
  if (!x || x.value == null) {
    const r = x?.reason ?? "not available";
    return { text: r.startsWith("n/m") ? "n/m" : "—", title: r };
  }
  return { text: fmtPeer(m, x.value), title: [x.period, x.basis].filter(Boolean).join(" · ") };
}

type SortKey = PeerMetricKey | "name";

function sortRows(rows: PeerRow[], key: SortKey, desc: boolean): PeerRow[] {
  const out = [...rows];
  out.sort((a, b) => {
    if (key === "name") return (a.name ?? a.symbol).localeCompare(b.name ?? b.symbol) * (desc ? -1 : 1);
    const va = a.metrics[key]?.value, vb = b.metrics[key]?.value;
    if (va == null && vb == null) return a.symbol.localeCompare(b.symbol);
    if (va == null) return 1; // missing values always last
    if (vb == null) return -1;
    return (va - vb) * (desc ? -1 : 1) || a.symbol.localeCompare(b.symbol);
  });
  return out;
}

function SortIcon({ on, desc }: { on: boolean; desc: boolean }) {
  if (!on) return <ArrowUpDown className="size-3 opacity-50" />;
  return desc ? <ArrowDown className="size-3" /> : <ArrowUp className="size-3" />;
}

/** "Q1–Q3" of the peers, or why there is none. */
function iqr(m: PeerMetricKey, s: PeerSummary | undefined): string {
  if (!s || s.median == null) return s?.reason ?? "—";
  return `${fmtPeer(m, s.q1)} – ${fmtPeer(m, s.q3)}`;
}
