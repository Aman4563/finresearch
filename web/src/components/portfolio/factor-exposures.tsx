"use client";

// Factor exposures (#348): the portfolio's and each holding's loadings on IIMA's Indian market, size, value and
// momentum factors, with Newey-West standard errors and 95 % intervals. Loaded only on request: the first run reads
// the factor file and up to three years of each holding's prices. A missing estimate shows its reason, never 0.

import { Layers } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote, Table } from "@/components/ui";
import { day, useApi } from "@/lib/api";

import { Loading, pct } from "./shared-analytics";

type Exposure = { factor: string; label: string; beta: number; se: number; t: number | null; ci95: [number, number] };
type Fit = {
  available: boolean; reason: string | null; exposures: Exposure[]; r2: number | null; lags: number | null;
  alpha_annual: { value: number | null; se: number | null } | null;
  window: { start: string | null; end: string | null; returns: number; dropped: number };
};
type HoldingFit = Fit & { key: string; name: string; weight: number | null };
type Source = { name: string; citation: string; terms: string; url: string; release: string; first: string; last: string; fetched_at: string | null };
type Factors = {
  available: boolean; reason: string | null; method: string; coverage_note: string | null; disclaimer: string; privacy: string;
  source: Source | null; portfolio: Fit | null; holdings: HoldingFit[];
};

const SHORT: Record<string, string> = { mkt: "Market", smb: "Size", hml: "Value", wml: "Momentum" };
const num = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));

function Window({ f }: { f: Fit }) {
  return <>{f.window.returns} daily returns, {day(f.window.start)} to {day(f.window.end)}{f.window.dropped ? ` (${f.window.dropped} not matched to a factor day, left out)` : ""}</>;
}

function PortfolioFit({ f }: { f: Fit }) {
  if (!f.available) return <p className="text-sm text-muted">Portfolio: {f.reason}</p>;
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted">Your portfolio, <Window f={f} />. R² {num(f.r2)}; Newey-West lags {f.lags}.</p>
      <Table label="Portfolio factor exposures">
        <thead><tr><th>Factor</th><th className="text-right">Exposure</th><th className="text-right">Std. error</th><th className="text-right">95 % interval</th><th className="text-right">t</th></tr></thead>
        <tbody>{f.exposures.map((e) => (
          <tr key={e.factor}><td>{e.label}</td><td className="num text-right font-medium">{num(e.beta)}</td><td className="num text-right">{num(e.se)}</td>
            <td className="num text-right">{num(e.ci95[0])} to {num(e.ci95[1])}</td><td className="num text-right">{num(e.t, 1)}</td></tr>
        ))}</tbody>
      </Table>
      <p className="text-xs text-muted">Alpha (unexplained return, a year): {pct(f.alpha_annual?.value, 2)} ± {pct(f.alpha_annual?.se, 2)} (one standard error): rarely distinguishable from zero over a few years.</p>
    </div>
  );
}

function HoldingRows({ items }: { items: HoldingFit[] }) {
  if (!items.length) return null;
  return (
    <Table label="Holding factor exposures" className="mt-3">
      <thead><tr><th>Holding</th><th className="text-right">Weight</th>{Object.values(SHORT).map((s) => <th key={s} className="text-right">{s}</th>)}</tr></thead>
      <tbody>{items.map((h) => {
        const e = Object.fromEntries(h.exposures.map((x) => [x.factor, x]));
        return (
          <tr key={h.key}><td className="max-w-[14rem] truncate">{h.name}</td><td className="num text-right">{pct(h.weight)}</td>
            {h.available ? Object.keys(SHORT).map((k) => <td key={k} className="num text-right">{num(e[k]?.beta)} <span className="text-[10px] text-muted">±{num(e[k]?.se)}</span></td>)
              : <td colSpan={4} className="text-muted">{h.reason}</td>}
          </tr>
        );
      })}</tbody>
    </Table>
  );
}

export function FactorExposures({ refresh }: { refresh: number }) {
  const [run, setRun] = useState(false);
  const { data, error, reload } = useApi<Factors>(run ? `/api/portfolio/analytics/factors?r=${refresh}` : null);
  return (
    <Card title="Factor exposures" icon={<Layers className="size-4" />}
      help="How your returns moved with India's market, size (small minus big), value (cheap minus expensive by book-to-market) and momentum (recent winners minus losers) factors. 1.0 on Market means it moved one for one with the market's excess return; a positive Size exposure leans small-cap. Estimates with errors, from the past, not advice.">
      {!run ? (
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          <p className="flex-1">Regresses your daily returns on IIMA&apos;s Indian Fama-French-momentum factors. The first run reads the factor file and up to three years of each holding&apos;s prices, which can take a few minutes.</p>
          <Button variant="secondary" onClick={() => setRun(true)}>Estimate exposures</Button>
        </div>
      ) : error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <Loading what="factor exposures" /> : !data.available ? (
        <p className="text-sm text-muted">{data.reason}</p>
      ) : (
        <div className="space-y-2">
          {data.coverage_note && <p className="text-xs text-warn"><Badge tone="warn">factor data ends {day(data.source?.last)}</Badge> {data.coverage_note}</p>}
          {data.portfolio && <PortfolioFit f={data.portfolio} />}
          <HoldingRows items={data.holdings} />
          <p className="text-[11px] text-muted">{data.method}</p>
          {data.source && <p className="text-[11px] text-muted">Source: {data.source.name}, release {data.source.release} ({day(data.source.first)} to {day(data.source.last)}). {data.source.citation}. {data.source.terms}</p>}
        </div>
      )}
    </Card>
  );
}
