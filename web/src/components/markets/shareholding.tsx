"use client";

// Shareholding split from each quarter's filed shareholding-pattern XBRL: a donut of the latest quarter (every
// category, shaded within its group) and a 100% stacked bar per quarter for the five groups.

import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { StockShareholding } from "@/components/markets/types";
import { InfoTip, cx } from "@/components/ui";
import { day, when } from "@/lib/api";

// four validated hues in a fixed order plus neutral grey for the catch-all group (see web/DESIGN.md chart tokens)
const GROUP_COLOR: Record<string, string> = {
  promoter: "var(--chart-1)",
  fii: "var(--chart-2)",
  dii: "var(--chart-3)",
  retail: "var(--chart-6)",
  other: "var(--muted)",
};
const SHADES = [100, 72, 52, 36, 24];

const GROUP_HELP: Record<string, string> = {
  promoter: "The founders / controlling group and their relatives and companies. Zero for professionally managed companies with no promoter (e.g. HDFC Bank, ITC).",
  fii: "Foreign institutional investors: registered foreign portfolio investors (FPI category I and II) plus other foreign institutions. A strategic foreign parent's stake is filed as FDI and counted under Others.",
  dii: "Domestic institutional investors: mutual funds, insurance companies, banks and other financial institutions, pension / provident funds, AIFs and other Indian institutions.",
  retail: "Individuals: resident Indians holding up to ₹2 lakh of nominal share capital (small retail), those above ₹2 lakh (HNIs), and non-resident Indians.",
  other: "Bodies corporate, strategic foreign (FDI) holders, government, IEPF, directors and KMP, trusts, clearing members and employee benefit trusts.",
};

const pct = (v: number | null | undefined, d = 2) => (v == null ? "—" : `${v.toFixed(d)}%`);
const quarterLabel = (iso: string) => {
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  return `${d.toLocaleDateString("en-IN", { month: "short", timeZone: "UTC" })} '${String(d.getUTCFullYear()).slice(2)}`;
};

export function ShareholdingSplit({ data }: { data: StockShareholding }) {
  const [active, setActive] = useState<string | null>(null);
  const quarters = data.quarters;
  const latest = quarters[quarters.length - 1];
  const prev = quarters.length > 1 ? quarters[quarters.length - 2] : null;

  // donut slices: every non-zero category, coloured by its group, lighter shades for the later members of a group
  const shadeIdx: Record<string, number> = {};
  const slices = data.category_labels
    .map((c) => ({ ...c, value: latest.categories[c.key] ?? 0 }))
    .filter((c) => c.value >= 0.005)
    .map((c) => {
      const i = (shadeIdx[c.group] = (shadeIdx[c.group] ?? -1) + 1);
      const s = SHADES[Math.min(i, SHADES.length - 1)];
      return { ...c, color: s === 100 ? GROUP_COLOR[c.group] : `color-mix(in oklab, ${GROUP_COLOR[c.group]} ${s}%, var(--card))` };
    });
  const activeSlice = slices.find((s) => s.key === active);
  const promoter = latest.groups.promoter;

  const trend = quarters.map((q) => ({ label: quarterLabel(q.as_of), as_of: q.as_of, ...q.groups }));
  const basisBreak = quarters.some((q) => q.taxonomy?.startsWith("2020")) && quarters.some((q) => !q.taxonomy?.startsWith("2020"));

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
        <div className="relative mx-auto size-[170px] shrink-0">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie data={slices} dataKey="value" nameKey="label" innerRadius="68%" outerRadius="92%" paddingAngle={1.5}
                stroke="var(--card)" strokeWidth={1} onMouseEnter={(_, i) => setActive(slices[i]?.key ?? null)}
                onMouseLeave={() => setActive(null)} animationDuration={700}>
                {slices.map((s) => (
                  <Cell key={s.key} fill={s.color} opacity={active == null || active === s.key ? 1 : 0.35} />
                ))}
              </Pie>
            </PieChart>
          </ResponsiveContainer>
          <div className="pointer-events-none absolute inset-0 grid place-items-center px-6 text-center">
            {activeSlice ? (
              <div>
                <p className="num text-lg font-semibold">{pct(activeSlice.value)}</p>
                <p className="text-[10px] leading-tight text-muted">{activeSlice.label}</p>
              </div>
            ) : (
              <div>
                <p className="num text-lg font-semibold">{pct(latest.groups.fii != null && latest.groups.dii != null ? latest.groups.fii + latest.groups.dii : null, 1)}</p>
                <p className="text-[11px] text-muted">FII + DII</p>
              </div>
            )}
          </div>
        </div>

        <ul className="min-w-0 flex-1 space-y-2 text-xs">
          {data.group_labels.map((g) => {
            const total = latest.groups[g.key];
            if (total == null || (total === 0 && g.key === "promoter")) return null; // no promoter group: said below
            const members = slices.filter((s) => s.group === g.key);
            const change = prev?.groups[g.key] != null ? total - (prev.groups[g.key] as number) : null;
            return (
              <li key={g.key}>
                <div className="flex items-center gap-2">
                  <span className="size-2.5 shrink-0 rounded-sm" style={{ background: GROUP_COLOR[g.key] }} />
                  <span className="font-medium">{g.label}</span>
                  <InfoTip label={`What is ${g.label}?`}>{GROUP_HELP[g.key]}</InfoTip>
                  <span className="num ml-auto font-semibold">{pct(total)}</span>
                  <span className={"num w-14 text-right text-[10px] text-muted"}
                    title={prev ? `Change since the quarter ended ${day(prev.as_of)}, percentage points` : undefined}>
                    {change == null ? "" : `${change > 0 ? "+" : change < 0 ? "−" : "±"}${Math.abs(change).toFixed(2)} pp`}
                  </span>
                </div>
                {(members.length > 1 || (members.length === 1 && members[0].label !== g.label && g.key !== "promoter")) && (
                  <ul className="mt-1 space-y-0.5 pl-4.5">
                    {members.map((m) => (
                      <li key={m.key} className={cx("flex items-center gap-2 transition-opacity", active != null && active !== m.key && "opacity-40")}
                        onMouseEnter={() => setActive(m.key)} onMouseLeave={() => setActive(null)}>
                        <span className="size-2 shrink-0 rounded-sm" style={{ background: m.color }} />
                        <span className="truncate text-muted">{m.label}</span>
                        <span className="num ml-auto">{pct(m.value)}</span>
                        <span className="w-14" />
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      {quarters.length > 1 && (
        <div>
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
            <span className="font-medium">Ownership by quarter</span>
            {data.group_labels.map((g) => (
              <span key={g.key} className="inline-flex items-center gap-1">
                <span className="size-2 rounded-sm" style={{ background: GROUP_COLOR[g.key] }} />
                {g.label}
              </span>
            ))}
          </div>
          <div style={{ height: 200 }} className="animate-fade-in">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={trend} margin={{ top: 4, right: 4, bottom: 0, left: 0 }} barCategoryGap="22%">
                <CartesianGrid vertical={false} strokeDasharray="3 3" />
                <XAxis dataKey="label" tickLine={false} axisLine={false} fontSize={11} interval="preserveStartEnd" />
                <YAxis tickLine={false} axisLine={false} fontSize={11} width={36} domain={[0, 100]} ticks={[0, 25, 50, 75, 100]}
                  tickFormatter={(v) => `${v}%`} />
                <Tooltip cursor={{ fill: "var(--background-subtle)" }} content={<TrendTip labels={data.group_labels} />} />
                {data.group_labels.map((g, i) => (
                  <Bar key={g.key} dataKey={g.key} name={g.label} stackId="own" fill={GROUP_COLOR[g.key]} stroke="var(--card)" strokeWidth={1}
                    radius={i === data.group_labels.length - 1 ? [4, 4, 0, 0] : 0} animationDuration={700} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      <div className="space-y-1 text-[11px] text-muted">
        <p>
          % of shares as filed under SEBI LODR Reg 31 (SCRR basis
          <InfoTip label="What is the SCRR basis?">
            Percentages are of total shares excluding shares that sit behind depository receipts (ADRs / GDRs), exactly as the company files them. That is why they match the exchange&apos;s promoter / public figures.
          </InfoTip>
          ).
          {latest.dr_pct_of_total_shares != null && latest.dr_pct_of_total_shares >= 0.01 && (
            <> Another {latest.dr_pct_of_total_shares.toFixed(2)}% of all shares underlie depository receipts (ADR / GDR) and sit outside this basis.</>
          )}
          {promoter === 0 && <> The company has no promoter group.</>}
          {basisBreak && <> Filings before 2022 used the older format: FPIs were not split by category and ADR shares counted as public, so the trend has a step there.</>}
        </p>
        <p>
          Source: quarter ended {day(latest.as_of)} shareholding pattern{" "}
          <a href={latest.xbrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-brand hover:underline">
            XBRL <ExternalLink className="size-3" />
          </a>
          {latest.submitted && <>, filed {day(latest.submitted)}</>} on {data.exchange ?? "NSE"}; read {when(data.fetched_at)}.
          {data.errors.length > 0 && <> {data.errors.length} older quarter{data.errors.length > 1 ? "s" : ""} could not be read.</>}
        </p>
      </div>
    </div>
  );
}

type TipRow = { name?: string; value?: number; color?: string; dataKey?: string; payload?: Record<string, unknown> };

function TrendTip({ active, payload, labels }: { active?: boolean; payload?: TipRow[]; labels: { key: string; label: string }[] }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload ?? {};
  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-xs shadow-pop backdrop-blur animate-scale-in">
      <p className="mb-1 font-medium text-muted">Quarter ended {day(String(row.as_of))}</p>
      {[...labels].reverse().map((g) => (
        <p key={g.key} className="flex items-center gap-2">
          <span className="size-2 rounded-full" style={{ background: GROUP_COLOR[g.key] }} />
          <span className="text-muted">{g.label}</span>
          <span className="num ml-auto pl-3 font-medium">{pct(row[g.key] as number | null)}</span>
        </p>
      ))}
    </div>
  );
}
