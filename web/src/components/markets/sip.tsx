"use client";

import { Calculator, Coins } from "lucide-react";
import { useEffect, useState } from "react";

import { SipChart } from "@/components/markets/charts";
import { Metric, inr, pctOf, signedPct, toneOf } from "@/components/markets/common";
import type { SipResult } from "@/components/markets/types";
import { Card, ErrorNote, Field, Segmented, Skeleton, cx, inputClass } from "@/components/ui";
import { day, useApi } from "@/lib/api";

const AMOUNTS = [1000, 5000, 10000, 25000, 50000];

/** SIP calculator on the fund's real NAVs: what `amount` a month over the last N years would be worth today,
 *  against the same money invested at once. */
export function SipCalculator({ code, maxYears }: { code: string; maxYears: number }) {
  const [amount, setAmount] = useState(10000);
  const [draft, setDraft] = useState("10000");
  const yearOptions = [1, 3, 5, 10].filter((y) => y <= Math.max(1, maxYears));
  const [years, setYears] = useState(String(Math.min(5, yearOptions[yearOptions.length - 1] ?? 1)));
  const [dom, setDom] = useState(5);

  // debounce typing so each keystroke doesn't recompute on the server
  useEffect(() => {
    const t = setTimeout(() => {
      const n = Math.round(Number(draft));
      if (Number.isFinite(n) && n >= 100 && n <= 10_000_000) setAmount(n);
    }, 350);
    return () => clearTimeout(t);
  }, [draft]);

  const r = useApi<SipResult>(`/api/funds/${code}/sip?amount=${amount}&years=${years}&day=${dom}`);
  const s = r.data;
  const sipBetter = s ? s.sip.value >= s.lump_sum.value : null;

  return (
    <Card title="SIP calculator" icon={<Calculator className="size-4" />}
      subtitle="What a monthly SIP in this fund would be worth today, from its actual NAVs"
      help="A SIP (systematic investment plan) buys units every month for a fixed amount. This replays it on the fund's real past NAVs; XIRR is the yearly return that accounts for each instalment's timing. Past returns don't predict future ones.">
      <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-[minmax(0,280px)_1fr]">
        <div className="space-y-4">
          <Field label="Monthly amount (₹)">
            <input className={cx(inputClass, "num w-full")} inputMode="numeric" value={draft} onChange={(e) => setDraft(e.target.value.replace(/[^0-9]/g, ""))} />
          </Field>
          <input type="range" min={500} max={100000} step={500} value={Math.min(amount, 100000)} aria-label="Monthly amount"
            onChange={(e) => { setDraft(e.target.value); setAmount(Number(e.target.value)); }}
            className="w-full accent-[var(--brand)]" />
          <div className="flex flex-wrap gap-1.5">
            {AMOUNTS.map((a) => (
              <button key={a} type="button" onClick={() => { setDraft(String(a)); setAmount(a); }}
                className={cx("num rounded-full px-2.5 py-0.5 text-xs ring-1 ring-inset transition",
                  a === amount ? "bg-brand-soft text-brand-strong ring-brand/30" : "text-muted ring-border hover:text-foreground")}>
                ₹{a.toLocaleString("en-IN")}
              </button>
            ))}
          </div>
          <Field label="For the last">
            <Segmented value={years} onChange={setYears} options={yearOptions.map((y) => ({ value: String(y), label: `${y}Y` }))} />
          </Field>
          <Field label="Instalment day" hint="If that day has no NAV, the next NAV day is used.">
            <Segmented value={String(dom)} onChange={(v) => setDom(Number(v))} options={[1, 5, 10, 15, 25].map((d) => ({ value: String(d), label: String(d) }))} />
          </Field>
        </div>

        <div className="min-w-0">
          {r.error ? (
            <ErrorNote error={r.error} onRetry={r.reload} />
          ) : !s ? (
            <div className="space-y-3">
              <div className="grid [&>*]:min-w-0 grid-cols-2 gap-2 sm:grid-cols-4">
                {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-16 rounded-lg" />)}
              </div>
              <Skeleton className="h-[260px] w-full rounded-lg" />
            </div>
          ) : (
            <div className="space-y-4 animate-fade-in">
              <div className="grid [&>*]:min-w-0 grid-cols-2 gap-2 sm:grid-cols-4">
                <Metric label="You'd have put in" value={inr(s.sip.invested, 0)} sub={`${s.sip.instalments} instalments`} />
                <Metric label="Worth today" value={inr(s.sip.value, 0)} tone={toneOf(s.sip.gain)} sub={`${s.sip.gain >= 0 ? "+" : ""}${inr(s.sip.gain, 0)}`} />
                <Metric label="XIRR" value={pctOf(s.sip.xirr)} tone={toneOf(s.sip.xirr)}
                  help="Extended internal rate of return: the steady yearly return that turns each dated instalment into today's value." />
                <Metric label="Lump sum instead" value={inr(s.lump_sum.value, 0)} tone={toneOf(s.lump_sum.gain)}
                  sub={s.lump_sum.cagr != null ? `${signedPct(s.lump_sum.cagr)} a year` : undefined}
                  help="The same total invested in one go on the SIP's first day, held to today." />
              </div>
              <SipChart path={s.path} />
              <p className="flex items-start gap-2 text-xs text-muted">
                <Coins className="mt-0.5 size-3.5 shrink-0 text-brand" />
                <span>
                  From {day(s.start)} to {day(s.end)}, {sipBetter ? "the SIP ended ahead of" : "a lump sum on day one beat"} {sipBetter ? "a lump sum on day one" : "the SIP"} by{" "}
                  <span className="num font-medium text-foreground">{inr(Math.abs(s.sip.value - s.lump_sum.value), 0)}</span>.{" "}
                  {sipBetter ? "Buying through dips lowered the average cost." : "Money invested earlier had longer to grow in a rising market."} A SIP also spreads the risk of investing just before a fall.
                </span>
              </p>
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
