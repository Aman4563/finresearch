"use client";

import { useState } from "react";

import { Button, Card, ErrorNote } from "@/components/ui";
import { api, when } from "@/lib/api";

type Quote = { last_price: string | null; iv: string | null; oi: string | null; change_in_oi: string | null; volume: string | null };
type Row = { strike: string; call: Quote | null; put: Quote | null };
type Chain = {
  symbol: string;
  expiry: string;
  underlying: string;
  as_of: string | null;
  lot_size: number | null;
  atm_strike: string | null;
  atm_iv: { call: string | null; put: string | null };
  pcr_oi: string | null;
  max_pain: string | null;
  rows: Row[];
};
type Leg = { right: "call" | "put" | "future"; strike: string; side: "buy" | "sell"; lots: number };
type Result = {
  breakevens: number[];
  max_profit: number | null;
  max_loss: number | null;
  net_premium: number;
  probability_of_profit: number | null;
  net_greeks: Record<string, number>;
  curve: [number, number][];
  lot_size: number;
  notes: string[];
  disclaimer: string;
};

const field = "rounded border border-border bg-background px-2 py-1 text-sm";
const inr = (x: number | null) => (x == null ? "unlimited" : `₹${Math.round(x).toLocaleString("en-IN")}`);

function Payoff({ curve, spot }: { curve: [number, number][]; spot: number }) {
  if (curve.length < 2) return null;
  const w = 640, h = 220, pad = 24;
  const xs = curve.map((p) => p[0]), ys = curve.map((p) => p[1]);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  const [y0, y1] = [Math.min(0, ...ys), Math.max(0, ...ys)];
  const X = (x: number) => pad + ((x - x0) / (x1 - x0 || 1)) * (w - 2 * pad);
  const Y = (y: number) => h - pad - ((y - y0) / (y1 - y0 || 1)) * (h - 2 * pad);
  const path = curve.map(([x, y], i) => `${i ? "L" : "M"}${X(x).toFixed(1)},${Y(y).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full max-w-2xl rounded border border-border bg-background">
      <line x1={pad} x2={w - pad} y1={Y(0)} y2={Y(0)} stroke="currentColor" strokeOpacity={0.3} />
      <line x1={X(spot)} x2={X(spot)} y1={pad} y2={h - pad} stroke="currentColor" strokeDasharray="4 4" strokeOpacity={0.4} />
      <path d={path} fill="none" stroke="#0ea5e9" strokeWidth={2} />
      <text x={X(spot) + 4} y={pad + 10} fontSize={10} fill="currentColor">spot {spot}</text>
    </svg>
  );
}

export default function Fno() {
  const [symbol, setSymbol] = useState("NIFTY");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("");
  const [chain, setChain] = useState<Chain | null>(null);
  const [legs, setLegs] = useState<Leg[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const e = await api<{ expiries: string[] }>(`/api/fno/${encodeURIComponent(symbol)}/expiries`);
      setExpiries(e.expiries);
      setExpiry(e.expiries[0] ?? "");
      setChain(null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  };
  const loadChain = async (exp: string) => {
    setExpiry(exp);
    try {
      setChain(await api<Chain>(`/api/fno/${encodeURIComponent(symbol)}/chain?expiry=${exp}`));
      setResult(null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  };
  const analyse = async () => {
    try {
      setResult(await api<Result>("/api/fno/strategy", { method: "POST", body: JSON.stringify({ symbol, expiry, legs }) }));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  };
  const add = (right: Leg["right"], strike: string, side: Leg["side"]) => setLegs([...legs, { right, strike, side, lots: 1 }]);

  const near = chain && chain.atm_strike ? chain.rows.filter((r) => Math.abs(Number(r.strike) - Number(chain.atm_strike)) <= Number(chain.atm_strike) * 0.03) : chain?.rows ?? [];

  return (
    <div className="space-y-4">
      <Card title="F&O analytics (NSE option chain · analysis only, no orders)">
        <div className="flex flex-wrap gap-2">
          <input className={field} value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} placeholder="NIFTY, BANKNIFTY, INFY…" />
          <Button onClick={load}>Load expiries</Button>
          {expiries.length > 0 && (
            <select className={field} value={expiry} onChange={(e) => loadChain(e.target.value)}>
              <option value="">Pick an expiry</option>
              {expiries.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </select>
          )}
          {expiry && <Button onClick={() => loadChain(expiry)}>Load chain</Button>}
        </div>
        <div className="mt-2">
          <ErrorNote error={error} />
        </div>
        {chain && (
          <p className="mt-2 text-sm">
            {chain.symbol} {chain.underlying} · {when(chain.as_of)} · lot {chain.lot_size ?? "?"} · ATM {chain.atm_strike} (IV call{" "}
            {chain.atm_iv.call}%, put {chain.atm_iv.put}%) · PCR (OI) {chain.pcr_oi ? Number(chain.pcr_oi).toFixed(2) : "—"} · max pain {chain.max_pain}
          </p>
        )}
      </Card>

      {chain && (
        <Card title="Option chain near the money">
          <table className="w-full text-xs">
            <thead className="text-muted">
              <tr>
                <th>Call OI</th>
                <th>Call IV</th>
                <th>Call LTP</th>
                <th />
                <th className="py-1">Strike</th>
                <th />
                <th>Put LTP</th>
                <th>Put IV</th>
                <th>Put OI</th>
              </tr>
            </thead>
            <tbody>
              {near.map((r) => (
                <tr key={r.strike} className={`border-t border-border text-center ${r.strike === chain.atm_strike ? "bg-sky-50 dark:bg-sky-950" : ""}`}>
                  <td>{r.call?.oi}</td>
                  <td>{r.call?.iv}</td>
                  <td>{r.call?.last_price}</td>
                  <td>
                    <button className="underline" onClick={() => add("call", r.strike, "buy")}>B</button>/
                    <button className="underline" onClick={() => add("call", r.strike, "sell")}>S</button>
                  </td>
                  <td className="py-1 font-medium">{r.strike}</td>
                  <td>
                    <button className="underline" onClick={() => add("put", r.strike, "buy")}>B</button>/
                    <button className="underline" onClick={() => add("put", r.strike, "sell")}>S</button>
                  </td>
                  <td>{r.put?.last_price}</td>
                  <td>{r.put?.iv}</td>
                  <td>{r.put?.oi}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {legs.length > 0 && (
        <Card title="Strategy" actions={<Button onClick={analyse}>Analyse</Button>}>
          <ul className="space-y-1 text-sm">
            {legs.map((l, i) => (
              <li key={i} className="flex items-center gap-2">
                <span className="w-10">{l.side}</span>
                <span className="w-10">{l.right}</span>
                <span className="w-20">{l.strike}</span>
                <input className={`${field} w-16`} type="number" min={1} value={l.lots}
                  onChange={(e) => setLegs(legs.map((x, j) => (j === i ? { ...x, lots: Math.max(1, Number(e.target.value)) } : x)))} />
                <span className="text-muted">lots</span>
                <button className="text-rose-600" onClick={() => setLegs(legs.filter((_, j) => j !== i))}>remove</button>
              </li>
            ))}
          </ul>
          {result && chain && (
            <div className="mt-4 space-y-2 text-sm">
              <Payoff curve={result.curve} spot={Number(chain.underlying)} />
              <p>
                Breakevens {result.breakevens.join(", ") || "none"} · max profit {inr(result.max_profit)} · max loss {inr(result.max_loss)} · net premium{" "}
                {inr(result.net_premium)} · probability of profit {result.probability_of_profit == null ? "—" : `${(result.probability_of_profit * 100).toFixed(1)}%`}
              </p>
              <p className="text-xs text-muted">
                Net greeks: delta {result.net_greeks.delta.toFixed(1)} · gamma {result.net_greeks.gamma.toFixed(3)} · vega {result.net_greeks.vega.toFixed(1)} · theta/day{" "}
                {result.net_greeks.theta.toFixed(1)} (lot size {result.lot_size})
              </p>
              {result.notes.map((n) => <p key={n} className="text-xs text-amber-600">{n}</p>)}
              <p className="text-xs text-muted">{result.disclaimer}</p>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
