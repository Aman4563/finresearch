"use client";

// /wealth: household finances. Net worth (latest portfolio snapshot + manual assets − loans) over time, allocation
// over the whole balance sheet vs a labelled glide-path rule of thumb, goals (seeded Monte Carlo), emergency fund
// with the DICGC per-bank flag, needs-based term cover, EMIs vs income and prepay-or-invest. All arithmetic is done
// by the local API in Python; nothing here is sent to an LLM.

import { Info, Landmark, LifeBuoy, PiggyBank, PieChart, Scale, ShieldCheck, Wallet } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { DonutChart, TimeSeriesChart, fmtCompactINR } from "@/components/charts";
import { Badge, Callout, Card, EmptyState, ErrorNote, InfoTip, PageHeader, Progress, Segmented, SkeletonRows, Stat, Table, cx } from "@/components/ui";
import { day, useApi } from "@/lib/api";

import { EntriesPanel } from "./entries-panel";
import { GoalsPanel } from "./goals-panel";
import { HouseholdPanel } from "./household-panel";
import { type Wealth, inr, pc } from "./types";

type Tab = "overview" | "goals" | "entries" | "household";
const TABS: { value: Tab; label: string }[] = [
  { value: "overview", label: "Overview" }, { value: "goals", label: "Goals" }, { value: "entries", label: "Assets & loans" }, { value: "household", label: "Household" },
];
const CLASS_COLOR: Record<string, string> = {
  Equity: "var(--chart-1)", Debt: "var(--chart-2)", Gold: "var(--chart-3)", Cash: "var(--chart-4)", "Real estate": "var(--chart-5)", Other: "var(--chart-6)",
};

function NetWorth({ w }: { w: Wealth }) {
  const h = w.history;
  return (
    <Card title="Net worth over time" icon={<Wallet className="size-4" />}
      help="Month ends, using only what was known on each day: the portfolio snapshot on or before it (saved when /portfolio is valued), your dated values, FD/RD growth and each loan's amortisation schedule. Real estate is your own estimate.">
      {h.length < 2 ? (
        <EmptyState title="Not enough history yet">The line starts from the first dated entry (a value, an FD, a loan or a portfolio snapshot).</EmptyState>
      ) : (
        <TimeSeriesChart data={h} area={false} showChange={false} format={fmtCompactINR} ranges={["1Y", "3Y", "5Y", "ALL"]} defaultRange="ALL"
          series={[{ key: "net_worth", label: "Net worth", color: "var(--chart-1)" }, { key: "liquid_net_worth", label: "Liquid net worth", color: "var(--chart-2)", dashed: true },
            { key: "liabilities", label: "Loans", color: "var(--loss)" }]} />
      )}
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <span className="text-muted">Portfolio<span className="num block text-sm font-medium text-foreground">{inr(w.net_worth.portfolio)}</span>
          {w.portfolio.day ? `snapshot ${day(w.portfolio.day)}` : <Link href="/portfolio" className="text-brand">no snapshot yet</Link>}</span>
        <span className="text-muted">Other assets<span className="num block text-sm font-medium text-foreground">{inr(w.net_worth.manual)}</span></span>
        <span className="text-muted">Loans<span className="num block text-sm font-medium text-loss">{inr(w.net_worth.liabilities)}</span></span>
        <span className="text-muted">Debt / assets<span className="num block text-sm font-medium text-foreground">{pc(w.debt.debt_to_assets_pct)}</span></span>
      </div>
    </Card>
  );
}

function Allocation({ w }: { w: Wealth }) {
  const a = w.allocation;
  const data = Object.entries(a.by_class).filter(([, v]) => v > 0).sort((x, y) => y[1] - x[1]).map(([k, v]) => ({ name: k, value: v, color: CLASS_COLOR[k] }));
  const total = data.reduce((s, d) => s + d.value, 0);
  return (
    <Card title="Allocation across everything" icon={<PieChart className="size-4" />}
      help="Portfolio classes from its latest snapshot (gold & international funds counted as gold, an approximation) plus your other assets: FD/RD/EPF/PPF as debt, NPS split by its equity share, gold and SGBs as gold. The glide-path comparison is over financial assets (real estate left out).">
      {total <= 0 ? <EmptyState title="Nothing valued yet">Add assets or value the portfolio.</EmptyState> : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <DonutChart data={data} format={fmtCompactINR} height={130} center={<div><p className="num text-base font-semibold">{fmtCompactINR(total)}</p><p className="text-[11px] text-muted">assets</p></div>} />
          <div>
            {a.comparison.length > 0 && (
              <Table label="Allocation across everything">
                <thead><tr><th>Class</th><th className="text-right">Now</th><th className="text-right">{a.own_target ? "Your target" : "Rule gives"}</th><th className="text-right">Drift</th></tr></thead>
                <tbody>{a.comparison.map((r) => (
                  <tr key={r.label}><td>{r.label}</td><td className="num text-right">{r.weight_pct.toFixed(1)}%</td><td className="num text-right">{r.target_pct.toFixed(0)}%</td>
                    <td className={cx("num text-right", r.outside_band ? "font-medium text-warn" : "text-muted")} title={`band ±${r.band_pp.toFixed(1)} pp`}>{r.drift_pp > 0 ? "+" : ""}{r.drift_pp.toFixed(1)} pp</td></tr>
                ))}</tbody>
              </Table>
            )}
            <p className="mt-2 text-sm">{a.message}</p>
            <p className="mt-1 text-[11px] text-muted"><Badge tone="warn">rule of thumb</Badge> {a.rule} Risk appetite ({a.risk_appetite}) from your <Link href="/profile" className="text-brand">profile</Link>.</p>
          </div>
        </div>
      )}
    </Card>
  );
}

function Emergency({ w }: { w: Wealth }) {
  const e = w.emergency;
  const over = e.banks.filter((b) => b.over_limit);
  return (
    <Card title="Emergency fund" icon={<LifeBuoy className="size-4" />}
      help="Months covered = liquid money ÷ essential monthly expenses. Liquid = savings/cash + 90 % of FDs/RDs you marked breakable (a premature-withdrawal haircut, rule of thumb) + other assets marked liquid.">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
        <div><p className="text-xs text-muted">Months covered</p><p className="num text-3xl font-semibold">{e.months == null ? "—" : e.months.toFixed(1)}</p></div>
        <div className="text-xs text-muted">Liquid <span className="num block text-sm font-medium text-foreground">{inr(e.liquid)}</span></div>
        <div className="text-xs text-muted">Target <span className="num block text-sm font-medium text-foreground">{e.target_months} months</span></div>
      </div>
      {e.months != null && <Progress className="mt-3" value={Math.min(e.months / e.target_months, 1)} tone={e.months >= e.target_months ? "gain" : "warn"} />}
      <p className="mt-2 text-sm">{e.message}</p>
      <div className="mt-4">
        <p className="mb-1 flex items-center gap-1 text-xs font-medium">Deposit insurance by bank
          <InfoTip>DICGC insures up to ₹5,00,000 per depositor per bank, principal and interest together, for deposits held in the same right and capacity (dicgc.org.in, guide to deposit insurance, read 30-Sep-2026). Joint accounts in a different capacity are insured separately, so this can over-flag. Savings and FD/RD balances only.</InfoTip></p>
        {e.banks.length === 0 ? <p className="text-xs text-muted">No bank deposits entered.</p> : (
          <Table label="Deposit insurance by bank">
            <thead><tr><th>Bank</th><th className="text-right">Deposits</th><th className="text-right">Above ₹5 lakh</th></tr></thead>
            <tbody>{e.banks.map((b) => (
              <tr key={b.bank}><td>{b.bank}{b.over_limit && <> <Badge tone="warn">above cover</Badge></>}</td><td className="num text-right">{inr(b.total)}</td>
                <td className={cx("num text-right", b.over_limit ? "text-warn" : "text-muted")}>{b.over_limit ? inr(b.uninsured) : "—"}</td></tr>
            ))}</tbody>
          </Table>
        )}
        {over.length > 0 && <p className="mt-2 text-xs text-muted">Deposits above ₹5 lakh at one bank are not covered by DICGC. Spreading them across banks is one way people keep each within cover.</p>}
      </div>
    </Card>
  );
}

function Insurance({ w }: { w: Wealth }) {
  const i = w.insurance;
  const t = i.term;
  return (
    <Card title="Insurance cover" icon={<ShieldCheck className="size-4" />}
      help="Needs method: present value of household expenses over the support years (at the real rate from your debt-return and inflation assumptions, paid at the start of each year) + loans + gaps on goals you marked for cover − financial assets − existing term cover. Generic arithmetic, not a product recommendation.">
      {t ? (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          <dt className="text-muted">Expenses for {t.years} years (PV at {t.real_rate_pct.toFixed(2)} % real)</dt><dd className="num text-right">{inr(t.expenses_pv)}</dd>
          <dt className="text-muted">+ Loans outstanding</dt><dd className="num text-right">{inr(t.loans)}</dd>
          <dt className="text-muted">+ Gaps on goals marked for cover</dt><dd className="num text-right">{inr(t.goal_gaps)}</dd>
          <dt className="text-muted">− Financial assets</dt><dd className="num text-right">{inr(t.assets)}</dd>
          <dt className="text-muted">− Existing term cover</dt><dd className="num text-right">{inr(t.existing)}</dd>
          <dt className="font-medium">Gap</dt><dd className={cx("num text-right font-semibold", t.gap > 0 ? "text-warn" : "text-gain")}>{inr(t.gap)}</dd>
        </dl>
      ) : null}
      <p className="mt-2 text-sm">{i.term_message}</p>
      {i.income_multiple && <p className="mt-1 text-xs text-muted"><Badge tone="warn">rule of thumb</Badge> Income multiple 10–15× gives {fmtCompactINR(i.income_multiple[0])}–{fmtCompactINR(i.income_multiple[1])} (a cross-check only).</p>}
      <div className="mt-3 border-t border-border pt-3 text-xs">
        <p>Health cover <span className="num font-medium">{inr(i.health_total)}</span>{i.health_employer > 0 && <span className="text-muted"> ({inr(i.health_employer)} through an employer, which ends if you leave)</span>}</p>
        <p className="mt-1 text-muted"><Badge tone="warn">rule of thumb</Badge> Planners often cite at least {fmtCompactINR(i.health_rule_inr)} family cover in a metro.</p>
      </div>
    </Card>
  );
}

function Debt({ w }: { w: Wealth }) {
  const d = w.debt;
  return (
    <Card title="EMIs and income" icon={<Landmark className="size-4" />}
      help="EMI/income (FOIR) = total monthly EMIs ÷ monthly net income. Many lenders cap it around 40–50 % (rule of thumb). The prepay-or-invest arithmetic for each loan is under Assets & loans.">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
        <div><p className="text-xs text-muted">EMI / income</p><p className="num text-3xl font-semibold">{pc(d.foir_pct)}</p></div>
        <div className="text-xs text-muted">EMIs a month<span className="num block text-sm font-medium text-foreground">{inr(d.total_emi)}</span></div>
        <div className="text-xs text-muted">Weighted loan rate<span className="num block text-sm font-medium text-foreground">{pc(d.weighted_rate_pct, 2)}</span></div>
      </div>
      {d.foir_pct != null && <Progress className="mt-3" value={Math.min(d.foir_pct / 100, 1)} tone={d.foir_pct >= 40 ? "warn" : "gain"} />}
      <p className="mt-2 text-sm">{d.message}</p>
    </Card>
  );
}

export function WealthPage() {
  const [tab, setTab] = useState<Tab>("overview");
  const [refresh, setRefresh] = useState(0);
  const { data: w, error, reload } = useApi<Wealth>(`/api/wealth?r=${refresh}`);
  const changed = useCallback(() => setRefresh((r) => r + 1), []);
  useEffect(() => {
    const sync = () => {
      const h = window.location.hash.slice(1) as Tab;
      setTab(TABS.some((t) => t.value === h) ? h : "overview");
    };
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  const go = (t: Tab) => { setTab(t); history.replaceState(null, "", `#${t}`); };
  const nw = w?.net_worth;
  const empty = w && w.assets.length === 0 && w.loans.length === 0 && !w.portfolio.day && w.household.age == null;

  return (
    <div>
      <PageHeader icon={<PiggyBank className="size-5" />} title="Wealth" eyebrow="You"
        description="Your household balance sheet: net worth, allocation across everything, goals, emergency fund, insurance and loans. Arithmetic on your own entries and stated assumptions." />
      {error && <ErrorNote error={error} onRetry={reload} />}
      {!w && !error && <SkeletonRows rows={6} />}
      {w && nw && (
        <>
          <div className="stagger mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Net worth" value={nw.net_worth} format={(n) => inr(n)} icon={<Wallet className="size-4" />}
              hint={`assets ${fmtCompactINR(nw.assets)} − loans ${fmtCompactINR(nw.liabilities)}`} help="Portfolio (latest snapshot) + your other assets − outstanding loans." />
            <Stat label="Liquid net worth" value={nw.liquid_net_worth} format={(n) => inr(n)} icon={<Scale className="size-4" />} tone="accent"
              hint="without property, EPF/PPF/NPS, home loans" help="Net worth without real estate and retirement lock-ins (EPF, PPF, NPS), and without home loans (secured on the property left out); other loans still count. Closer to what you could use within weeks." />
            <Stat label="Emergency fund" display={<span className="num">{w.emergency.months == null ? "—" : `${w.emergency.months.toFixed(1)} mo`}</span>} icon={<LifeBuoy className="size-4" />}
              tone={w.emergency.months != null && w.emergency.months >= w.emergency.target_months ? "gain" : "warn"} hint={`target ${w.emergency.target_months} months`} />
            <Stat label="EMI / income" display={<span className="num">{pc(w.debt.foir_pct)}</span>} icon={<Landmark className="size-4" />}
              tone={w.debt.foir_pct != null && w.debt.foir_pct >= 40 ? "warn" : "brand"} hint={`EMIs ${inr(w.debt.total_emi)} a month`} />
          </div>
          {empty && tab === "overview" && (
            <div className="mb-4"><Callout tone="info" title="Start here">Fill in the Household tab (age, income, expenses), then add your savings, deposits, EPF/PPF and loans under Assets &amp; loans. The portfolio is included from its latest snapshot on /portfolio.</Callout></div>
          )}
          <div className="mb-4 overflow-x-auto"><Segmented value={tab} onChange={go} options={TABS} /></div>
          <div key={tab} className="animate-fade-up">
            {tab === "overview" && (
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <div className="lg:col-span-2"><NetWorth w={w} /></div>
                <div className="lg:col-span-2"><Allocation w={w} /></div>
                <Emergency w={w} />
                <div className="min-w-0 space-y-4"><Debt w={w} /><Insurance w={w} /></div>
              </div>
            )}
            {tab === "goals" && <GoalsPanel w={w} onChanged={changed} refresh={refresh} />}
            {tab === "entries" && <EntriesPanel w={w} onChanged={changed} />}
            {tab === "household" && <HouseholdPanel key={JSON.stringify(w.household)} h={w.household} onSaved={changed} />}
          </div>
          <p className="mt-6 flex items-start gap-1.5 text-[11px] text-muted"><Info className="mt-0.5 size-3 shrink-0" />{w.privacy} {w.disclaimer} Facts marked [unverified] could not be checked against an official source.</p>
        </>
      )}
    </div>
  );
}
