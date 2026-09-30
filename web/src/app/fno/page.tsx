"use client";

import { Activity, Calculator, Crosshair, Layers, Loader2, Minus, Plus, RefreshCw, Search, Sparkles, Target, Trash2, TrendingUp } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { InteractivePayoff, OiButterfly } from "@/components/fno/charts";
import { CostsBreakdown, IvTile, ProbabilityPanel, RiskBanner } from "@/components/fno/risk";
import { SignalCard } from "@/components/signal";
import { type Chain, type Leg, type PresetKey, PRESETS, type Result, n, payoffCurve, pnlAt, premiumOf, presetLegs } from "@/components/fno/model";
import { fmtINR } from "@/components/charts";
import { LiveStamp, sessionOpen, useMarketStatus } from "@/components/live";
import { Badge, Button, Callout, Card, EmptyState, ErrorNote, InfoTip, PageHeader, Segmented, Skeleton, Stat, cx, inputClass } from "@/components/ui";
import { api, day, when } from "@/lib/api";
import { useTimeFrames } from "@/lib/timeframes";

const QUICK = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "INFY"];
const inr = (x: number | null) => (x == null ? "Unlimited" : `${x < 0 ? "−" : ""}${fmtINR(Math.abs(Math.round(x)))}`);
const compact = (v: number | null) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  return a >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : a >= 1e5 ? `${(v / 1e5).toFixed(2)}L` : a >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : String(v);
};

const GREEKS: { key: string; label: string; digits: number; help: string }[] = [
  { key: "delta", label: "Delta", digits: 1, help: "How many rupees the position gains when the underlying rises ₹1. Positive = bullish, negative = bearish." },
  { key: "gamma", label: "Gamma", digits: 3, help: "How fast delta changes as the price moves. High gamma means the position's direction changes quickly." },
  { key: "vega", label: "Vega", digits: 1, help: "Rupees gained per 1 point rise in implied volatility (IV). Buyers of options usually have positive vega." },
  { key: "theta", label: "Theta / day", digits: 1, help: "Rupees gained (or lost, if negative) each day from time decay alone, if nothing else changes." },
];

function ChainTable({ chain, width, onAdd }: { chain: Chain; width: number; onAdd: (right: Leg["right"], strike: string, side: Leg["side"]) => void }) {
  const spot = Number(chain.underlying);
  const rows = useMemo(() => {
    const sorted = [...chain.rows].sort((a, b) => Number(a.strike) - Number(b.strike));
    const i = sorted.findIndex((r) => r.strike === chain.atm_strike);
    return i < 0 || width === 0 ? sorted : sorted.slice(Math.max(0, i - width), i + width + 1);
  }, [chain, width]);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // centre the strike column and the ATM row (the chain is wider and taller than a phone)
    const el = box.current;
    if (!el) return;
    el.scrollLeft = (el.scrollWidth - el.clientWidth) / 2;
    const atm = el.querySelector<HTMLElement>("[data-atm]");
    if (atm) el.scrollTop = atm.offsetTop - el.clientHeight / 2;
  }, [rows]);
  const maxOi = Math.max(1, ...rows.flatMap((r) => [n(r.call?.oi) ?? 0, n(r.put?.oi) ?? 0]));
  const trade = (right: "call" | "put", strike: string) => (
    <span className="inline-flex gap-0.5">
      <button type="button" title={`Buy ${right} ${strike}`} onClick={() => onAdd(right, strike, "buy")}
        className="rounded px-1.5 py-0.5 text-[10px] font-semibold text-gain ring-1 ring-inset ring-gain/30 transition hover:bg-gain-soft">B</button>
      <button type="button" title={`Sell ${right} ${strike}`} onClick={() => onAdd(right, strike, "sell")}
        className="rounded px-1.5 py-0.5 text-[10px] font-semibold text-loss ring-1 ring-inset ring-loss/30 transition hover:bg-loss-soft">S</button>
    </span>
  );
  const chg = (x: string | null | undefined) => {
    const v = n(x);
    return v == null ? "—" : <span className={v > 0 ? "text-gain" : v < 0 ? "text-loss" : ""}>{v > 0 ? "+" : ""}{compact(v)}</span>;
  };
  return (
    <div ref={box} className="-mx-4 max-h-[600px] overflow-auto sm:-mx-5">
      <table className="num w-full min-w-[720px] text-xs [&_td]:px-2 [&_td]:py-1.5 [&_th]:px-2 [&_th]:py-2 [&_th]:text-[10px] [&_th]:font-medium [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-muted">
        <thead>
          <tr className="border-b border-border">
            <th colSpan={5} className="!text-center !text-loss">Calls</th>
            <th className="bg-background-subtle !text-center">Strike</th>
            <th colSpan={5} className="!text-center !text-gain">Puts</th>
          </tr>
          <tr className="border-b border-border text-right">
            <th className="!text-left">OI</th><th className="!text-right">Chg OI</th><th className="!text-right">IV</th><th className="!text-right">LTP</th><th />
            <th className="bg-background-subtle" />
            <th /><th className="!text-left">LTP</th><th className="!text-left">IV</th><th className="!text-left">Chg OI</th><th className="!text-right">OI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const k = Number(r.strike);
            const atm = r.strike === chain.atm_strike;
            const callItm = k < spot, putItm = k > spot;
            const coi = n(r.call?.oi) ?? 0, poi = n(r.put?.oi) ?? 0;
            return (
              <tr key={r.strike} data-atm={atm || undefined} className={cx("border-b border-border/50 transition-colors hover:bg-card-hover", atm && "!bg-brand-soft/70 font-semibold")}>
                <td className={cx(callItm && "bg-warn-soft/40")}>
                  <div className="flex items-center gap-1.5">
                    <div className="h-1.5 w-14 shrink-0 overflow-hidden rounded-full bg-background-subtle"><div className="ml-auto h-full rounded-full bg-loss/70" style={{ width: `${(coi / maxOi) * 100}%` }} /></div>
                    <span className="w-12 text-right">{compact(coi)}</span>
                  </div>
                </td>
                <td className={cx("text-right", callItm && "bg-warn-soft/40")}>{chg(r.call?.change_in_oi)}</td>
                <td className={cx("text-right text-muted", callItm && "bg-warn-soft/40")}>{r.call?.iv ?? "—"}</td>
                <td className={cx("text-right font-medium", callItm && "bg-warn-soft/40")}>{r.call?.last_price ?? "—"}</td>
                <td className={cx("text-center", callItm && "bg-warn-soft/40")}>{r.call?.last_price ? trade("call", r.strike) : null}</td>
                <td className={cx("bg-background-subtle text-center text-sm font-semibold", atm && "!bg-brand text-brand-fg")}>
                  {k.toLocaleString("en-IN")}
                  {atm && <span className="ml-1 text-[9px] font-medium uppercase">ATM</span>}
                </td>
                <td className={cx("text-center", putItm && "bg-warn-soft/40")}>{r.put?.last_price ? trade("put", r.strike) : null}</td>
                <td className={cx("font-medium", putItm && "bg-warn-soft/40")}>{r.put?.last_price ?? "—"}</td>
                <td className={cx("text-muted", putItm && "bg-warn-soft/40")}>{r.put?.iv ?? "—"}</td>
                <td className={cx(putItm && "bg-warn-soft/40")}>{chg(r.put?.change_in_oi)}</td>
                <td className={cx(putItm && "bg-warn-soft/40")}>
                  <div className="flex items-center gap-1.5">
                    <span className="w-12">{compact(poi)}</span>
                    <div className="h-1.5 w-14 shrink-0 overflow-hidden rounded-full bg-background-subtle"><div className="h-full rounded-full bg-gain/70" style={{ width: `${(poi / maxOi) * 100}%` }} /></div>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function Fno() {
  const [symbol, setSymbol] = useState("NIFTY");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("");
  const [chain, setChain] = useState<Chain | null>(null);
  const [legs, setLegs] = useState<Leg[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [analysed, setAnalysed] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<"" | "expiries" | "chain" | "analyse">("");
  const [width, setWidth] = useState(10);
  const [what, setWhat] = useState<number | null>(null);
  const [chainAt, setChainAt] = useState<Date | null>(null);
  const market = useMarketStatus();
  const fnoMs = useTimeFrames().tf.fno_refresh_s * 1000; // profile: 1, 2 or 5 min, or off
  const fnoOpen = sessionOpen(market.data, "equity");
  const liveFno = fnoOpen && fnoMs > 0;

  const loadChain = useCallback(async (sym: string, exp: string) => {
    setExpiry(exp);
    if (!exp) return;
    setLoading("chain");
    try {
      setChain(await api<Chain>(`/api/fno/${encodeURIComponent(sym)}/chain?expiry=${exp}`));
      setChainAt(new Date());
      setResult(null);
      setLegs([]);
      setWhat(null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading("");
    }
  }, []);

  const load = useCallback(async (sym: string) => {
    setSymbol(sym);
    setLoading("expiries");
    try {
      const e = await api<{ expiries: string[] }>(`/api/fno/${encodeURIComponent(sym)}/expiries`);
      setExpiries(e.expiries);
      setChain(null);
      setError(null);
      if (e.expiries[0]) await loadChain(sym, e.expiries[0]);
      else setExpiry("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading((l) => (l === "expiries" ? "" : l));
    }
  }, [loadChain]);

  useEffect(() => {
    // open on NIFTY's nearest expiry (a GET; scheduled so the first render is not blocked)
    const t = setTimeout(() => load(new URLSearchParams(window.location.search).get("symbol")?.toUpperCase() || "NIFTY"), 0);
    return () => clearTimeout(t);
  }, [load]);

  const analyse = useCallback(async (ls: Leg[]) => {
    if (!ls.length) return;
    setLoading("analyse");
    try {
      setResult(await api<Result>("/api/fno/strategy", { method: "POST", body: JSON.stringify({ symbol: chain?.symbol ?? symbol, expiry: chain?.expiry ?? expiry, legs: ls }) }));
      setAnalysed(JSON.stringify(ls));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading("");
    }
  }, [symbol, expiry, chain]);

  // while NSE is open the chain refreshes every minute; legs stay, premiums (and so the payoff) follow the market
  const legsRef = useRef<Leg[]>([]);
  const refreshChain = useCallback(async () => {
    if (!chain) return;
    try {
      setChain(await api<Chain>(`/api/fno/${encodeURIComponent(chain.symbol)}/chain?expiry=${chain.expiry}`));
      setChainAt(new Date());
      if (legsRef.current.length) await analyse(legsRef.current);
    } catch {
      /* keep the last chain; the stamp shows its time */
    }
  }, [chain, analyse]);
  const refreshRef = useRef(refreshChain);
  useEffect(() => {
    legsRef.current = result ? legs : [];
    refreshRef.current = refreshChain;
  });
  const hasChain = chain != null;
  useEffect(() => {
    if (!liveFno || !hasChain) return;
    const t = setInterval(() => !document.hidden && refreshRef.current(), fnoMs);
    return () => clearInterval(t);
  }, [liveFno, hasChain, fnoMs]);

  // ?preset=iron_condor (shareable link): apply once, when the first chain arrives
  const [presetDone, setPresetDone] = useState(false);
  useEffect(() => {
    if (!chain || presetDone) return;
    const k = new URLSearchParams(window.location.search).get("preset") as PresetKey | null;
    const t = setTimeout(() => {
      setPresetDone(true);
      if (k && PRESETS.some((p) => p.key === k)) {
        const ls = presetLegs(chain, k);
        if (ls) {
          setLegs(ls);
          analyse(ls);
        }
      }
    }, 0);
    return () => clearTimeout(t);
  }, [chain, presetDone, analyse]);

  const add = (right: Leg["right"], strike: string, side: Leg["side"]) => setLegs((l) => [...l, { right, strike, side, lots: 1 }]);
  const applyPreset = (k: PresetKey) => {
    if (!chain) return;
    const ls = presetLegs(chain, k);
    if (!ls) return setError("Not enough traded strikes near the money for this preset.");
    setLegs(ls);
    setWhat(null);
    analyse(ls);
  };

  const spot = chain ? Number(chain.underlying) : 0;
  const lot = result?.lot_size ?? chain?.lot_size ?? 1;
  const curve = useMemo(() => (chain && legs.length ? payoffCurve(chain, legs, lot) : null), [chain, legs, lot]);
  const fresh = !!result && analysed === JSON.stringify(legs);
  const whatSpot = what ?? spot;
  const whatPnl = chain && legs.length ? pnlAt(chain, legs, lot, whatSpot) : 0;
  const oiRows = useMemo(() => {
    if (!chain) return [];
    const sorted = [...chain.rows].sort((a, b) => Number(a.strike) - Number(b.strike));
    const i = sorted.findIndex((r) => r.strike === chain.atm_strike);
    const near = i < 0 ? sorted : sorted.slice(Math.max(0, i - 12), i + 13);
    return near.map((r) => ({
      strike: Number(r.strike), calls: -(n(r.call?.oi) ?? 0), puts: n(r.put?.oi) ?? 0,
      callChg: n(r.call?.change_in_oi) ?? 0, putChg: n(r.put?.change_in_oi) ?? 0,
    }));
  }, [chain]);
  const pcr = chain?.pcr_oi ? Number(chain.pcr_oi) : null;
  const atmIv = chain ? (([n(chain.atm_iv.call), n(chain.atm_iv.put)].filter((x) => x != null) as number[]).reduce((s, x, _, arr) => s + x / arr.length, 0) || null) : null;
  const an = fresh ? result?.analysis : undefined;

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<Activity className="size-5" />}
        eyebrow="Research"
        title="F&O analytics"
        description="NSE option chain, open interest and a strategy builder that shows what you could make or lose at expiry. Analysis only: no orders are placed."
      />
      <RiskBanner />
      {chain && (
        <LiveStamp session="equity" live={fnoOpen} autoRefresh={fnoMs > 0} status={market.data} updatedAt={chainAt} everyMs={fnoMs || 60000}
          asOf={chain.as_of} onRefresh={refreshChain} className="-mt-4" />
      )}

      <Card>
        <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); load(symbol); }}>
          <label className="relative min-w-40 flex-1 sm:max-w-64">
            <span className="sr-only">Symbol</span>
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted" />
            <input className={cx(inputClass, "w-full pl-9 uppercase")} value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} placeholder="NIFTY, BANKNIFTY, INFY…" />
          </label>
          <Button type="submit" size="md" disabled={!!loading} icon={loading === "expiries" ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}>
            Load
          </Button>
          {expiries.length > 0 && (
            <label className="flex items-center gap-2 text-xs text-muted">
              Expiry
              <select className={inputClass} value={expiry} onChange={(e) => loadChain(symbol, e.target.value)}>
                {expiries.map((e) => (
                  <option key={e} value={e}>
                    {day(e)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {expiry && (
            <Button variant="ghost" size="md" onClick={() => loadChain(symbol, expiry)} disabled={!!loading}>
              Reload chain
            </Button>
          )}
        </form>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {QUICK.map((q) => (
            <button key={q} type="button" onClick={() => { setSymbol(q); load(q); }}
              className={cx("rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset transition", q === chain?.symbol ? "bg-brand-soft text-brand ring-brand/30" : "text-muted ring-border hover:text-foreground hover:ring-border-strong")}>
              {q}
            </button>
          ))}
        </div>
        <div className="mt-3">
          <ErrorNote error={error} onRetry={() => (expiry ? loadChain(symbol, expiry) : load(symbol))} />
        </div>
      </Card>

      {loading === "chain" || (loading === "expiries" && !chain) ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-28 rounded-xl" />)}
          </div>
          <Skeleton className="h-96 rounded-xl" />
        </div>
      ) : chain ? (
        <>
          <div className="stagger grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <Stat label={`${chain.symbol} spot`} value={spot} format={(v) => v.toLocaleString("en-IN", { maximumFractionDigits: 2 })} icon={<TrendingUp className="size-4" />}
              hint={chain.as_of ? `as of ${when(chain.as_of)}` : undefined} />
            <Stat label="ATM strike" value={n(chain.atm_strike)} format={(v) => v.toLocaleString("en-IN")} tone="accent" icon={<Crosshair className="size-4" />}
              help="At the money: the strike closest to the current price. IV (implied volatility) is how much movement option prices are expecting, as a yearly %."
              hint={`IV call ${chain.atm_iv.call ?? "—"}% · put ${chain.atm_iv.put ?? "—"}%`} />
            <Stat label="PCR (OI)" value={pcr} format={(v) => v.toFixed(2)} tone={pcr == null ? "neutral" : pcr > 1 ? "gain" : pcr < 0.7 ? "loss" : "warn"} icon={<Layers className="size-4" />}
              help="Put-call ratio: total put open interest ÷ total call open interest. Above 1 means more puts are open (often read as support below the price); below 0.7, more calls. A rough sentiment gauge, not a signal."
              hint={pcr == null ? undefined : pcr > 1 ? "more puts than calls" : "more calls than puts"} />
            <Stat label="Max pain" value={n(chain.max_pain)} format={(v) => v.toLocaleString("en-IN")} tone="warn" icon={<Target className="size-4" />}
              help="The expiry price at which option buyers, in total, would lose the most (and sellers keep the most). Prices sometimes drift towards it near expiry; it is not a forecast."
              hint={chain.lot_size ? `lot size ${chain.lot_size} · expires ${day(chain.expiry)}` : `expires ${day(chain.expiry)}`} />
            <IvTile symbol={chain.symbol} atmIv={atmIv} rv20={fresh ? result?.analysis?.vols.realised_20d : null} />
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
            <Card title="Open interest by strike" icon={<Layers className="size-4" />} 
              subtitle="Calls left, puts right, around the money"
              help="Open interest (OI) is the number of contracts still open. Big call OI above the price often acts as resistance, big put OI below it as support.">
              <OiButterfly rows={oiRows} spot={spot} maxPain={n(chain.max_pain)} />
              <div className="mt-2 flex justify-center gap-4 text-[11px] text-muted">
                <span className="inline-flex items-center gap-1"><span className="size-2 rounded-sm bg-loss" /> Call OI</span>
                <span className="inline-flex items-center gap-1"><span className="size-2 rounded-sm bg-gain" /> Put OI</span>
                <span className="inline-flex items-center gap-1"><span className="h-0.5 w-3 bg-accent" /> nearest spot</span>
                <span className="inline-flex items-center gap-1"><span className="h-0.5 w-3 bg-warn" /> max pain</span>
              </div>
            </Card>
            <Card title="Option chain" icon={<Activity className="size-4" />} 
              subtitle="Tap B or S to add a leg to the strategy. Shaded cells are in the money."
              help="LTP = last traded price (the premium per share). IV = implied volatility. Chg OI = change in open interest today. In the money: a call below the spot or a put above it."
              actions={<Segmented value={String(width)} onChange={(v) => setWidth(Number(v))} options={[{ value: "5", label: "±5" }, { value: "10", label: "±10" }, { value: "0", label: "All" }]} />}>
              <ChainTable chain={chain} width={width} onAdd={add} />
            </Card>
          </div>

          <Card title="Strategy builder" icon={<Calculator className="size-4" />}
            subtitle="Pick a preset or add legs from the chain; the payoff updates as you edit"
            help="A strategy is a combination of options (legs). The chart shows the profit or loss of the whole position if you hold it to expiry.">
            <div className="mb-4 flex flex-wrap items-center gap-2">
              {PRESETS.map((p) => (
                <span key={p.key} className="inline-flex items-center gap-1 rounded-lg bg-background-subtle py-1 pr-1.5 pl-2.5 text-xs ring-1 ring-inset ring-border transition hover:ring-brand/40">
                  <button type="button" onClick={() => applyPreset(p.key)} className="font-medium hover:text-brand">{p.label}</button>
                  <span className="text-[10px] text-muted">{p.view}</span>
                  <InfoTip>{p.help}</InfoTip>
                </span>
              ))}
              {legs.length > 0 && (
                <span className="ml-auto flex gap-2">
                  <Button variant="ghost" icon={<Trash2 className="size-3.5" />} onClick={() => { setLegs([]); setResult(null); }}>Clear</Button>
                  <Button onClick={() => analyse(legs)} disabled={loading === "analyse"} icon={loading === "analyse" ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}>
                    {fresh ? "Re-analyse" : "Analyse"}
                  </Button>
                </span>
              )}
            </div>

            {legs.length === 0 ? (
              <EmptyState icon={<Calculator className="size-5" />} title="No legs yet">
                Choose a preset above, or tap B (buy) or S (sell) next to a strike in the option chain.
              </EmptyState>
            ) : (
              <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
                <div className="space-y-2">
                  {legs.map((l, i) => {
                    const prem = premiumOf(chain, l);
                    return (
                      <div key={i} className="flex items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-2 text-sm animate-fade-up">
                        <button type="button" onClick={() => setLegs(legs.map((x, j) => (j === i ? { ...x, side: x.side === "buy" ? "sell" : "buy" } : x)))}
                          title="Switch buy/sell"
                          className={cx("w-11 rounded-md py-0.5 text-[11px] font-semibold uppercase", l.side === "buy" ? "bg-gain-soft text-gain" : "bg-loss-soft text-loss")}>
                          {l.side}
                        </button>
                        <div className="min-w-0 flex-1">
                          <p className="num font-medium">{Number(l.strike).toLocaleString("en-IN")} <span className="text-xs uppercase text-muted">{l.right === "call" ? "CE" : l.right === "put" ? "PE" : "FUT"}</span></p>
                          <p className="num text-[11px] text-muted">{prem != null && l.right !== "future" ? `premium ₹${prem}` : "no traded price"}</p>
                        </div>
                        <div className="flex items-center rounded-md ring-1 ring-inset ring-border">
                          <button type="button" aria-label="One lot less" className="px-1.5 py-1 text-muted hover:text-foreground" onClick={() => setLegs(legs.map((x, j) => (j === i ? { ...x, lots: Math.max(1, x.lots - 1) } : x)))}><Minus className="size-3" /></button>
                          <input aria-label="Lots" className="num w-8 bg-transparent text-center text-xs outline-none" type="number" min={1} max={100} value={l.lots}
                            onChange={(e) => setLegs(legs.map((x, j) => (j === i ? { ...x, lots: Math.min(100, Math.max(1, Number(e.target.value) || 1)) } : x)))} />
                          <button type="button" aria-label="One lot more" className="px-1.5 py-1 text-muted hover:text-foreground" onClick={() => setLegs(legs.map((x, j) => (j === i ? { ...x, lots: Math.min(100, x.lots + 1) } : x)))}><Plus className="size-3" /></button>
                        </div>
                        <button type="button" aria-label="Remove leg" className="text-muted hover:text-loss" onClick={() => setLegs(legs.filter((_, j) => j !== i))}>
                          <Trash2 className="size-3.5" />
                        </button>
                      </div>
                    );
                  })}
                  <p className="text-[11px] text-muted">
                    Lot size <span className="num">{lot}</span>. Premiums are the chain&apos;s last traded prices.
                  </p>

                  {/* what-if */}
                  <div className="mt-3 rounded-xl border border-border bg-background-subtle/50 p-3">
                    <p className="flex items-center gap-1 text-xs font-medium text-muted">
                      What if {chain.symbol} expires at…
                      <InfoTip>Drag the slider (or click the chart) to see the position&apos;s profit or loss if the price ends at that level on expiry day.</InfoTip>
                    </p>
                    <p className="num mt-1 text-lg font-semibold">{Math.round(whatSpot).toLocaleString("en-IN")}
                      <span className={cx("ml-2 text-xs", whatSpot >= spot ? "text-gain" : "text-loss")}>{whatSpot >= spot ? "+" : ""}{(((whatSpot - spot) / spot) * 100).toFixed(2)}%</span>
                    </p>
                    {curve && (
                      <input type="range" aria-label="Expiry price" className="mt-2 w-full accent-[var(--brand)]" min={Math.round(curve.lo)} max={Math.round(curve.hi)} step={1}
                        value={Math.round(whatSpot)} onChange={(e) => setWhat(Number(e.target.value))} />
                    )}
                    <p className={cx("num mt-1 text-2xl font-bold tracking-tight transition-colors", whatPnl >= 0 ? "text-gain" : "text-loss")}>
                      {whatPnl >= 0 ? "+" : "−"}{fmtINR(Math.abs(Math.round(whatPnl)))}
                    </p>
                    {what != null && <button type="button" className="text-[11px] text-brand hover:underline" onClick={() => setWhat(null)}>reset to spot</button>}
                  </div>
                </div>

                <div className="min-w-0 space-y-4">
                  {curve && (
                    <InteractivePayoff points={curve.points} spot={spot} breakevens={fresh ? result!.breakevens : curve.breakevens}
                      what={{ spot: whatSpot, pnl: Math.round(whatPnl) }} onPick={setWhat} band={an?.expected_move ?? null} />
                  )}
                  {result && !fresh && (
                    <Callout tone="warn">The numbers below are for the previous legs. Press Analyse to update them.</Callout>
                  )}
                  {loading === "analyse" && !result && <Skeleton className="h-24 rounded-xl" />}
                  {result && (
                    <div className={cx("space-y-3 transition-opacity", !fresh && "opacity-50")}>
                      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                        <Stat label="Max profit" display={<span className="num text-gain">{inr(result.max_profit)}</span>} tone="gain" />
                        <Stat label="Max loss" display={<span className="num text-loss">{inr(result.max_loss)}</span>} tone="loss" />
                        <Stat label={result.net_premium > 0 ? "Net debit (you pay)" : "Net credit (you receive)"} display={<span className="num">{inr(Math.abs(result.net_premium))}</span>}
                          help="Premium received (credit) or paid (debit) to open all legs together." />
                        <Stat label="Expected move (1σ)" tone="info"
                          display={<span className="num">{result.analysis?.expected_move ? `±${Math.round(result.analysis.expected_move.move).toLocaleString("en-IN")}` : "—"}</span>}
                          hint={result.analysis?.expected_move ? `${Math.round(result.analysis.expected_move.low).toLocaleString("en-IN")}–${Math.round(result.analysis.expected_move.high).toLocaleString("en-IN")}` : undefined}
                          help="±S·σ·√T with the ATM implied volatility: under the model about two in three expiry prices land inside this band (shaded on the chart)." />
                      </div>
                      {result.analysis && (
                        <div className="grid gap-3 xl:grid-cols-2">
                          <ProbabilityPanel a={result.analysis} />
                          <CostsBreakdown a={result.analysis} />
                        </div>
                      )}
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <span className="text-muted">Breakevens</span>
                        {result.breakevens.length ? result.breakevens.map((b) => <Badge key={b} tone="warn"><span className="num">{b.toLocaleString("en-IN")}</span></Badge>) : <span>none</span>}
                        <InfoTip>Expiry prices where the position neither makes nor loses money (the orange dots on the chart).</InfoTip>
                      </div>
                      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                        {GREEKS.map((g) => {
                          const v = result.net_greeks[g.key];
                          return (
                            <div key={g.key} className="rounded-lg border border-border px-3 py-2">
                              <p className="flex items-center gap-1 text-[11px] text-muted">{g.label}<InfoTip>{g.help}</InfoTip></p>
                              <p className={cx("num text-sm font-semibold", v > 0 ? "text-gain" : v < 0 ? "text-loss" : "")}>{v == null ? "—" : v.toFixed(g.digits)}</p>
                            </div>
                          );
                        })}
                      </div>
                      {result.notes.map((x) => <p key={x} className="text-xs text-warn">{x}</p>)}
                      <p className="text-[11px] text-muted">{result.disclaimer}</p>
                    </div>
                  )}
                </div>
              </div>
            )}
          </Card>

          {fresh && result && (
            <SignalCard key={analysed} asset="fno" instrument={chain.symbol} title="Strategy check" compact={false}
              query={{ expiry: chain.expiry, legs: JSON.stringify(legs.map(({ right, strike, side, lots }) => ({ right, strike, side, lots }))) }} />
          )}
        </>
      ) : (
        !error && (
          <EmptyState icon={<Activity className="size-5" />} title="Load an option chain">
            Type an index or F&amp;O stock symbol (e.g. NIFTY, BANKNIFTY, INFY) and press Load.
          </EmptyState>
        )
      )}
    </div>
  );
}
