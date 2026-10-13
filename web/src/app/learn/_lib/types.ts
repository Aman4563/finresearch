// Learn F&O (#319): lesson content as plain data. Section bodies are Markdown (GFM tables allowed); a citation is
// written [[source-id]] and rendered as a numbered link to the page's source list (sources.ts). Every factual claim
// carries one; a claim we could not re-read from a primary source points at a source marked "secondary" or
// "unverified", and the page shows that status next to it.

export type Leg = {
  right: "call" | "put" | "future" | "stock";
  side: "buy" | "sell";
  strike?: number; // options only
  days?: number; // days to this leg's expiry at entry (default: the sketch's `days`)
  lots?: number; // default 1
};

/** An illustrative payoff: premiums come from Black-Scholes at `iv`, never from market quotes. */
export type Sketch = {
  spot: number;
  days: number; // days to the (first) expiry; the payoff is drawn on that day
  iv: number; // e.g. 0.14
  lot: number;
  legs: Leg[];
  unlimited?: "profit" | "loss" | "both"; // a side the drawn range cannot show
};

export type StrategyCard = {
  view: string; // market view, e.g. "moderately bullish"
  when: string;
  maxProfit: string;
  maxLoss: string;
  greeks: string; // delta / gamma / theta / vega profile
  manage: string;
  backtest?: { key: string; label: string } | null; // pre-registered variant in /fno/backtests, if any
  builder?: string; // the matching preset in the /fno strategy builder, if any
};

export type Section = {
  id: string;
  title: string;
  body: string; // Markdown with [[source-id]] citations
  sketch?: Sketch;
  card?: StrategyCard;
};

export type Question = { q: string; options: string[]; answer: number; why: string };

export type Module = {
  slug: string;
  n: number;
  title: string;
  summary: string;
  sections: Section[];
  quiz: Question[];
};
