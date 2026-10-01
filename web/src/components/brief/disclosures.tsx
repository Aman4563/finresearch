"use client";

// The morning brief's "Red flags and disclosures" card: what /api/brief puts under `disclosures`
// (finresearch.disclosures.views.brief_section; database only, refreshed by the monitor after each close).

import { ShieldAlert } from "lucide-react";
import Link from "next/link";

import { crore, inr } from "@/components/markets/common";
import { actionLabel, type Flag, type RatingRow } from "@/components/markets/disclosures";
import { Badge, Card } from "@/components/ui";
import { day } from "@/lib/api";

type Who = { key: string; name: string | null; held: boolean; watched: boolean };
export type BriefDisclosures = {
  flags: (Who & Flag)[]; insider: (Who & { net_value: string })[];
  deals: (Who & { kind: string; day: string; side: string; client: string | null; quantity: string; price: string })[];
  ratings: (Who & RatingRow)[]; sebi_orders: (Who & { title: string; link: string; day: string | null })[];
  unavailable: string[]; tracked: number; bonds: number; bse_only: string[]; error?: string;
};

const href = (key: string) => (key.startsWith("IN") && key.length === 12 ? `/bonds/${key}` : `/stocks/${encodeURIComponent(key)}`);
const tag = (w: Who) => (w.held ? "held" : "watched");

export function BriefDisclosuresCard({ brief }: { brief: object }) {
  const d = (brief as { disclosures?: BriefDisclosures }).disclosures;
  if (!d) return null;
  const empty = !d.flags.length && !d.insider.length && !d.deals.length && !d.ratings.length && !d.sebi_orders.length;
  return (
    <Card title="Red flags and disclosures" icon={<ShieldAlert className="size-4" />}
      subtitle={`Holdings and the watchlist (${d.tracked} NSE stock${d.tracked === 1 ? "" : "s"}, ${d.bonds} bond${d.bonds === 1 ? "" : "s"}): surveillance, F&O ban, pledge, insiders, deals, ratings, SEBI orders.`}
      help="NSE ASM/GSM lists and the F&O ban list, promoter pledge, insider (PIT) filings, bulk/block deals, credit-rating filings and SEBI orders (possible matches by legal name). Read by the monitor after each close and before the open. Context, not advice.">
      {d.error ? <p className="text-sm text-warn">Disclosures unavailable: {d.error}</p> : empty ? (
        <p className="py-2 text-sm text-muted">{d.unavailable.length ? "Nothing found in the sources that could be read." : d.tracked || d.bonds ? "No red flag, insider activity, deal, rating action or SEBI order to report." : "Hold or watch stocks to see their disclosures here."}</p>
      ) : (
        <ul className="space-y-1.5 text-sm">
          {d.flags.map((f) => (
            <li key={`f-${f.key}-${f.label}`} className="flex flex-wrap items-center gap-2">
              <Link href={href(f.key)} className="font-medium hover:underline">{f.key}</Link><Badge tone={f.tone}>{f.label}</Badge>
              <span className="text-[11px] text-muted">{tag(f)} · as of {day(f.as_of)}</span>
            </li>
          ))}
          {d.ratings.map((r, i) => (
            <li key={`r-${r.key}-${i}`} className="flex flex-wrap items-center gap-2">
              <Link href={href(r.key)} className="font-medium hover:underline">{r.name ?? r.key}</Link>
              <Badge tone={r.adverse ? "loss" : "gain"}>{actionLabel(r.action)}</Badge>
              <span className="text-xs text-muted">{r.agency}: {r.rating ?? "—"}{r.outlook ? `, outlook ${r.outlook}` : ""} ({day(r.rating_day)})</span>
            </li>
          ))}
          {d.sebi_orders.map((o) => (
            <li key={`o-${o.link}`} className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{o.key}</span><Badge tone="warn">SEBI order, possible match</Badge>
              <a href={o.link} target="_blank" rel="noreferrer" className="text-xs text-brand hover:underline">{o.title}</a>
            </li>
          ))}
          {d.insider.map((x) => (
            <li key={`i-${x.key}`} className="flex flex-wrap items-center gap-2">
              <Link href={href(x.key)} className="font-medium hover:underline">{x.key}</Link>
              <span className={Number(x.net_value) > 0 ? "text-gain" : "text-loss"}>insiders net {Number(x.net_value) > 0 ? "bought" : "sold"} {crore(Math.abs(Number(x.net_value)), 2)}</span>
              <span className="text-[11px] text-muted">open market, 90 days</span>
            </li>
          ))}
          {d.deals.map((x, i) => (
            <li key={`d-${x.key}-${i}`} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-medium">{x.key}</span><Badge tone={x.side === "BUY" ? "gain" : "loss"}>{x.kind} {x.side}</Badge>
              <span className="text-muted">{x.client} · {Number(x.quantity).toLocaleString("en-IN")} @ {inr(x.price)} · {day(x.day)}</span>
            </li>
          ))}
        </ul>
      )}
      {d.unavailable.length > 0 && <p className="mt-2 text-[11px] text-warn">Unavailable (not &ldquo;none&rdquo;): {d.unavailable.join(", ")}.</p>}
      {d.bse_only.length > 0 && <p className="mt-1 text-[11px] text-muted">Not covered (BSE-only): {d.bse_only.join(", ")}.</p>}
    </Card>
  );
}
