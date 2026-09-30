"use client";

// Small pieces shared by the report reader's tabs: cited text, claim chips for a chart point, status dots, the
// glossary lookup behind "explain this term" and the confidence meter.

import { type ReactNode, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { CiteChip } from "@/components/evidence";
import { GLOSSARY, type Term } from "@/components/help/content";
import { InfoTip, cx } from "@/components/ui";
import type { Claim } from "@/lib/api";

export type ClaimMap = Record<string, Claim>;
export type OpenClaim = (id: number) => void;

const CITE = /\[C(\d+)\](?!\()/g;
export const linkCites = (md: string) => md.replace(CITE, "[C$1](#cite-$1)");

/** Inline markdown (bold, links) with [C123] rendered as evidence chips. */
export function CiteText({ text, claims, onOpen, className, block }: {
  text: string; claims: ClaimMap; onOpen: OpenClaim; className?: string; /** keep paragraphs and lists */ block?: boolean;
}) {
  const comps = useMemo<Components>(
    () => ({
      ...(block ? {} : { p: ({ children }) => <>{children}</> }),
      a: ({ href, children }) => {
        const m = href?.match(/^#cite-(\d+)$/);
        if (m) return <CiteChip id={Number(m[1])} claim={claims[m[1]]} onOpen={onOpen} />;
        return (
          <a href={href} target="_blank" rel="noreferrer noopener" className="text-brand underline underline-offset-2">
            {children}
          </a>
        );
      },
    }),
    [claims, onOpen, block],
  );
  if (block)
    return (
      <div className={cx("[&_strong]:font-semibold [&_p]:my-1.5 [&_ul]:list-disc [&_ul]:pl-5 [&_li]:my-0.5", className)}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={comps}>
          {linkCites(text)}
        </ReactMarkdown>
      </div>
    );
  return (
    <span className={cx("[&_strong]:font-semibold", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={comps}>
        {linkCites(text)}
      </ReactMarkdown>
    </span>
  );
}

const DOT: Record<string, string> = {
  verified: "bg-gain", needs_review: "bg-warn", unverified: "bg-warn", contradicted: "bg-loss", unsupported: "bg-loss",
};
export const STATUS_LABEL: Record<string, string> = {
  verified: "verified", needs_review: "needs review", unverified: "unverified", contradicted: "contradicted", unsupported: "unsupported",
};

export function StatusDot({ status, className }: { status: string; className?: string }) {
  return <span title={STATUS_LABEL[status] ?? status} className={cx("inline-block size-1.5 shrink-0 rounded-full", DOT[status] ?? "bg-muted", className)} />;
}

/** The claims behind a chart point / tile, as chips that open the evidence panel. */
export function SourceStrip({ ids, claims, onOpen, label = "Sources", className }: {
  ids: number[]; claims: ClaimMap; onOpen: OpenClaim; label?: ReactNode; className?: string;
}) {
  if (!ids.length) return null;
  return (
    <div className={cx("flex flex-wrap items-center gap-1 text-[11px] text-muted", className)}>
      {label ? <span>{label}:</span> : null}
      {ids.map((i) => (
        <CiteChip key={i} id={i} claim={claims[String(i)]} onOpen={onOpen} />
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ glossary

const byKey = new Map<string, Term>();
for (const t of GLOSSARY) {
  byKey.set(t.term.toLowerCase(), t);
  for (const part of t.term.split(" / ")) byKey.set(part.toLowerCase(), t);
  for (const a of (t.aka ?? "").split(",")) if (a.trim()) byKey.set(a.trim().toLowerCase(), t);
}

/** A glossary entry by its name or alias ("P/E", "price-to-earnings", "TER"). */
export const termFor = (name: string | null | undefined) => (name ? byKey.get(name.toLowerCase()) : undefined);

/** Words that stay plain: generic in running text, or not about investing in a report. */
const SKIP = new Set(["Lot", "Allotment", "Subscription (x times)", "Duration", "Grey market", "Premium", "Option", "Strike", "Expiry"]);

export type GlossPattern = { re: RegExp; term: Term };

/** Regexes for auto-linking glossary terms in report text (IPO, stocks, funds and bonds terms only). */
export const GLOSS_PATTERNS: GlossPattern[] = (() => {
  const out: GlossPattern[] = [];
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
  for (const t of GLOSSARY) {
    if (t.category === "F&O" || t.category === "FinResearch" || SKIP.has(t.term)) continue;
    const names = [...t.term.replace(/\s*\(.*?\)\s*/g, "").split(" / "), ...(t.aka ?? "").split(",").map((a) => a.trim())]
      .filter((n) => n.length >= 2 && !/^(TRI|OI|market lot|minimum bid)$/i.test(n));
    for (const n of names) {
      const caps = /[A-Z].*[A-Z]|^[A-Z]+$/.test(n) || /\//.test(n);
      out.push({ re: new RegExp(`(?<![\\w/-])${esc(n)}(?![\\w/-])`, caps ? "" : "i"), term: t });
    }
  }
  return out.sort((a, b) => b.re.source.length - a.re.source.length); // longest names first
})();

export const glossSlug = (t: Term) => t.term.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
export const GLOSS_BY_SLUG = new Map(GLOSSARY.map((t) => [glossSlug(t), t]));

/** Dotted-underline term with a hover / tap definition. */
export function GlossTerm({ term, children }: { term: Term; children: ReactNode }) {
  return (
    <span className="group relative inline">
      <button type="button" className="cursor-help border-b border-dotted border-brand/60 text-inherit decoration-0 hover:border-brand hover:text-brand focus:text-brand focus:outline-none">
        {children}
      </button>
      <span role="tooltip" className="pointer-events-none absolute bottom-full left-0 z-40 mb-1.5 hidden w-72 max-w-[80vw] rounded-lg border border-border bg-card p-2.5 text-left text-xs font-normal not-italic leading-snug text-foreground shadow-pop group-hover:block group-focus-within:block group-hover:animate-scale-in">
        <span className="mb-0.5 block font-semibold text-brand">
          {term.term}
          {term.aka && <span className="font-normal text-muted"> · {term.aka}</span>}
        </span>
        {term.def}
      </span>
    </span>
  );
}

/** Plain-English help for a key-number tile: the glossary first, then a short built-in note. */
const EXPLAIN: Record<string, string> = {
  "Revenue": "Money the company earned from its main business in the period (before any costs).",
  "Net profit (PAT)": "What is left for shareholders after all costs, interest and tax.",
  "Net profit": "What is left for shareholders after all costs, interest and tax.",
  "Return on equity": "Net profit ÷ shareholders' money. Around 15%+ is healthy; check it is not driven by heavy debt.",
  "Issue size": "The total money the IPO raises: new shares (fresh issue) plus shares sold by existing owners (OFS).",
  "One lot costs": "The minimum you must bid: one lot × the upper price band. The amount is blocked in your bank until allotment.",
  "Market cap": "Share price × number of shares: what the market values the whole company at.",
  "Share price": "The last traded price on NSE when the report was written.",
  "FCF yield": "Free cash flow ÷ market cap: the cash return the business generates on today's price.",
  "Fund size (AUM)": "Total money managed in the scheme. Very large mid/small-cap funds can find it harder to move.",
  "Volatility": "How much the NAV swings in a typical year. Higher = a bumpier ride.",
  "Worst fall": "The largest peak-to-trough fall in the period: how painful holding could have been.",
  "Last traded price": "What the bond last changed hands for on the exchange, per ₹1,000 face value.",
  "Face value (repaid at maturity)": "What the issuer pays back per bond on the maturity date.",
  "Coupon payments a year": "How often interest is paid: 12 = monthly, 1 = once a year.",
  "Series size": "How much of this bond series was issued: small series trade thinly.",
};

export function tileHelp(label: string, term: string | null): string | undefined {
  return termFor(term)?.def ?? EXPLAIN[label] ?? termFor(label)?.def;
}

// ------------------------------------------------------------------ confidence

const LEVELS = ["low", "medium", "high"] as const;

export function ConfidenceMeter({ level }: { level: string | null }) {
  const i = LEVELS.indexOf((level ?? "").toLowerCase() as (typeof LEVELS)[number]);
  return (
    <div className="flex items-center gap-2" aria-label={`confidence ${level ?? "not stated"}`}>
      <div className="flex gap-1">
        {LEVELS.map((l, j) => (
          <span key={l} className={cx("h-2 w-7 rounded-full transition-colors", j <= i ? (i === 0 ? "bg-warn" : i === 1 ? "bg-info" : "bg-gain") : "bg-background-subtle ring-1 ring-inset ring-border")} />
        ))}
      </div>
      <span className="text-xs font-medium capitalize">{level ?? "not stated"}</span>
      <span className="rounded border border-border px-1 text-[10px] text-muted">uncalibrated</span>
      <InfoTip>How sure the report is of its own call. Medium usually means one or two key data points are still pending or unverified: read &ldquo;what would change the view&rdquo;. Uncalibrated: there are not yet enough resolved past calls to say how often &ldquo;medium&rdquo; turns out right, so treat it as the writer&apos;s wording, not a probability.</InfoTip>
    </div>
  );
}
