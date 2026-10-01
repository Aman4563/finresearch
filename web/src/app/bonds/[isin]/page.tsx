"use client";

import {
  ArrowLeft, BadgeCheck, BadgePercent, CalendarRange, Coins, FlaskConical, Gauge as GaugeIcon, Landmark, PiggyBank, Receipt, SlidersHorizontal, TriangleAlert, Waves,
} from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { shortDate } from "@/components/charts";
import { ensureBond, startResearch } from "@/components/markets/actions";
import { CashFlowChart, PriceYieldChart } from "@/components/markets/charts";
import { BondLadderCard } from "@/components/markets/signal-charts";
import { Facts, Metric, inr, pctOf, ratingLabel, ratingTone, signedPct, toneOf } from "@/components/markets/common";
import type { BondAnalytics } from "@/components/markets/types";
import { SignalCard } from "@/components/signal";
import {
  Badge, Button, Callout, Card, ErrorNote, Field, InfoTip, PageHeader, Segmented, Skeleton, Stat, Table, cx, inputClass,
} from "@/components/ui";
import { day, type Profile, useApi, when } from "@/lib/api";

const FREQS = [
  { value: "1", label: "Yearly" },
  { value: "2", label: "Half-yearly" },
  { value: "4", label: "Quarterly" },
  { value: "12", label: "Monthly" },
];
const SLABS = ["0", "5", "10", "15", "20", "25", "30"];

export default function BondDetail() {
  const { isin: raw } = useParams<{ isin: string }>();
  const isin = decodeURIComponent(raw).toUpperCase();
  const router = useRouter();
  const profile = useApi<Profile>("/api/profile");
  const [freqChoice, setFreq] = useState<string | null>(null);  // null: the API uses verified research, else assumes yearly
  const [basis, setBasis] = useState<"dirty" | "clean">("dirty");
  const [slabChoice, setSlabChoice] = useState<string | null>(null);
  const slab = slabChoice ?? (profile.data ? String(Number(profile.data.tax_slab_pct)) : null);
  const [shiftBp, setShiftBp] = useState(0);
  const [fdDraft, setFdDraft] = useState("6.40");  // SBI 2-3 year card rate, w.e.f. 15-Dec-2025
  const [fdTouched, setFdTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const r = useApi<BondAnalytics>(slab != null || profile.error ? `/api/bonds/${isin}/analytics?${freqChoice ? `freq=${freqChoice}&` : ""}basis=${basis}${slab != null ? `&tax_slab_pct=${slab}` : ""}` : null);
  const d = r.data;
  const freq = freqChoice ?? (d ? String(d.freq) : "1");
  const fs = d?.freq_source;
  const b = d?.bond;
  const a = d?.analytics;
  const shifted = a?.curve.find((p) => p.bp === shiftBp) ?? null;
  const fd = Math.max(0, Number(fdDraft) || 0) / 100;
  const fdAfterTax = d ? fd * (1 - d.tax_rate) : null;
  // the signal uses the same assumptions as the page (it compares yields as effective yearly rates)
  const signalQuery: Record<string, string> = { basis, ...(freqChoice ? { freq: freqChoice } : {}), ...(slab != null ? { tax_slab_pct: slab } : {}), ...(fdTouched && Number(fdDraft) > 0 ? { fd: String(Math.min(20, Number(fdDraft))) } : {}) };

  const research = async () => {
    if (!b || !confirm(`Start a full research run for ${b.symbol} ${b.series ?? ""} (${b.isin})? It uses your Claude plan window.`)) return;
    setBusy(true);
    try {
      router.push(`/runs/${await startResearch(await ensureBond(b.isin), "bond_report")}`);
    } catch (e) {
      setActionError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div>
      <Link href="/bonds" className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-muted transition hover:text-brand">
        <ArrowLeft className="size-3.5" /> All bonds
      </Link>
      <PageHeader
        icon={<Landmark className="size-5" />}
        eyebrow={<span className="num">NSE capital market · {isin}</span>}
        title={b ? <span className="inline-flex flex-wrap items-center gap-2">{b.symbol} <span className="text-base font-normal text-muted">{b.series}</span>
          {b.rating ? <Badge tone={ratingTone(b.rating)}>{ratingLabel(b.rating)}{b.rating_agency ? ` · ${b.rating_agency}` : ""}</Badge> : <Badge tone="warn">unrated in NSE list</Badge>}</span>
          : r.error ? isin : <Skeleton className="h-7 w-56" />}
        description={b ? <>{b.coupon_pct}% coupon · face {inr(b.face_value, 0)} · matures {day(b.maturity)}{b.as_of && <> · price as of {when(b.as_of)}</>}</> : undefined}
        actions={<Button size="md" disabled={busy || !b} onClick={research} icon={<FlaskConical className="size-4" />}>Research</Button>}
      />

      <div className="space-y-5">
        <ErrorNote error={actionError} />
        {r.error && <ErrorNote error={r.error} onRetry={r.reload} />}

        <Card padded className="p-4" title="Assumptions" icon={<SlidersHorizontal className="size-4" />}
          subtitle="Change these to match the bond's offer document and your taxes; everything below recalculates">
          <div className="grid [&>*]:min-w-0 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label={<span className="inline-flex items-center gap-1">Coupon paid <InfoTip>How often interest is paid. NSE&apos;s list doesn&apos;t say: check the offer document or information memorandum. It changes the yield and accrued interest.</InfoTip></span>}>
              <Segmented value={freq} onChange={setFreq} options={FREQS.map((f) => ({ ...f, label: f.label.replace("-yearly", "-yr") }))} />
              {fs?.kind === "verified" && (
                <span className="mt-1.5 flex items-center gap-1 text-[11px] text-gain">
                  <BadgeCheck className="size-3.5" /> Verified in <Link href={`/runs/${fs.run_id}/report`} className="underline underline-offset-2">research run #{fs.run_id}</Link>
                </span>
              )}
              {fs?.kind === "assumed" && (
                <span className="mt-1.5 flex items-center gap-1 text-[11px] text-warn">
                  <TriangleAlert className="size-3.5" /> Assumed yearly: check the offer document, the yield depends on it
                </span>
              )}
            </Field>
            <Field label={<span className="inline-flex items-center gap-1">NSE price is <InfoTip>NSE states that capital-market bonds are traded and settled on the dirty price, i.e. including accrued interest. Switch to clean only if you know the quote excludes it.</InfoTip></span>}>
              <Segmented value={basis} onChange={setBasis} options={[{ value: "dirty", label: "Dirty (NSE)" }, { value: "clean", label: "Clean" }]} />
            </Field>
            <Field label="Your income-tax slab" hint={slabChoice == null && profile.data ? "From your profile; 4% cess is added" : "4% cess is added"}>
              <select className={cx(inputClass, "w-full")} value={slab ?? "30"} onChange={(e) => setSlabChoice(e.target.value)}>
                {SLABS.map((s) => <option key={s} value={s}>{s}%</option>)}
              </select>
            </Field>
            <Field label="Compare with an FD at (% a year)" hint={fdTouched ? "Your rate; interest is taxed at the same slab" : "SBI 2–3 yr card rate (w.e.f. 15-Dec-2025); the signal uses SBI's rate for the bond's tenor until you change this"}>
              <input className={cx(inputClass, "num w-full")} inputMode="decimal" value={fdDraft} onChange={(e) => { setFdTouched(true); setFdDraft(e.target.value.replace(/[^0-9.]/g, "")); }} />
            </Field>
          </div>
        </Card>

        {d?.warnings.length ? (
          <Callout tone="warn" icon={<TriangleAlert className="size-4" />} title="Check before relying on these numbers">
            <ul className="list-disc pl-4">{d.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
          </Callout>
        ) : null}
        {d?.error && <Callout tone="loss" title="Yield can't be computed">{d.error}</Callout>}

        {slab != null && (
          <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
            <SignalCard asset="bond" instrument={isin} query={signalQuery} title="Buy, hold or avoid?" />
            <BondLadderCard isin={isin} query={signalQuery} />
          </div>
        )}

        <div className="grid [&>*]:min-w-0 grid-cols-2 gap-3 lg:grid-cols-5 stagger">
          {!d ? (
            Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-[104px] rounded-xl" />)
          ) : a ? (
            <>
              <Stat label="Yield to maturity" icon={<BadgePercent className="size-4" />} value={a.ytm * 100} format={(n) => `${n.toFixed(2)}%`}
                hint="before tax, if held to maturity" help="YTM: the yearly return if you buy at today's price, receive every coupon and hold until the face value is repaid (coupons assumed reinvested at the same rate)." />
              <Stat label="After-tax yield" tone="brand" icon={<PiggyBank className="size-4" />} value={a.after_tax_ytm * 100} format={(n) => `${n.toFixed(2)}%`}
                hint={`at ${d.tax_slab_pct}% slab + cess`} help="The yield on what you keep: each coupon is taxed at your slab (plus 4% cess), the price paid includes the accrued interest, and the gap between face value and the clean price is a capital gain or loss at maturity. A loss is assumed not set off against other gains." />
              {/* de-emphasised (roadmap §B /bonds 2): for a premium bond it overstates the return, so it says so */}
              <Stat label="Current yield" tone="neutral" icon={<Coins className="size-4" />}
                display={<span className="num text-muted">{(a.current_yield * 100).toFixed(2)}%</span>}
                hint={a.current_yield > a.ytm ? "overstates: ignores the premium lost at maturity; use YTM" : "coupon ÷ clean price; ignores maturity gain"}
                help="This year's interest divided by the clean price. Ignores the gain or loss at maturity, so it overstates the return of a bond bought above face value. Yield to maturity is the figure to compare." />
              <Stat label="Modified duration" tone="accent" icon={<Waves className="size-4" />} value={a.modified_duration} format={(n) => `${n.toFixed(2)}`}
                hint={`≈ −${a.modified_duration.toFixed(2)}% if rates +1%`} help="How sensitive the price is to interest rates: if yields rise by 1 percentage point, the price falls by about this many percent (and rises if yields fall)." />
              <Stat label="Accrued interest" tone="warn" icon={<Receipt className="size-4" />} value={a.accrued_interest} format={(n) => inr(n)}
                hint="per bond, inside the NSE price" help="Interest earned since the last coupon that the buyer pays the seller now and gets back on the next coupon date. NSE's price already includes it (dirty price)." />
            </>
          ) : null}
        </div>

        {a && d && b && (
          <>
            <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-5">
              <Card className="lg:col-span-3" title="Interest-rate sensitivity" icon={<Waves className="size-4" />}
                subtitle="Drag to see what the price would do if market yields moved"
                help="Bond prices move opposite to interest rates. The curve is the price at each yield; its bend (convexity) means prices rise a little more when yields fall than they drop when yields rise.">
                <div className="mb-3 rounded-lg bg-background-subtle/70 p-3 ring-1 ring-inset ring-border/60">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <span className="text-muted">Yield change</span>
                    <span className={cx("num font-semibold", shiftBp > 0 ? "text-loss" : shiftBp < 0 ? "text-gain" : "")}>
                      {shiftBp > 0 ? "+" : ""}{shiftBp} bp <span className="text-xs font-normal text-muted">({pctOf(a.ytm)} → {pctOf(a.ytm + shiftBp / 10000)})</span>
                    </span>
                  </div>
                  <input type="range" min={-300} max={300} step={10} value={shiftBp} onChange={(e) => setShiftBp(Number(e.target.value))}
                    aria-label="Yield change in basis points" className="mt-2 w-full accent-[var(--brand)]" />
                  <div className="mt-1 flex justify-between text-[10px] text-muted"><span>−3%</span><span>0</span><span>+3%</span></div>
                  {shifted && (
                    <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
                      <Metric label="New price" value={inr(shifted.dirty)} />
                      <Metric label="Price change" value={signedPct(shifted.dirty / a.dirty_price - 1)} tone={toneOf(shifted.dirty - a.dirty_price)}
                        sub={`${shifted.dirty >= a.dirty_price ? "+" : "−"}${inr(Math.abs(shifted.dirty - a.dirty_price))} per bond`} />
                      <Metric label="Duration estimate" value={signedPct(-a.modified_duration * shiftBp / 10000)}
                        help="The quick rule: −modified duration × yield change. The gap to the exact change is convexity." />
                    </div>
                  )}
                </div>
                <PriceYieldChart curve={a.curve} ytm={a.ytm} price={a.dirty_price} shifted={shifted} />
                <p className="mt-1 text-center text-[11px] text-muted">Yield (x) against price including accrued interest (y). 1 bp = 0.01%.</p>
              </Card>

              <Card className="lg:col-span-2" title="Compared with a fixed deposit" icon={<Landmark className="size-4" />}
                help="Both are taxed at your slab. An FD has deposit insurance up to ₹5 lakh per bank and no price risk; a bond can be sold before maturity but its price moves with rates and the issuer's credit.">
                <div className="space-y-3">
                  {[
                    { label: "This bond, after tax", v: a.after_tax_ytm, tone: "bg-gradient-to-r from-brand to-accent" },
                    { label: `FD at ${pctOf(fd)}, after tax`, v: fdAfterTax ?? 0, tone: "bg-muted" },
                    { label: "This bond, before tax", v: a.ytm, tone: "bg-brand/40" },
                  ].map((row) => {
                    const max = Math.max(a.ytm, fd, 0.0001);
                    return (
                      <div key={row.label}>
                        <div className="mb-1 flex justify-between text-xs"><span className="text-muted">{row.label}</span><span className="num font-medium">{pctOf(row.v)}</span></div>
                        <div className="h-2.5 overflow-hidden rounded-full bg-background-subtle">
                          <div className={cx("h-full rounded-full transition-[width] duration-700 ease-out", row.tone)} style={{ width: `${Math.max(0, row.v / max) * 100}%` }} />
                        </div>
                      </div>
                    );
                  })}
                  <div className="grid [&>*]:min-w-0 grid-cols-2 gap-2">
                    <Metric label="FD rate that would match" value={pctOf(a.after_tax_ytm / Math.max(0.0001, 1 - d.tax_rate))}
                      help="The pre-tax FD rate that leaves you the same after tax as this bond's after-tax yield." />
                    <Metric label="Tax you'd pay" value={pctOf(d.tax_rate, 1)} sub="slab + 4% cess on interest" />
                  </div>
                  <p className="rounded-lg bg-background-subtle/70 p-3 text-xs ring-1 ring-inset ring-border/60">
                    {fdAfterTax != null && (a.after_tax_ytm > fdAfterTax ? (
                      <>The bond keeps <span className="num font-semibold text-gain">{((a.after_tax_ytm - fdAfterTax) * 100).toFixed(2)}%</span> a year more after tax, in return for credit risk{b.rating ? ` (rated ${ratingLabel(b.rating)})` : " (no rating in NSE's list)"} and price risk if sold early.</>
                    ) : (
                      <>The FD keeps <span className="num font-semibold text-loss">{((fdAfterTax - a.after_tax_ytm) * 100).toFixed(2)}%</span> a year more after tax, with less risk. The bond&apos;s premium over face value is a loss at maturity that eats into its yield.</>
                    ))}
                  </p>
                </div>
              </Card>
            </div>

            <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-5">
              <Card className="lg:col-span-3" title="Cash-flow schedule" icon={<CalendarRange className="size-4" />}
                subtitle={`${a.cash_flows.length} payments from ${shortDate(a.cash_flows[0].date)} to ${shortDate(a.cash_flows[a.cash_flows.length - 1].date)}, per bond of face ${inr(b.face_value, 0)}`}
                help="Every coupon still to come and the face value repaid at maturity, on the assumed coupon frequency. Dates step back from the maturity date.">
                <CashFlowChart flows={a.cash_flows} />
                <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric label="You pay today" value={inr(a.totals.paid_today)} sub="dirty price" />
                  <Metric label="Coupons to come" value={inr(a.totals.coupons)} sub={`${inr(a.coupon_per_payment)} each`} />
                  <Metric label="Principal back" value={inr(a.totals.principal)} sub={day(b.maturity)} />
                  <Metric label="Total received" value={inr(a.totals.received)} tone={toneOf(a.totals.received - a.totals.paid_today)}
                    sub={`${a.totals.received >= a.totals.paid_today ? "+" : ""}${inr(a.totals.received - a.totals.paid_today)} before tax`} />
                </div>
              </Card>

              <Card className="lg:col-span-2" title="Terms and risk measures" icon={<GaugeIcon className="size-4" />}>
                <Facts rows={[
                  { label: "Years to maturity", value: a.years_to_maturity.toFixed(2) },
                  { label: "Clean price", value: inr(a.clean_price), help: "The price without accrued interest: the figure yields are computed from." },
                  { label: "Dirty price", value: inr(a.dirty_price), help: "What you actually pay: clean price + accrued interest. NSE's traded price." },
                  { label: "Premium over face", value: <span className={toneOf(-a.premium_pct)}>{a.premium_pct >= 0 ? "+" : ""}{a.premium_pct.toFixed(2)}%</span>, help: "How far the clean price is above (premium) or below (discount) the face value you get back." },
                  { label: "Macaulay duration", value: `${a.macaulay_duration.toFixed(2)} yrs`, help: "The weighted-average time until you get your money back, weighting each payment by its present value." },
                  { label: "Convexity", value: a.convexity.toFixed(2), help: "How much the price–yield line curves. Higher convexity cushions losses when rates rise and adds to gains when they fall." },
                  { label: "Settlement date", value: day(a.settlement) },
                ]} />
              </Card>
            </div>

            <Card title="Price change for common rate moves" icon={<Waves className="size-4" />} subtitle="Exact repricing vs the duration-and-convexity estimate">
              <Table label="Price change for common rate moves">
                <thead>
                  <tr>
                    <th>Yield move</th>
                    <th className="text-right!">New price</th>
                    <th className="text-right!">Change</th>
                    <th className="text-right!">Estimate</th>
                  </tr>
                </thead>
                <tbody>
                  {a.sensitivity.map((row) => (
                    <tr key={row.bp}>
                      <td className="num">{row.bp > 0 ? "+" : ""}{row.bp} bp</td>
                      <td className="num text-right">{inr(row.price)}</td>
                      <td className={cx("num text-right font-medium", toneOf(row.change_pct))}>{row.change_pct > 0 ? "+" : ""}{row.change_pct.toFixed(2)}%</td>
                      <td className="num text-right text-muted">{row.duration_estimate_pct > 0 ? "+" : ""}{row.duration_estimate_pct.toFixed(2)}%</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>

            <p className="text-[11px] text-muted">
              Source: <a href={d.source} target="_blank" rel="noreferrer" className="underline underline-offset-2">NSE bonds traded in capital market</a>. Conventions: {a.conventions}. Not investment advice.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
