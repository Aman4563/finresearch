"use client";

// Accuracy checks (accounting identities, recomputed ratios, unit/scale slips; verify/identities.py) and valuation
// triangulation (reverse DCF, seeded Monte Carlo, DCF grid, peer percentile; fincalc). Both are computed by Python
// from the claim ledger with no model; every input is a claim chip or a labelled ASSUMPTION.

import { AlertTriangle, CheckCircle2, Scale, ShieldCheck, Sigma, XCircle } from "lucide-react";
import { useMemo, useState } from "react";

import { Metric } from "@/components/markets/common";
import { Badge, Card, InfoTip, cx } from "@/components/ui";
import { type ClaimMap, type OpenClaim, SourceStrip, StatusDot } from "@/components/workspace/report/shared";
import type { Accuracy, IdentityCheck, Triangulation } from "@/lib/insights";

const ICON = {
  pass: <CheckCircle2 className="size-4 text-gain" aria-hidden />,
  warn: <AlertTriangle className="size-4 text-warn" aria-hidden />,
  fail: <XCircle className="size-4 text-loss" aria-hidden />,
};
const WORD = { pass: "passed", warn: "warning", fail: "failed" } as const;
const FAMILY: Record<string, string> = {
  pnl_total_income: "P&L", pnl_pbt: "P&L", pnl_exceptional: "P&L", pnl_pat: "P&L", pnl_pat_split: "P&L",
  eps_identity: "EPS", eps_basis: "EPS restated", bs_balance: "Balance sheet", cash_rollforward: "Cash flow",
  margin: "Margin", growth: "Growth", scale_shift: "Units / scale", basis_mix: "Standalone vs consolidated",
  cross_document: "Restated vs later filing", sanity: "Sanity band",
};

function CheckRow({ c, claims, onOpen }: { c: IdentityCheck; claims: ClaimMap; onOpen: OpenClaim }) {
  return (
    <li className="flex gap-2.5 py-2.5">
      <span className="mt-0.5 shrink-0">{ICON[c.status]}</span>
      <div className="min-w-0 flex-1 text-sm">
        <p className="flex flex-wrap items-center gap-1.5 font-medium leading-snug">
          <span className="sr-only">{WORD[c.status]}: </span>
          {c.title}
          <Badge tone="neutral">{FAMILY[c.family] ?? c.family}</Badge>
          {c.period && <span className="num text-[11px] font-normal text-muted">{c.period}{c.basis !== "unknown" && !c.period.includes(c.basis) ? ` · ${c.basis}` : ""}</span>}
          {c.hard && c.status === "fail" && <Badge tone="loss">blocks if cited</Badge>}
        </p>
        <p className="mt-0.5 text-[13px] leading-relaxed text-muted">{c.detail}</p>
        {c.hint && c.status !== "pass" && <p className="mt-0.5 text-[12px] leading-relaxed text-foreground/80">→ {c.hint}</p>}
        <SourceStrip ids={c.claim_ids} claims={claims} onOpen={onOpen} className="mt-1"
          label={c.cited.length ? <span>claims (<span className="num">{c.cited.length}</span> cited in the report)</span> : "claims (none cited in the report)"} />
      </div>
    </li>
  );
}

/** Summary-tab card: how many accounting identities pass, warn or fail, with the claims involved. */
export function AccuracyCard({ acc, claims, onOpen }: { acc: Accuracy; claims: ClaimMap; onOpen: OpenClaim }) {
  const bad = acc.checks.filter((c) => c.status !== "pass");
  const good = acc.checks.filter((c) => c.status === "pass");
  return (
    <Card title="Accuracy checks" icon={<Sigma className="size-4" />}
      subtitle="Do the report's own numbers add up? Checked by Python, no AI."
      help="Accounting identities recomputed from the claim ledger: revenue + other income = total income, PBT − tax = PAT, EPS × shares = profit, assets = equity + liabilities, the cash roll-forward, margins and growth rates from their parts, EPS restated after a bonus or split, and lakh/crore/million or ₹/US$ slips. A failure on a high-importance figure the report cites blocks publishing; other gaps are warnings. Tolerance allows for rounding of printed figures.">
      {acc.applicable === 0 ? (
        <p className="text-sm text-muted">
          Not enough inputs: the ledger has no set of figures that an accounting identity can be checked against
          {acc.facts ? <> (<span className="num">{acc.facts}</span> numeric claims, none forming a P&L, balance-sheet or cash-flow set)</> : null}. This is normal for funds and bonds.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap gap-2">
            {(["pass", "warn", "fail"] as const).map((s) => (
              <span key={s} className={cx("inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm ring-1 ring-inset",
                s === "pass" ? "bg-gain-soft ring-gain/25" : s === "warn" ? "bg-warn-soft ring-warn/25" : "bg-loss-soft ring-loss/25")}>
                {ICON[s]}
                <span className="num font-semibold">{acc.counts[s]}</span>
                <span className="text-muted">{WORD[s]}</span>
              </span>
            ))}
          </div>
          {bad.length > 0 ? (
            <ul className="mt-2 divide-y divide-border/60">{bad.map((c, i) => <CheckRow key={i} c={c} claims={claims} onOpen={onOpen} />)}</ul>
          ) : (
            <p className="mt-3 flex items-center gap-1.5 text-sm"><ShieldCheck className="size-4 text-gain" aria-hidden /> Every check with inputs in the ledger adds up.</p>
          )}
          {good.length > 0 && (
            <details className="group mt-2 rounded-lg bg-background-subtle/60 px-3 py-2">
              <summary className="cursor-pointer text-xs font-medium text-muted">Show the <span className="num">{good.length}</span> passing checks</summary>
              <ul className="divide-y divide-border/60">{good.map((c, i) => <CheckRow key={i} c={c} claims={claims} onOpen={onOpen} />)}</ul>
            </details>
          )}
        </>
      )}
      {acc.not_enough_inputs.length > 0 && (
        <details className="mt-2 text-xs text-muted">
          <summary className="cursor-pointer">Not checked: not enough inputs ({acc.not_enough_inputs.length})</summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">{acc.not_enough_inputs.map((m) => <li key={m}>{m}</li>)}</ul>
        </details>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ valuation triangulation

const rs = (v: number) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: v >= 1000 ? 0 : 2 })}`;
const pc = (v: number, d = 1) => `${(v * 100).toFixed(d)}%`;

function Histogram({ mc, price }: { mc: NonNullable<Triangulation["monte_carlo"]>; price: number | null }) {
  const h = mc.histogram;
  const lo = h[0]?.lo ?? mc.p5, hi = h[h.length - 1]?.hi ?? mc.p95;
  const max = Math.max(1, ...h.map((b) => b.count));
  const pos = (v: number) => ((v - lo) / (hi - lo || 1)) * 100;
  const priceIn = price != null && price >= lo && price <= hi;
  const [hover, setHover] = useState<number | null>(null);
  const hb = hover != null ? h[hover] : null;
  return (
    <div>
      <div className="relative h-44 pt-5" role="img"
        aria-label={`Fair value per share from ${mc.n} simulations: P5 ${rs(mc.p5)}, median ${rs(mc.p50)}, P95 ${rs(mc.p95)}${price != null ? `; price ${rs(price)}` : ""}`}>
        <div className="flex h-full items-end gap-[2px]" onMouseLeave={() => setHover(null)}>
          {h.map((b, i) => {
            const above = price != null && (b.lo + b.hi) / 2 > price;
            return (
              <div key={i} className="group relative flex h-full flex-1 items-end" onMouseEnter={() => setHover(i)}>
                <div className={cx("w-full rounded-t-[4px] transition-opacity", price == null ? "bg-[var(--chart-1)]" : above ? "bg-gain/85" : "bg-loss/70",
                  hover != null && hover !== i && "opacity-60")} style={{ height: `${Math.max(1.5, (b.count / max) * 100)}%` }} />
              </div>
            );
          })}
        </div>
        {price != null && (
          priceIn ? (
            <div className="pointer-events-none absolute inset-y-0 border-l-2 border-dashed border-warn" style={{ left: `${pos(price)}%` }}>
              <span className="num absolute -top-0.5 left-1 whitespace-nowrap rounded bg-card/90 px-1 text-[10px] font-medium text-warn">price {rs(price)}</span>
            </div>
          ) : (
            <span className={cx("num absolute top-0 whitespace-nowrap rounded bg-warn-soft px-1.5 text-[10px] font-medium text-warn", price > hi ? "right-0" : "left-0")}>
              {price > hi ? `price ${rs(price)} →` : `← price ${rs(price)}`}
            </span>
          )
        )}
        {hb && (
          <div className="pointer-events-none absolute top-0 z-10 rounded-md border border-border bg-card px-2 py-1 text-[11px] shadow-pop"
            style={{ left: `min(calc(${pos((hb.lo + hb.hi) / 2)}% - 3rem), calc(100% - 7.5rem))` }}>
            <span className="num font-medium">{rs(hb.lo)}–{rs(hb.hi)}</span>
            <span className="block text-muted"><span className="num">{hb.count}</span> of {mc.n} draws ({pc(hb.count / mc.n)})</span>
          </div>
        )}
      </div>
      <div className="num mt-1 flex justify-between text-[10px] text-muted">
        <span>{rs(lo)}</span><span>median {rs(mc.p50)}</span><span>{rs(hi)}</span>
      </div>
      <p className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-muted">
        {price != null && <>
          <span className="inline-flex items-center gap-1"><span className="size-2 rounded-sm bg-loss/70" /> value below the price</span>
          <span className="inline-flex items-center gap-1"><span className="size-2 rounded-sm bg-gain/85" /> value above the price</span>
        </>}
        <span className="inline-flex items-center gap-1"><span className="h-2 border-l-2 border-dashed border-warn" /> price</span>
      </p>
    </div>
  );
}

function ImpliedGrowth({ t, claims, onOpen }: { t: Triangulation; claims: ClaimMap; onOpen: OpenClaim }) {
  const rd = t.reverse_dcf;
  if (!rd) return null;
  const rows = [
    ...(rd.implied_growth != null ? [{ label: "Priced in (reverse DCF g*)", value: rd.implied_growth, ids: [] as number[], own: true }] : []),
    ...t.history.map((h) => ({ label: h.label.replace(/_/g, " "), value: h.value, ids: h.claim_ids, own: false })),
  ];
  const vals = rows.map((r) => r.value);
  const min = Math.min(0, ...vals), max = Math.max(0.01, ...vals);
  const pos = (v: number) => ((v - min) / (max - min)) * 100;
  return (
    <div>
      <p className="mb-2 text-xs font-medium">
        Growth the price needs vs growth the company has shown
        <InfoTip>Reverse DCF (Mauboussin &amp; Rappaport): the yearly cash-flow growth for the next {rd.years} years at which a discounted-cash-flow value equals today&apos;s price, at a {pc(rd.discount_rate)} discount rate and {pc(rd.terminal_growth)} growth after that. If it is well above what the business has delivered, the price is demanding.</InfoTip>
      </p>
      {rd.implied_growth == null && <p className="text-sm text-muted">{rd.note}</p>}
      <ul className="space-y-1.5">
        {rows.map((r, i) => (
          <li key={i}>
            <button type="button" disabled={!r.ids.length} onClick={() => r.ids[0] && onOpen(r.ids[0])}
              className="grid w-full grid-cols-[minmax(0,10rem)_1fr_3.5rem] items-center gap-2 rounded text-left enabled:hover:bg-card-hover sm:grid-cols-[minmax(0,14rem)_1fr_3.5rem]">
              <span className={cx("truncate text-[12px]", r.own ? "font-semibold" : "text-muted")} title={r.label}>{r.label}</span>
              <span className="relative h-3.5">
                <span className="absolute inset-y-0 w-px bg-border" style={{ left: `${pos(0)}%` }} />
                <span className={cx("absolute inset-y-0 rounded-[4px]", r.own ? "bg-brand" : "bg-[var(--chart-2)]")}
                  style={{ left: `${pos(Math.min(0, r.value))}%`, width: `max(2px, ${Math.abs(pos(r.value) - pos(0))}%)` }} />
              </span>
              <span className="num text-right text-[12px]">{pc(r.value)}</span>
            </button>
          </li>
        ))}
      </ul>
      {t.history.length > 0 && (
        <SourceStrip ids={t.history.flatMap((h) => h.claim_ids)} claims={claims} onOpen={onOpen} label="history from" className="mt-1.5" />
      )}
    </div>
  );
}

function Grid({ t }: { t: Triangulation }) {
  const g = t.grid;
  if (!g) return null;
  return (
    <div className="overflow-x-auto">
      <p className="mb-1.5 text-xs font-medium">
        DCF value per share at {pc(g.growth)} growth: discount rate × terminal growth
        <InfoTip>How much the answer moves if the discount rate or the long-run growth assumption is off by a point. ▲ = above today&apos;s price, ▼ = below.</InfoTip>
      </p>
      <table className="num w-full min-w-[20rem] text-right text-[12px]">
        <thead>
          <tr className="text-muted"><th className="py-1 text-left font-normal">Discount ↓ / terminal →</th>{g.terminal_growths.map((x) => <th key={x} className="py-1 font-normal">{pc(x)}</th>)}</tr>
        </thead>
        <tbody>
          {g.discount_rates.map((r, i) => (
            <tr key={r} className={cx("border-t border-border/60", i === Math.floor(g.discount_rates.length / 2) && "bg-background-subtle/60")}>
              <td className="py-1 text-left text-muted">{pc(r, 2)}</td>
              {g.values[i].map((v, j) => (
                <td key={j} className={cx("whitespace-nowrap py-1 pl-2", v == null ? "text-muted" : t.price == null ? "" : v >= t.price ? "text-gain" : "text-loss")}>
                  {v == null ? "n/a" : <>{t.price != null && (v >= t.price ? "▲ " : "▼ ")}{rs(v)}</>}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Charts-tab card: the fair-value distribution with the price line, the reverse DCF, the grid, peers and inputs. */
export function TriangulationCard({ t, claims, onOpen }: { t: Triangulation; claims: ClaimMap; onOpen: OpenClaim }) {
  const mc = t.monte_carlo;
  const assumptions = useMemo(() => t.inputs.filter((i) => i.claim_id == null).length, [t.inputs]);
  return (
    <Card title="Valuation triangulation" icon={<Scale className="size-4" />} className="animate-fade-up"
      subtitle={t.status === "ok" ? `Fincalc, from ledger claims plus ${assumptions} labelled assumption${assumptions === 1 ? "" : "s"}; ${t.cash_flow_basis ?? "no cash flow"} basis` : "Not enough inputs in the ledger"}
      help="Several valuation methods side by side instead of one number: a Monte Carlo DCF (thousands of runs with growth, discount rate and terminal growth drawn from stated ranges; the same seed always gives the same chart), the growth the price implies (reverse DCF), a sensitivity grid and where the company's P/E sits among its peers. Garbage in, garbage out: check the inputs table.">
      {t.status !== "ok" ? (
        <div className="text-sm text-muted">
          <p>Not enough inputs to triangulate a value honestly. Missing from the ledger:</p>
          <ul className="mt-1 list-disc pl-5">{t.missing.map((m) => <li key={m}>{m}</li>)}</ul>
        </div>
      ) : (
        <div className="space-y-5">
          {mc && (
            <section>
              <div className="mb-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Metric label="P5 (pessimistic)" value={rs(mc.p5)} />
                <Metric label="Median (P50)" value={rs(mc.p50)} />
                <Metric label="P95 (optimistic)" value={rs(mc.p95)} />
                <Metric label="P(value > price)" value={mc.prob_above_price == null ? "—" : pc(mc.prob_above_price, 0)}
                  tone={mc.prob_above_price == null ? undefined : mc.prob_above_price >= 0.5 ? "text-gain" : "text-loss"}
                  help="Share of the simulated values above the price. Not a probability of profit: it only says how the price compares with these inputs." />
              </div>
              <Histogram mc={mc} price={t.price} />
              <p className="mt-1 text-[11px] text-muted">
                <span className="num">{mc.n.toLocaleString("en-IN")}</span> seeded draws (seed <span className="num">{t.seed}</span>), {t.years}-year DCF per share
                {mc.rejected ? <>; <span className="num">{mc.rejected}</span> draws with discount rate too close to terminal growth were redrawn</> : null}.
              </p>
            </section>
          )}
          <ImpliedGrowth t={t} claims={claims} onOpen={onOpen} />
          <Grid t={t} />
          {t.peers && (
            <p className="text-sm leading-relaxed">
              <span className="font-medium">Peers:</span> the company&apos;s P/E of <span className="num">{t.peers.own.toFixed(2)}x</span> sits at the{" "}
              <span className="num font-medium">{t.peers.percentile?.toFixed(0)}th</span> percentile of <span className="num">{t.peers.n}</span> peers
              (median <span className="num">{t.peers.median.toFixed(1)}x</span>, middle half <span className="num">{t.peers.q1.toFixed(1)}–{t.peers.q3.toFixed(1)}x</span>)
              {t.peers.implied && <> — at peer multiples the price would be <span className="num">{rs(t.peers.implied.low)}–{rs(t.peers.implied.high)}</span></>}.
              <SourceStrip ids={[t.peers.own_claim, ...t.peers.claim_ids]} claims={claims} onOpen={onOpen} label="from" className="mt-1" />
            </p>
          )}
          {t.band && (
            <div className="rounded-lg bg-background-subtle/60 p-3 text-sm">
              <p className="font-medium">
                Fair-value band <span className="num">{rs(t.band.low)}–{rs(t.band.high)}</span>
                {t.band.intersection && <> · where methods agree <span className="num">{rs(t.band.intersection[0])}–{rs(t.band.intersection[1])}</span></>}
              </p>
              <ul className="mt-1 space-y-0.5 text-[12px] text-muted">
                {t.band.methods.map((m) => <li key={m.name}>{m.name}: <span className="num text-foreground/80">{rs(m.low)}–{rs(m.high)}</span></li>)}
              </ul>
              {t.band.disagree && (
                <p className="mt-1.5 flex gap-1.5 text-[12px] text-warn"><AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  <span>The methods disagree by more than 30% (<span className="num">{pc(t.band.spread, 0)}</span> between midpoints): treat any single value with caution.</span></p>
              )}
            </div>
          )}
          {t.notes.map((n) => <p key={n} className="flex gap-1.5 text-[12px] text-warn"><AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />{n}</p>)}
          <details className="text-sm" open>
            <summary className="cursor-pointer text-xs font-medium text-muted">Inputs and where each came from</summary>
            <ul className="mt-1.5 divide-y divide-border/60 text-[12px]">
              {t.inputs.map((i) => (
                <li key={i.name} className="py-1.5">
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="text-muted">{i.name}</span>
                    <span className="flex shrink-0 items-baseline gap-2">
                      <span className="num">{i.display}</span>
                      {i.claim_id != null ? (
                        <button type="button" onClick={() => onOpen(i.claim_id!)} className="inline-flex items-center gap-1 text-brand hover:underline">
                          <StatusDot status={i.status ?? ""} /> C{i.claim_id}
                        </button>
                      ) : <Badge tone="warn">assumption</Badge>}
                    </span>
                  </div>
                  {i.claim_id == null && <p className="mt-0.5 text-[11px] leading-snug text-muted">{i.source.replace(/^ASSUMPTION:\s*/, "")}</p>}
                </li>
              ))}
            </ul>
          </details>
          {t.missing.length > 0 && <p className="text-[11px] text-muted">Left out (not in the ledger): {t.missing.join("; ")}.</p>}
        </div>
      )}
    </Card>
  );
}
