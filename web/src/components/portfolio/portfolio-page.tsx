"use client";

// /portfolio: holdings with P&L and XIRR, allocation, invested and realised over time, dividends, tax and imports.
// Personal data from the local API only (never sent to an LLM). Signals load per row on demand, so the table does
// not fire one NSE/AMFI request per holding.

import { Activity, BadgeIndianRupee, Briefcase, ChevronRight, Coins, Info, PieChart, PlugZap, Receipt, Upload, Wallet } from "lucide-react";
import Link from "next/link";
import { Fragment, useCallback, useEffect, useState } from "react";

import { DonutChart, TimeSeriesChart, fmtCompactINR } from "@/components/charts";
import type { Signal } from "@/components/signal";
import { Badge, Button, Callout, Card, EmptyState, ErrorNote, Field, InfoTip, Modal, PageHeader, Segmented, SkeletonRows, Stat, Table, cx, inputClass } from "@/components/ui";
import { API_URL, api, day, useApi, when } from "@/lib/api";

import { ImportPanel } from "./import-panel";
import { TaxPanel } from "./tax-panel";
import { ASSET_CLASSES, type Holding, type HoldingDetail, type Snapshot, TAX_CLASS_LABEL, type TaxClass, inr, pctx, signed, units } from "./types";

type Tab = "holdings" | "allocation" | "pnl" | "dividends" | "tax" | "import";
const TABS: { value: Tab; label: string }[] = [
  { value: "holdings", label: "Holdings" }, { value: "allocation", label: "Allocation" }, { value: "pnl", label: "P&L" },
  { value: "dividends", label: "Dividends" }, { value: "tax", label: "Tax" }, { value: "import", label: "Import" },
];
const POSITIVE = new Set(["BUY", "ACCUMULATE"]);
const NEGATIVE = new Set(["SELL", "REDUCE", "AVOID"]);

function SignalBadge({ s }: { s: NonNullable<Holding["signal"]> }) {
  const [state, setState] = useState<"idle" | "loading" | Signal | string>("idle");
  const load = async () => {
    setState("loading");
    try {
      setState(await api<Signal>(`/api/signals/${s.asset}/${encodeURIComponent(s.instrument)}`));
    } catch (e) {
      setState((e as Error).message);
    }
  };
  if (state === "idle") return <button type="button" onClick={load} className="inline-flex items-center gap-1 text-[11px] font-medium text-brand hover:underline"><Activity className="size-3" />Signal</button>;
  if (state === "loading") return <span className="text-[11px] text-muted">loading…</span>;
  if (typeof state === "string") return <span className="text-[11px] text-muted" title={state}>no signal</span>;
  const tone = POSITIVE.has(state.action) ? "gain" : NEGATIVE.has(state.action) ? "loss" : "neutral";
  return (
    <Link href={s.href} title={`${state.method} · ${state.validation.status}`} className="inline-flex">
      <Badge tone={tone}>{state.action.replaceAll("_", " ").toLowerCase()}{state.validation.status === "uncalibrated" ? " ·?" : ""}</Badge>
    </Link>
  );
}

function HoldingEditor({ h, onSaved }: { h: Holding; onSaved: () => void }) {
  const { data, error, reload } = useApi<HoldingDetail>(`/api/portfolio/holdings/${h.id}`);
  const [f, setF] = useState({ tax_class: h.tax_class_override ?? "", nse_symbol: h.nse_symbol ?? "", scheme_code: h.scheme_code ?? "",
    sector: h.sector ?? "", fmv_2018: h.fmv_2018 != null ? String(h.fmv_2018) : "", sgb: h.sgb_original_subscriber });
  const [msg, setMsg] = useState<string | null>(null);
  const [opening, setOpening] = useState<Record<number, { price: string; acquired: string }>>({});
  const save = async () => {
    setMsg(null);
    try {
      await api(`/api/portfolio/holdings/${h.id}`, { method: "PUT", body: JSON.stringify({
        tax_class: f.tax_class || null, nse_symbol: f.nse_symbol || null, scheme_code: f.scheme_code || null, sector: f.sector || null,
        fmv_2018: f.fmv_2018 || null, flags: { sgb_original_subscriber: f.sgb } }) });
      setMsg("Saved");
      onSaved();
    } catch (e) { setMsg((e as Error).message); }
  };
  const saveOpening = async (id: number) => {
    const o = opening[id];
    try {
      await api(`/api/portfolio/transactions/${id}`, { method: "PUT", body: JSON.stringify({ price: o.price, meta: { acquired: o.acquired } }) });
      reload(); onSaved();
    } catch (e) { setMsg((e as Error).message); }
  };
  const delTxn = async (id: number) => {
    await api(`/api/portfolio/transactions/${id}`, { method: "DELETE" });
    reload(); onSaved();
  };
  return (
    <div className="max-h-[70vh] space-y-4 overflow-y-auto px-4 py-4 sm:px-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={<span className="inline-flex items-center gap-1">Tax class <InfoTip>Detected: {TAX_CLASS_LABEL[h.tax_class_auto]} ({h.tax_class_why}). The equity rates need a fund with at least 65 % in Indian equity; the app maps SEBI categories and names, so check it.</InfoTip></span>}>
          <select value={f.tax_class} onChange={(e) => setF({ ...f, tax_class: e.target.value })} className={cx(inputClass, "w-full")}>
            <option value="">Auto: {TAX_CLASS_LABEL[h.tax_class_auto]}</option>
            {(Object.keys(TAX_CLASS_LABEL) as TaxClass[]).map((k) => <option key={k} value={k}>{TAX_CLASS_LABEL[k]}</option>)}
          </select>
        </Field>
        {h.asset_type === "mf"
          ? <Field label="AMFI scheme code"><input value={f.scheme_code} onChange={(e) => setF({ ...f, scheme_code: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
          : <Field label="NSE symbol"><input value={f.nse_symbol} onChange={(e) => setF({ ...f, nse_symbol: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>}
        <Field label="Sector"><input value={f.sector} onChange={(e) => setF({ ...f, sector: e.target.value })} className={cx(inputClass, "w-full")} placeholder="from NSE" /></Field>
        <Field label={<span className="inline-flex items-center gap-1">FMV 31-Jan-2018 (₹/unit) <InfoTip>Grandfathering: for equity bought before 1-Feb-2018, the cost is the higher of what you paid and the lower of this price and the sale price. Use the day&apos;s highest traded price (NAV for funds).</InfoTip></span>}>
          <input inputMode="decimal" value={f.fmv_2018} onChange={(e) => setF({ ...f, fmv_2018: e.target.value })} className={cx(inputClass, "w-full num")} />
        </Field>
        {h.tax_class === "sgb" && <Field label="SGB bought at issue"><label className="flex h-9 items-center gap-2 text-sm"><input type="checkbox" checked={f.sgb} onChange={(e) => setF({ ...f, sgb: e.target.checked })} />Original subscriber</label></Field>}
      </div>
      <div className="flex items-center gap-3"><Button onClick={save}>Save</Button>{msg && <span className="text-xs text-muted">{msg}</span>}</div>
      {error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <SkeletonRows rows={3} /> : (
        <>
          {data.warnings.length > 0 && <ul className="rounded-lg bg-warn-soft px-3 py-2 text-xs">{data.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
          <div>
            <p className="mb-1 text-xs font-medium">Open lots (FIFO order)</p>
            <Table>
              <thead><tr><th>Acquired</th><th>Origin</th><th className="text-right">Units</th><th className="text-right">Open</th><th className="text-right">Cost / unit</th></tr></thead>
              <tbody>{data.lots.filter((l) => Number(l.open_quantity) > 0).map((l) => (
                <tr key={l.id}><td className="num text-xs">{l.acquired ? day(l.acquired) : "unknown"}</td><td className="text-xs">{l.origin}</td>
                  <td className="num text-right">{units(l.quantity)}</td><td className="num text-right">{units(l.open_quantity)}</td>
                  <td className="num text-right">{l.cost_per_unit == null ? <Badge tone="warn">unknown</Badge> : inr(Number(l.cost_per_unit), 2)}</td></tr>
              ))}</tbody>
            </Table>
          </div>
          <details>
            <summary className="cursor-pointer text-xs font-medium">Transactions ({data.transactions.length})</summary>
            <Table className="mt-2">
              <thead><tr><th>Date</th><th>Kind</th><th className="text-right">Units</th><th className="text-right">Price</th><th className="text-right">Amount</th><th>Source</th><th /></tr></thead>
              <tbody>{data.transactions.map((t) => (
                <tr key={t.id}>
                  <td className="num text-xs">{day(t.day)}</td>
                  <td className="text-xs">{t.kind}{t.kind === "bonus" && ` ${t.meta.a}:${t.meta.b}`}{t.kind === "split" && ` ₹${t.meta.from}→₹${t.meta.to}`}{t.meta.reinvest ? " (reinvested)" : ""}</td>
                  <td className="num text-right">{units(t.quantity)}</td>
                  <td className="num text-right">{t.price ?? "—"}</td>
                  <td className="num text-right">{t.amount ?? "—"}</td>
                  <td className="text-xs text-muted">{t.source}</td>
                  <td className="text-right">
                    {t.kind === "opening" && t.price == null ? (
                      <span className="inline-flex items-center gap-1">
                        <input type="date" aria-label="Acquired on" className={cx(inputClass, "h-7 w-32 text-xs")} onChange={(e) => setOpening({ ...opening, [t.id]: { ...(opening[t.id] ?? { price: "" }), acquired: e.target.value } })} />
                        <input placeholder="cost/unit" inputMode="decimal" className={cx(inputClass, "h-7 w-20 text-xs")} onChange={(e) => setOpening({ ...opening, [t.id]: { ...(opening[t.id] ?? { acquired: "" }), price: e.target.value } })} />
                        <Button variant="secondary" disabled={!opening[t.id]?.price || !opening[t.id]?.acquired} onClick={() => saveOpening(t.id)}>Set cost</Button>
                      </span>
                    ) : t.source === "manual" || t.source === "nse_actions" ? <Button variant="ghost" onClick={() => delTxn(t.id)}>Delete</Button> : null}
                  </td>
                </tr>
              ))}</tbody>
            </Table>
          </details>
        </>
      )}
    </div>
  );
}

const SOURCE_LABEL: Record<string, string> = {
  manual: "manual", cas: "CAS", nse_actions: "corp. action", zerodha: "Zerodha CSV", groww: "Groww CSV", upstox: "Upstox CSV",
  groww_api: "Groww sync", zerodha_api: "Zerodha sync", upstox_api: "Upstox sync", dhan_api: "Dhan sync",
  groww_holdings: "Groww holdings file", zerodha_holdings: "Zerodha holdings file", upstox_holdings: "Upstox holdings file",
};

function SourceBadge({ source }: { source: string }) {
  if (source === "manual" || source === "nse_actions") return null;
  return <Badge tone={source.endsWith("_api") ? "brand" : "neutral"}>{SOURCE_LABEL[source] ?? source}</Badge>;
}

type ConnSummary = { key: string; label: string; status: string; last_sync_at: string | null; last_error: string | null };

/** "Groww synced 2 h ago · Zerodha: log in again" — the broker connections behind the numbers, linking to Profile. */
function SyncStrip() {
  const { data } = useApi<ConnSummary[]>("/api/connections/summary", 120000);
  if (!data) return null;
  return (
    <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted">
      <PlugZap className="size-3.5" />
      {data.length === 0 ? (
        <Link href="/profile#connections" className="text-brand hover:underline">Connect a broker (Groww, Zerodha, Upstox, Dhan) or set up the statement inbox</Link>
      ) : data.map((c) => (
        <Link key={c.key} href="/profile#connections" className="inline-flex items-center gap-1.5 rounded-md bg-background-subtle px-2 py-1 ring-1 ring-inset ring-border hover:text-foreground">
          <span className={cx("size-1.5 rounded-full", c.status === "connected" ? "bg-gain" : c.status === "reconnect" || c.status === "error" ? "bg-warn" : "bg-muted")} />
          <span className="font-medium text-foreground">{c.label}</span>
          <span>{c.key === "cas_inbox" ? (c.status === "connected" ? "watching the folder" : "off") : c.status === "reconnect" ? "log in again" : c.last_sync_at ? `synced ${when(c.last_sync_at)}` : "not synced yet"}</span>
        </Link>
      ))}
    </div>
  );
}

function Holdings({ snap, onChanged, updating }: { snap: Snapshot; onChanged: () => void; updating: { done: number; total: number } | null }) {
  const [open, setOpen] = useState<number | null>(null);
  const [showClosed, setShowClosed] = useState(false);
  const rows = snap.holdings.filter((h) => showClosed || !h.closed);
  const openH = snap.holdings.find((h) => h.id === open) ?? null;
  const closed = snap.holdings.length - snap.holdings.filter((h) => !h.closed).length;
  return (
    <Card padded={false} title="Holdings" subtitle={updating
      ? <span className="inline-flex items-center gap-1.5"><span className="size-1.5 rounded-full bg-info animate-pulse-ring" />prices updating… {updating.done} of {updating.total} · cost basis shown</span>
      : `${snap.summary.holdings} open · priced ${day(snap.as_of)}`}
      actions={closed > 0 ? <button type="button" className="text-xs text-brand" onClick={() => setShowClosed((s) => !s)}>{showClosed ? "Hide" : "Show"} {closed} closed</button> : undefined}>
      <Table className="!mx-0">
        <thead>
          <tr>
            <th>Holding</th><th className="text-right">Value</th><th className="text-right">Unrealised</th>
            <th className="text-right"><span className="inline-flex items-center gap-1">XIRR <InfoTip>Annualised return on your actual cash flows (buys, sales, dividends paid out, today&apos;s value). Not shown for holdings younger than 60 days or with an unknown cost.</InfoTip></span></th>
            <th className="text-right">Units</th><th className="text-right">Avg cost</th><th className="text-right">Price</th>
            <th className="text-right">Realised</th><th>Signal</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((h) => (
            <Fragment key={h.id}>
              <tr className="cursor-pointer" onClick={() => setOpen(open === h.id ? null : h.id)}>
                <td className="max-w-[12rem] sm:max-w-[17rem]">
                  <span className="flex items-center gap-1.5">
                    <ChevronRight className="size-3.5 shrink-0 text-muted" />
                    <span className="truncate font-medium">{h.name}</span>
                  </span>
                  <span className="ml-5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
                    <span className="truncate">{h.account}</span>·<span>{TAX_CLASS_LABEL[h.tax_class]}</span>
                    {(h.sources ?? []).map((src) => <SourceBadge key={src} source={src} />)}
                    {h.broker_baseline && <Badge tone="neutral">broker avg cost</Badge>}
                    {!h.cost_known && <Badge tone="warn">cost unknown</Badge>}
                    {h.warnings.length > 0 && <Badge tone="warn">{h.warnings.length} note{h.warnings.length > 1 ? "s" : ""}</Badge>}
                  </span>
                </td>
                <td className="num text-right font-medium">{h.pending ? <span className="skeleton inline-block h-3 w-14 rounded align-middle" /> : inr(h.value)}</td>
                <td className={cx("num text-right", (h.unrealised ?? 0) >= 0 ? "text-gain" : "text-loss")}>
                  {h.pending ? <span className="skeleton inline-block h-3 w-14 rounded align-middle" /> : <>{signed(h.unrealised)}<span className="block text-[10px]">{pctx(h.unrealised_pct)}</span></>}
                </td>
                <td className="num text-right" title={h.xirr_reason ?? ""}>{h.pending ? <span className="text-xs text-muted">…</span> : h.xirr == null ? <span className="text-xs text-muted">—</span> : pctx(h.xirr * 100)}</td>
                <td className="num text-right">{units(h.units)}</td>
                <td className="num text-right">{inr(h.avg_cost, 2)}</td>
                <td className="num text-right" title={h.price_source ? `${h.price_source}${h.price_as_of ? ` · as of ${h.price_as_of}` : ""}` : undefined}>
                  {h.pending ? <span className="skeleton inline-block h-3 w-14 rounded align-middle" aria-label="price updating" /> : h.price == null ? <span className="text-xs text-muted" title={h.price_error ?? ""}>no price</span> : inr(h.price, 2)}
                  {h.price_source?.includes("statement") && <span className="block text-[10px] text-warn">statement NAV {day(h.price_as_of)}</span>}
                  {!h.pending && h.price_source?.includes("close (official)") && <span className="block text-[10px] text-muted">close {day(h.price_as_of)}</span>}
                </td>
                <td className={cx("num text-right", h.realised > 0 ? "text-gain" : h.realised < 0 ? "text-loss" : "text-muted")}>{h.realised ? signed(h.realised) : "—"}</td>
                <td onClick={(e) => e.stopPropagation()}>{h.signal ? <SignalBadge s={h.signal} /> : <span className="text-[11px] text-muted">—</span>}</td>
              </tr>
            </Fragment>
          ))}
        </tbody>
      </Table>
      <Modal wide open={openH != null} onClose={() => setOpen(null)}
        title={openH ? <span>{openH.name} <span className="font-normal text-muted">· {openH.account}</span></span> : null}>
        {openH && <HoldingEditor h={openH} onSaved={onChanged} />}
      </Modal>
    </Card>
  );
}

function Targets({ snap, onSaved }: { snap: Snapshot; onSaved: () => void }) {
  const [t, setT] = useState<Record<string, string>>(() =>
    Object.fromEntries(ASSET_CLASSES.map((k) => [k, snap.targets?.[k] != null ? String(snap.targets[k]) : ""])));
  const [msg, setMsg] = useState<string | null>(null);
  const total = Object.values(t).reduce((a, v) => a + (Number(v) || 0), 0);
  const save = async () => {
    setMsg(null);
    try {
      const body = Object.fromEntries(Object.entries(t).filter(([, v]) => v.trim()).map(([k, v]) => [k, Number(v)]));
      await api("/api/portfolio/targets", { method: "PUT", body: JSON.stringify(body) });
      setMsg("Saved");
      onSaved();
    } catch (e) { setMsg((e as Error).message); }
  };
  const drift = snap.drift ?? [];
  return (
    <Card title="Target allocation and drift" icon={<PieChart className="size-4" />}
      help="Your target weight per asset class. Drift is today's weight minus the target, in percentage points; a common rule is to rebalance beyond ±5 pp. The 'Allocation drift' alert rule reads it.">
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {ASSET_CLASSES.map((k) => (
          <Field key={k} label={k}>
            <input inputMode="decimal" value={t[k]} onChange={(e) => setT({ ...t, [k]: e.target.value })} placeholder="0" className={cx(inputClass, "w-full num")} />
          </Field>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button onClick={save} disabled={total !== 0 && Math.abs(total - 100) > 0.5}>Save targets</Button>
        <span className={cx("num text-xs", Math.abs(total - 100) > 0.5 && total !== 0 ? "text-warn" : "text-muted")}>total {total.toFixed(1)} %</span>
        {msg && <span className="text-xs text-muted">{msg}</span>}
      </div>
      {drift.length > 0 && (
        <Table className="mt-4">
          <thead><tr><th>Asset class</th><th className="text-right">Weight</th><th className="text-right">Target</th><th className="text-right">Drift</th></tr></thead>
          <tbody>{drift.map((d) => (
            <tr key={d.label}><td>{d.label}</td><td className="num text-right">{d.weight_pct.toFixed(1)}%</td><td className="num text-right">{d.target_pct.toFixed(1)}%</td>
              <td className={cx("num text-right", Math.abs(d.drift_pp) >= 5 ? "font-medium text-warn" : "text-muted")}>{d.drift_pp > 0 ? "+" : ""}{d.drift_pp.toFixed(1)} pp</td></tr>
          ))}</tbody>
        </Table>
      )}
    </Card>
  );
}

function Allocation({ snap, onChanged }: { snap: Snapshot; onChanged: () => void }) {
  const donut = (rows: { label: string; value: number }[]) => {
    const top = rows.slice(0, 5);
    const rest = rows.slice(5).reduce((a, r) => a + r.value, 0);
    return [...top.map((r) => ({ name: r.label, value: r.value })), ...(rest > 0 ? [{ name: "Other", value: rest }] : [])];
  };
  const total = snap.summary.value;
  const center = <div><p className="num text-lg font-semibold">{fmtCompactINR(total)}</p><p className="text-[11px] text-muted">valued</p></div>;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="By asset class" icon={<PieChart className="size-4" />}><DonutChart data={donut(snap.allocation.asset)} center={center} format={fmtCompactINR} height={170} /></Card>
      <Card title="By sector" icon={<PieChart className="size-4" />} help="Stocks use NSE's industry (or your own label). Funds are not looked through to their holdings.">
        <DonutChart data={donut(snap.allocation.sector)} center={center} format={fmtCompactINR} height={170} />
      </Card>
      <Card title="By market cap" icon={<PieChart className="size-4" />}
        help={`SEBI: large cap = top 100 companies by market cap, mid = 101-250, small = the rest. Cut-offs: ${snap.cap_list.note} (large ≥ ₹${Number(snap.cap_list.large_min_cr).toLocaleString("en-IN")} cr, mid ≥ ₹${Number(snap.cap_list.mid_min_cr).toLocaleString("en-IN")} cr). Funds by their SEBI category.`}>
        <DonutChart data={donut(snap.allocation.cap)} center={center} format={fmtCompactINR} height={170} />
      </Card>
      <div className="lg:col-span-2"><Targets snap={snap} onSaved={onChanged} /></div>
    </div>
  );
}

function Pnl({ snap }: { snap: Snapshot }) {
  const tl = snap.timeline;
  if (tl.length < 2) return <EmptyState title="Not enough history yet">Import a statement or tradebook with at least two months of activity.</EmptyState>;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Net invested over time" help="Cumulative purchases minus sale proceeds at each month end. Past market values are not stored, so there is no value line.">
        <TimeSeriesChart data={tl} series={[{ key: "invested", label: "Net invested", color: "var(--chart-1)" }]} format={fmtCompactINR} ranges={["1Y", "3Y", "ALL"]} defaultRange="ALL" showChange={false} />
      </Card>
      <Card title="Realised P&L and dividends (cumulative)" help="Gains booked on sales (FIFO) and dividends received, added up month by month. Two series on one ₹ axis.">
        <TimeSeriesChart data={tl} area={false} series={[{ key: "realised", label: "Realised P&L", color: "var(--chart-2)" }, { key: "dividends", label: "Dividends", color: "var(--chart-3)" }]}
          format={fmtCompactINR} ranges={["1Y", "3Y", "ALL"]} defaultRange="ALL" showChange={false} />
      </Card>
    </div>
  );
}

function Dividends({ snap }: { snap: Snapshot }) {
  const d = snap.dividends;
  if (!d.items.length) return <EmptyState icon={<Coins className="size-5" />} title="No dividends recorded">Dividends come from CAS statements (IDCW payouts and reinvestments) or can be added by hand.</EmptyState>;
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
      <Card title="By financial year" subtitle={d.note}>
        <Table><thead><tr><th>Year</th><th className="text-right">Amount</th></tr></thead>
          <tbody>{d.by_fy.map((y) => <tr key={y.fy}><td>{y.label}</td><td className="num text-right">{inr(y.amount)}</td></tr>)}</tbody></Table>
      </Card>
      <Card title="Payments">
        <Table><thead><tr><th>Date</th><th>Holding</th><th className="text-right">Amount</th><th /></tr></thead>
          <tbody>{d.items.slice(0, 100).map((it, i) => (
            <tr key={i}><td className="num text-xs">{day(it.day)}</td><td className="max-w-[18rem] truncate">{it.name}</td><td className="num text-right">{inr(it.amount, 2)}</td>
              <td>{it.reinvested && <Badge tone="info">reinvested</Badge>}</td></tr>
          ))}</tbody></Table>
      </Card>
    </div>
  );
}

/** The portfolio, progressively: cost basis at once from the price cache (no network), then each price as it arrives
 * over /api/portfolio/prices/stream (concurrent, rate-limited, cached for 10 minutes), then the full valuation (XIRR,
 * allocation, summary) from the now-warm cache. */
function useProgressivePortfolio(refresh: number) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updating, setUpdating] = useState<{ done: number; total: number } | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    let alive = true;
    const ctl = new AbortController();
    (async () => {
      try {
        const first = await api<Snapshot>(`/api/portfolio?prices=cached&r=${refresh}`);
        if (!alive) return;
        setData(first);
        setError(null);
        const total = first.pending ?? 0;
        if (total > 0) {
          setUpdating({ done: 0, total });
          const res = await fetch(`${API_URL}/api/portfolio/prices/stream`, { signal: ctl.signal, cache: "no-store" });
          const reader = res.body?.getReader();
          const dec = new TextDecoder();
          let buf = "", done = 0;
          while (reader) {
            const { value, done: end } = await reader.read();
            if (end) break;
            buf += dec.decode(value, { stream: true });
            const lines = buf.split("\n");
            buf = lines.pop() ?? "";
            for (const line of lines.filter(Boolean)) {
              const e = JSON.parse(line) as { type: string; id: number; price: number | null; as_of: string | null; source: string | null; error: string | null };
              if (e.type !== "price" || !alive) continue;
              done += 1;
              setUpdating({ done: Math.min(done, total), total });
              setData((d) => d && {
                ...d,
                holdings: d.holdings.map((h) => {
                  if (h.id !== e.id || !h.pending) return h;
                  const value = e.price == null ? null : e.price * h.units;
                  const unrealised = value != null && h.cost_known && h.cost != null ? value - h.cost : null;
                  return { ...h, pending: false, price: e.price, price_as_of: e.as_of, price_source: e.source, price_error: e.error, value, unrealised,
                    unrealised_pct: unrealised != null && h.cost ? (unrealised / h.cost) * 100 : null };
                }),
              });
            }
          }
          const full = await api<Snapshot>(`/api/portfolio?r=${refresh}`);
          if (alive) setData(full);
        }
      } catch (e) {
        if (alive && (e as Error).name !== "AbortError") setError((e as Error).message);
      } finally {
        if (alive) setUpdating(null);
      }
    })();
    return () => { alive = false; ctl.abort(); };
  }, [refresh, tick]);
  return { data, error, reload, updating };
}

export function PortfolioPage() {
  const [tab, setTab] = useState<Tab>("holdings");
  const [refresh, setRefresh] = useState(0);
  const { data, error, reload, updating } = useProgressivePortfolio(refresh);
  const changed = useCallback(() => setRefresh((r) => r + 1), []);
  // the tab lives in the URL hash so a link or reload lands on the same view
  useEffect(() => {
    const sync = () => {
      const h = window.location.hash.slice(1) as Tab;
      setTab(TABS.some((t) => t.value === h) ? h : "holdings");
    };
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  const go = (t: Tab) => { setTab(t); history.replaceState(null, "", `#${t}`); };
  const s = data?.summary;
  const empty = data && data.holdings.length === 0;

  return (
    <div>
      <PageHeader icon={<Briefcase className="size-5" />} title="Portfolio" eyebrow="You"
        description="Your holdings from broker connections, CAS statements, tradebooks and manual entries: FIFO lots, P&L, XIRR, allocation and capital-gains tax."
        actions={<Button variant="secondary" icon={<Upload className="size-3.5" />} onClick={() => go("import")}>Import</Button>} />
      {error && <ErrorNote error={error} onRetry={reload} />}
      {!data && !error && <SkeletonRows rows={6} />}
      {data && (
        <>
          <div className="stagger mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Current value" value={s!.value} display={updating ? <span className="skeleton inline-block h-7 w-32 rounded" aria-label="updating" /> : undefined} format={(n) => inr(n)} icon={<Wallet className="size-4" />}
              hint={updating ? `prices updating… ${updating.done}/${updating.total}` : s!.unpriced ? `${s!.unpriced} holding(s) without a price` : `cost ${inr(s!.cost)}`} />
            <Stat label="Unrealised P&L" value={s!.unrealised} display={updating ? <span className="skeleton inline-block h-7 w-32 rounded" aria-label="updating" /> : undefined} format={(n) => signed(n)} icon={<BadgeIndianRupee className="size-4" />} tone={s!.unrealised >= 0 ? "gain" : "loss"}
              hint={updating ? "waiting for prices" : s!.unknown_cost ? `${s!.unknown_cost} holding(s) with unknown cost excluded` : "value − cost of open lots"} />
            <Stat label="Realised P&L" value={s!.realised} format={(n) => signed(n)} icon={<Receipt className="size-4" />} tone={s!.realised >= 0 ? "gain" : "loss"}
              hint={`+ dividends ${inr(s!.dividends)}`} />
            <Stat label="XIRR" display={updating ? <span className="skeleton inline-block h-7 w-32 rounded" aria-label="updating" /> : s!.xirr == null ? <span className="text-muted">—</span> : <span className="num">{pctx(s!.xirr * 100)}</span>}
              icon={<Activity className="size-4" />} hint={updating ? "waiting for prices" : s!.xirr_reason ?? "annualised, all holdings"}
              help="Money-weighted annual return over every cash flow: purchases, sales, dividends paid out and today's value." />
          </div>
          <SyncStrip />
          <div className="mb-4 overflow-x-auto"><Segmented value={tab} onChange={go} options={TABS} /></div>
          {empty && tab !== "import" ? (
            <EmptyState icon={<Briefcase className="size-5" />} title="No holdings yet" action={<Button onClick={() => go("import")}>Import a statement</Button>}>
              Import your CAMS/KFintech CAS for mutual funds and your broker tradebook for shares, or add transactions by hand.
            </EmptyState>
          ) : (
            <div key={tab} className="animate-fade-up">
              {tab === "holdings" && <Holdings snap={data} onChanged={changed} updating={updating} />}
              {tab === "allocation" && <Allocation snap={data} onChanged={changed} />}
              {tab === "pnl" && <Pnl snap={data} />}
              {tab === "dividends" && <Dividends snap={data} />}
              {tab === "tax" && <TaxPanel refresh={refresh} />}
              {tab === "import" && <ImportPanel onChanged={changed} />}
            </div>
          )}
          <p className="mt-6 flex items-start gap-1.5 text-[11px] text-muted"><Info className="mt-0.5 size-3 shrink-0" />{data.privacy} Personal research, not investment or tax advice.</p>
        </>
      )}
      {!data && error && tab === "import" && <ImportPanel onChanged={changed} />}
      {data && s && s.unknown_cost > 0 && tab === "holdings" && (
        <div className="mt-4"><Callout tone="warn" title="Some costs are unknown">A one-year CAS starts with an opening balance whose cost and purchase date are not in the statement. Open the holding and set them (or import an older statement) to get P&L, XIRR and tax for those units.</Callout></div>
      )}
    </div>
  );
}
