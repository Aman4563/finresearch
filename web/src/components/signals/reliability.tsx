"use client";

// Reliability diagram: for each bin of forecasts, the mean forecast probability (x) against how often the event
// actually happened (y), with the 95 % Wilson interval as a whisker. Points on the diagonal are well calibrated;
// below it the forecasts were too confident in the event, above it not confident enough.

import { useState } from "react";

import type { ReliabilityBin } from "@/lib/api";

const W = 320, H = 320, M = { l: 44, r: 14, t: 14, b: 40 };
const PW = W - M.l - M.r, PH = H - M.t - M.b;
const x = (p: number) => M.l + p * PW;
const y = (p: number) => M.t + (1 - p) * PH;
const pc = (p: number) => `${Math.round(p * 100)}%`;
const TICKS = [0, 0.25, 0.5, 0.75, 1];

export function ReliabilityDiagram({ bins, baseRate }: { bins: ReliabilityBin[]; baseRate: number | null }) {
  const [hover, setHover] = useState<number | null>(null);
  const maxN = Math.max(1, ...bins.map((b) => b.n));
  const h = hover != null ? bins[hover] : null;
  return (
    <figure className="relative mx-auto w-full max-w-[380px]">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img"
        aria-label={bins.length ? `Reliability diagram with ${bins.length} bins` : "Reliability diagram, no resolved forecasts yet"}>
        {TICKS.map((t) => (
          <g key={t}>
            <line x1={x(0)} x2={x(1)} y1={y(t)} y2={y(t)} stroke="var(--chart-grid)" strokeWidth={1} />
            <line y1={y(0)} y2={y(1)} x1={x(t)} x2={x(t)} stroke="var(--chart-grid)" strokeWidth={1} />
            <text x={M.l - 6} y={y(t) + 3} textAnchor="end" fontSize={10} fill="var(--muted)" className="num">{pc(t)}</text>
            <text x={x(t)} y={H - M.b + 14} textAnchor="middle" fontSize={10} fill="var(--muted)" className="num">{pc(t)}</text>
          </g>
        ))}
        <text x={M.l + PW / 2} y={H - 6} textAnchor="middle" fontSize={10.5} fill="var(--muted)">Forecast probability</text>
        <text transform={`translate(11 ${M.t + PH / 2}) rotate(-90)`} textAnchor="middle" fontSize={10.5} fill="var(--muted)">Observed frequency</text>
        {/* perfect calibration */}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} stroke="var(--border-strong)" strokeWidth={1.5} strokeDasharray="4 4" />
        {baseRate != null && bins.length > 0 && (
          <>
            <line x1={x(0)} x2={x(1)} y1={y(baseRate)} y2={y(baseRate)} stroke="var(--muted)" strokeWidth={1} strokeDasharray="1 3" />
          </>
        )}
        {bins.map((b, i) => {
          const r = 4 + 5 * Math.sqrt(b.n / maxN);
          const active = hover === i;
          return (
            <g key={i} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(i)}
              onBlur={() => setHover(null)} tabIndex={0} aria-label={`forecast ${pc(b.mean_p)}, observed ${pc(b.observed)} of ${b.n}`}>
              {b.ci && (
                <>
                  <line x1={x(b.mean_p)} x2={x(b.mean_p)} y1={y(b.ci[0])} y2={y(b.ci[1])} stroke="var(--chart-1)" strokeWidth={2} strokeLinecap="round" />
                  <line x1={x(b.mean_p) - 5} x2={x(b.mean_p) + 5} y1={y(b.ci[0])} y2={y(b.ci[0])} stroke="var(--chart-1)" strokeWidth={2} strokeLinecap="round" />
                  <line x1={x(b.mean_p) - 5} x2={x(b.mean_p) + 5} y1={y(b.ci[1])} y2={y(b.ci[1])} stroke="var(--chart-1)" strokeWidth={2} strokeLinecap="round" />
                </>
              )}
              <circle cx={x(b.mean_p)} cy={y(b.observed)} r={r} fill="var(--chart-1)" stroke="var(--card)" strokeWidth={2}
                opacity={hover == null || active ? 1 : 0.45} />
              <circle cx={x(b.mean_p)} cy={y(b.observed)} r={Math.max(14, r + 6)} fill="transparent" />
            </g>
          );
        })}
      </svg>
      <div className="mt-1 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[11px] text-muted">
        <span className="inline-flex items-center gap-1.5"><svg width="18" height="6" aria-hidden><line x1="0" y1="3" x2="18" y2="3" stroke="var(--border-strong)" strokeWidth="1.5" strokeDasharray="4 4" /></svg>perfectly calibrated</span>
        {baseRate != null && bins.length > 0 && (
          <span className="inline-flex items-center gap-1.5"><svg width="18" height="6" aria-hidden><line x1="0" y1="3" x2="18" y2="3" stroke="var(--muted)" strokeWidth="1" strokeDasharray="1 3" /></svg>base rate {pc(baseRate)}</span>
        )}
        <span className="inline-flex items-center gap-1.5"><svg width="10" height="14" aria-hidden><line x1="5" y1="1" x2="5" y2="13" stroke="var(--chart-1)" strokeWidth="2" /><circle cx="5" cy="7" r="3.5" fill="var(--chart-1)" /></svg>group of forecasts (size = count), 95% CI</span>
      </div>
      {h && (
        <figcaption className="pointer-events-none absolute top-2 right-2 rounded-lg border border-border bg-card px-3 py-2 text-xs shadow-pop animate-scale-in">
          <p className="font-medium">{h.p_low === h.p_high ? `Forecast ${pc(h.p_low)}` : `Forecasts ${pc(h.p_low)}–${pc(h.p_high)}`}</p>
          <p className="num text-muted">happened {Math.round(h.observed * h.n)} of {h.n} ({pc(h.observed)})</p>
          {h.ci && <p className="num text-muted">95% CI {pc(h.ci[0])}–{pc(h.ci[1])}</p>}
        </figcaption>
      )}
    </figure>
  );
}
