"use client";

// Summary tab: the verdict at a glance, a plain-English explanation, key numbers, pros and cons, the top risks,
// what to do next and how much of the evidence is verified. Every figure is a ledger claim (chips open the source).

import { AlertTriangle, CheckCircle2, ClipboardCheck, Compass, Lightbulb, ShieldCheck, ThumbsDown, ThumbsUp } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Badge, Card, InfoTip, cx } from "@/components/ui";
import { parseVerdict } from "@/components/workspace/report-parts";
import { AccuracyCard } from "@/components/workspace/report/accuracy";
import { KindIcon, kindMeta } from "@/components/workspace/run-meta";
import {
  type ClaimMap, CiteText, ConfidenceMeter, type OpenClaim, SourceStrip, StatusDot, STATUS_LABEL, tileHelp,
} from "@/components/workspace/report/shared";
import { SinceReport } from "@/components/markets/stock-signal";
import { listingLabel, type Report } from "@/lib/api";
import type { Insights, Tile } from "@/lib/insights";

const TONE_RING: Record<string, string> = {
  gain: "from-gain/15 ring-gain/30 text-gain",
  loss: "from-loss/15 ring-loss/30 text-loss",
  warn: "from-warn/15 ring-warn/30 text-warn",
  neutral: "from-brand/10 ring-border text-foreground",
};

const firstSentence = (s: string) => {
  const m = s.match(/^([\s\S]+?[.!?])(\s|$)/);
  return (m ? m[1] : s).trim();
};

function VerdictHero({ report, ins, claims, onOpen, title }: { report: Report; ins: Insights | null; claims: ClaimMap; onOpen: OpenClaim; title: string }) {
  const v = useMemo(() => parseVerdict(report.markdown), [report.markdown]);
  const word = ins?.verdict.word ?? v?.word ?? null;
  // a report's bottom line is a research view, never painted as a green "go" or a red "stop" (#241)
  const tone = "neutral";
  const stockView = report.kind === "stock_report" && (ins?.verdict.policy === "informational" || /^(Favourable|Mixed|Unfavourable)$/i.test(word ?? ""));
  const conf = ins?.verdict.confidence ?? v?.confidence ?? null;
  const words = report.markdown.split(/\s+/).length;
  const vd = ins?.verdict;
  // the headline zone: entry zone (stock), price/yield (bond), who it suits (fund), or the two IPO views
  const zone: { label: string; text: string }[] = [];
  if (vd?.listing) zone.push({ label: "Listing view", text: vd.listing });
  if (vd?.long_term) zone.push({ label: "Long-term view", text: vd.long_term });
  if (vd?.entry_zone) zone.push({ label: report.kind === "stock_report" ? "Price range discussed (context, not an instruction)" : "Entry zone", text: vd.entry_zone });
  if (vd?.price_context) zone.push({ label: "Price range discussed (context, not an instruction)", text: vd.price_context });
  if (vd?.price_or_yield) zone.push({ label: "Price / yield it applies at", text: vd.price_or_yield });
  if (vd?.suits) zone.push({ label: "Suits", text: vd.suits });
  const fair = v?.rows.find((r) => /fair.value/i.test(r.label));
  if (fair) zone.push({ label: "Fair-value range", text: fair.value });
  const rows = zone.length ? zone : (v?.rows ?? []).map((r) => ({ label: r.label, text: r.value }));
  return (
    <Card padded={false} className="relative overflow-hidden animate-fade-up">
      <div className={cx("pointer-events-none absolute inset-0 bg-gradient-to-br to-transparent to-60% opacity-80", TONE_RING[tone].split(" ")[0])} />
      <div className="relative grid gap-5 p-4 sm:p-6 lg:grid-cols-[minmax(0,17rem)_1fr]">
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-xs text-muted">
            <KindIcon kind={report.kind} className="size-8" />
            <span>
              {kindMeta(report.kind).label} report · run <span className="num">#{report.run_id}</span>
            </span>
          </div>
          <div>
            <p className="flex items-center gap-1 text-[11px] font-medium uppercase tracking-wider text-muted">
              {stockView ? "Research view" : "The report's view"}
              <InfoTip>
                {stockView
                  ? "Informational, no validated edge: FinResearch has not shown that its stock research beats the index, so a stock report gives a research view (favourable, mixed or unfavourable), not a buy or sell call."
                  : "The report's own researched or rule-based view, not advice. Read the reasoning and check the cited evidence before you act."}
              </InfoTip>
            </p>
            <p className={cx("mt-1 inline-flex rounded-xl px-3 py-1.5 text-2xl font-bold tracking-tight ring-1 ring-inset", TONE_RING[tone].split(" ").slice(1).join(" "))}>
              {word ?? "See report"}
            </p>
            {stockView && (
              <p className="mt-1 text-xs text-muted">
                Informational, no validated edge{ins?.verdict.original_word ? ` (the report's original label was ${ins.verdict.original_word})` : ""}.
              </p>
            )}
          </div>
          <ConfidenceMeter level={conf} />
          <div className="flex flex-wrap gap-1.5">
            {ins?.kind === "stock_report" && ins.subject.key && <Badge tone={ins.subject.key.startsWith("BSE:") ? "accent" : "neutral"}>{listingLabel(ins.subject.key)}</Badge>}
            {vd?.horizon && <Badge tone="info">Horizon: {vd.horizon}</Badge>}
            {report.published ? (
              <Badge tone="gain">
                <ShieldCheck className="size-3" /> gate passed
              </Badge>
            ) : (
              <Badge tone="loss">not published</Badge>
            )}
            <Badge tone="neutral">{Math.max(1, Math.round(words / 230))} min read</Badge>
          </div>
        </div>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold leading-snug tracking-tight sm:text-xl">{title}</h1>
          {rows.length > 0 ? (
            <dl className="mt-3 grid gap-2.5 sm:grid-cols-2">
              {rows.slice(0, 4).map((r) => (
                <div key={r.label} className="rounded-lg border border-border bg-card/70 p-3">
                  <dt className="text-[11px] font-medium uppercase tracking-wider text-muted">{r.label}</dt>
                  <dd className="mt-1 line-clamp-4 text-[13px] leading-relaxed">
                    <CiteText text={r.text} claims={claims} onOpen={onOpen} />
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="mt-2 text-sm text-muted">This report has no verdict box; read the full report for the reasoning.</p>
          )}
        </div>
      </div>
    </Card>
  );
}

/** Headline precision (roadmap §B0.5): drop decimals beyond three significant figures ("₹4,06,260.47 Cr" →
 * "₹4,06,260 Cr", "13.54x" → "13.5x", "31.71%" → "31.7%"). Whole-number digits are never changed; the exact
 * ledger value stays in the tooltip and the evidence panel. */
export function headline(display: string): string {
  return display.replace(/(\d[\d,]*)\.(\d+)/, (_, int: string, dec: string) => {
    const digits = int.replace(/,/g, "").replace(/^0+/, "").length;
    const keep = Math.max(0, 3 - digits);
    if (keep >= dec.length) return `${int}.${dec}`;
    const v = Number(`${int.replace(/,/g, "")}.${dec}`);
    const r = v.toFixed(keep);
    const [ri, rd] = r.split(".");
    // re-group the integer part the way the source did (Indian grouping when it had commas)
    const grouped = int.includes(",") || ri.length > int.length ? Number(ri).toLocaleString("en-IN") : ri;
    return rd ? `${grouped}.${rd}` : grouped;
  });
}

function KeyTile({ t, claims, onOpen }: { t: Tile; claims: ClaimMap; onOpen: OpenClaim }) {
  const help = tileHelp(t.label, t.term);
  return (
    <div className="group relative rounded-xl border border-border bg-card p-3.5 shadow-card transition hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow">
      <p className="flex items-center gap-1 text-xs font-medium text-muted">
        <span className="truncate">{t.label}</span>
        {help && <InfoTip>{help}</InfoTip>}
      </p>
      <button type="button" onClick={() => onOpen(t.claim_id)} className="mt-1.5 block text-left" title={`${t.display} (exact) · open the source of C${t.claim_id}`}>
        <span className="num text-lg font-semibold tracking-tight sm:text-xl">{headline(t.display)}</span>
        {t.unit.startsWith("₹/") && <span className="ml-1 text-xs text-muted">/{t.unit.slice(2)}</span>}
      </button>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted">
        {t.delta && (
          <span className={cx("num font-medium", t.delta.pct > 0 ? "text-gain" : t.delta.pct < 0 ? "text-loss" : "")} title={`fincalc: change vs ${t.delta.vs} from C${t.delta.claim_ids.join(", C")}`}>
            {t.delta.pct > 0 ? "▲" : t.delta.pct < 0 ? "▼" : ""} {Math.abs(t.delta.pct).toFixed(2)}% vs {t.delta.vs}
          </span>
        )}
        {t.hint && <span className="truncate">{t.hint}</span>}
      </div>
      <div className="mt-1.5 flex items-center gap-1.5 text-[10px] text-muted">
        <StatusDot status={t.status} />
        {t.status === "verified" ? "verified" : "UNVERIFIED (needs review)"}
        <SourceStrip ids={[t.claim_id]} claims={claims} onOpen={onOpen} label="" className="ml-auto" />
      </div>
    </div>
  );
}

const WEIGHT: Record<string, "loss" | "warn" | "neutral"> = { high: "loss", medium: "warn", low: "neutral" };
const SEV_TONE = (s: string | null): "loss" | "warn" | "neutral" => (!s ? "neutral" : /high/.test(s) ? "loss" : /medium/.test(s) ? "warn" : "neutral");

function CaseCard({ title, icon, tone, points, claims, onOpen, empty }: {
  title: string; icon: React.ReactNode; tone: "gain" | "loss"; points: Insights["pros"]; claims: ClaimMap; onOpen: OpenClaim; empty: string;
}) {
  return (
    <Card title={title} icon={icon} subtitle={`${points.length} point${points.length === 1 ? "" : "s"}, strongest first`}>
      {points.length === 0 ? (
        <p className="text-sm text-muted">{empty}</p>
      ) : (
        <ul className="space-y-3">
          {[...points].sort((a, b) => ["high", "medium", "low"].indexOf(a.weight ?? "low") - ["high", "medium", "low"].indexOf(b.weight ?? "low")).map((p, i) => (
            <li key={i} className={cx("border-l-2 pl-3 text-sm leading-relaxed", tone === "gain" ? "border-gain/50" : "border-loss/50")}>
              <CiteText text={p.text} claims={claims} onOpen={onOpen} />
              <div className="mt-1 flex flex-wrap items-center gap-2">
                {p.weight && <Badge tone={tone === "gain" ? (p.weight === "high" ? "gain" : "neutral") : WEIGHT[p.weight] ?? "neutral"}>{p.weight} weight</Badge>}
                <SourceStrip ids={p.claim_ids} claims={claims} onOpen={onOpen} label="evidence" />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Checklist({ runId, items, claims, onOpen }: { runId: number; items: Insights["checklist"]; claims: ClaimMap; onOpen: OpenClaim }) {
  const key = `finresearch:checklist:${runId}`;
  const [done, setDone] = useState<Set<number>>(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(key) ?? "[]") as number[]);
    } catch {
      return new Set();
    }
  });
  const flip = (i: number) =>
    setDone((d) => {
      const n = new Set(d);
      if (n.has(i)) n.delete(i);
      else n.add(i);
      try {
        localStorage.setItem(key, JSON.stringify([...n]));
      } catch {}
      return n;
    });
  return (
    <ol className="space-y-2">
      {items.map((it, i) => (
        <li key={i} className="flex gap-2.5 text-sm leading-relaxed">
          <input type="checkbox" checked={done.has(i)} onChange={() => flip(i)} aria-label={`mark step ${i + 1} done`}
            className="mt-1 size-4 shrink-0 accent-[var(--brand)]" />
          <span className={cx(done.has(i) && "text-muted line-through decoration-muted/60")}>
            <CiteText text={it.text} claims={claims} onOpen={onOpen} />
          </span>
        </li>
      ))}
    </ol>
  );
}

const STATUS_ORDER = ["verified", "needs_review", "unverified", "contradicted", "unsupported"] as const;
const STATUS_BAR: Record<string, string> = { verified: "bg-gain", needs_review: "bg-warn", unverified: "bg-warn/60", contradicted: "bg-loss", unsupported: "bg-loss/60" };

export function QualityBar({ counts, total, className }: { counts: Record<string, number>; total: number; className?: string }) {
  return (
    <div className={cx("flex h-2.5 overflow-hidden rounded-full bg-background-subtle", className)}>
      {STATUS_ORDER.map((s) => (counts[s] ? <span key={s} className={cx("h-full", STATUS_BAR[s])} style={{ width: `${(counts[s] / Math.max(1, total)) * 100}%` }} title={`${STATUS_LABEL[s]}: ${counts[s]}`} /> : null))}
    </div>
  );
}

function QualityCard({ q, onEvidence }: { q: Insights["quality"]; onEvidence: () => void }) {
  const pct = q.cited_verified_pct ?? 0;
  const tone = pct >= 90 ? "text-gain" : pct >= 70 ? "text-warn" : "text-loss";
  return (
    <Card title="Citations verified" icon={<CheckCircle2 className="size-4" />}
      help="Every number in the report is a claim that a separate verifier checked against its source. Verified = the quote was found at the cited page/lines; needs review = plausible but not fully confirmed (the report marks it UNVERIFIED); contradicted/unsupported claims are never used as fact. This measures whether the facts are quoted correctly, not whether the call will turn out right.">
      <div className="flex items-end gap-3">
        <p className={cx("num text-3xl font-semibold tracking-tight", tone)}>{q.cited_verified_pct == null ? "—" : `${q.cited_verified_pct}%`}</p>
        <p className="pb-1 text-xs text-muted">
          of the <span className="num">{q.cited}</span> claims the report cites are verified
        </p>
      </div>
      <div className="mt-3 space-y-1.5">
        <QualityBar counts={q.by_status} total={q.total} />
        <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
          {STATUS_ORDER.filter((s) => q.by_status[s]).map((s) => (
            <li key={s} className="flex items-center gap-1">
              <span className={cx("size-2 rounded-sm", STATUS_BAR[s])} />
              {STATUS_LABEL[s]} <span className="num text-foreground">{q.by_status[s]}</span>
            </li>
          ))}
        </ul>
        <p className="text-[11px] text-muted">
          All <span className="num">{q.total}</span> claims in the ledger, <span className="num">{q.verified_pct ?? 0}%</span> verified. Contradicted ones were caught and left out.
        </p>
        {q.cited_by_grade && (
          <p className="text-[11px] text-muted" title="A: quote found at the document lines. B: quote found on a stored copy of the web page. C: web source, quote not checked. D: re-computed by fincalc from cited inputs. U: unsupported.">
            Evidence of the cited claims:{" "}
            {(["A", "B", "D", "C", "U"] as const).filter((g) => q.cited_by_grade?.[g]).map((g) => (
              <span key={g} className="mr-2">
                {g} <span className="num text-foreground">{q.cited_by_grade?.[g]}</span>
              </span>
            ))}
            <span>(A document, B checked web page, D fincalc, C unchecked web, U unsupported)</span>
          </p>
        )}
      </div>
      <p className="mt-2 rounded-md bg-background-subtle/70 px-2 py-1.5 text-[11px] text-muted">
        A verified citation is not a correct forecast. How past views turned out is tracked on{" "}
        <Link href="/signals" className="font-medium text-brand hover:underline">Signals</Link>.
      </p>
      <button type="button" onClick={onEvidence} className="mt-3 text-xs font-medium text-brand hover:underline">
        Explore every claim →
      </button>
    </Card>
  );
}

export function SummaryTab({ report, ins, claims, onOpen, title, goTab, extra }: {
  report: Report; ins: Insights | null; claims: ClaimMap; onOpen: OpenClaim; title: string; goTab: (t: string) => void; extra?: React.ReactNode;
}) {
  const [allRisks, setAllRisks] = useState(false);
  const word = ins?.verdict.word ?? parseVerdict(report.markdown)?.word;
  const bullets: string[] = [];
  if (word) bullets.push(`${report.kind === "stock_report" ? "The research view (informational, no validated edge) is" : "The report's view is"} **${word}**${ins?.verdict.confidence ? ` with ${ins.verdict.confidence} confidence` : ""}${ins?.verdict.horizon ? `, for a ${ins.verdict.horizon} horizon` : ""}.`);
  bullets.push(...(ins?.plain_english ?? []).slice(0, 4));
  if (ins?.verdict.condition) bullets.push(`**What would change the view:** ${firstSentence(ins.verdict.condition)}`);
  const risks = ins?.risks ?? [];
  return (
    <div className="space-y-5">
      <VerdictHero report={report} ins={ins} claims={claims} onOpen={onOpen} title={title} />
      {report.kind === "stock_report" && <SinceReport runId={report.run_id} />}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <Card title="In plain English" icon={<Lightbulb className="size-4" />} subtitle="What the report is saying, without the jargon. Chips open the evidence.">
          <ul className="space-y-2.5">
            {bullets.map((b, i) => (
              <li key={i} className="flex gap-2.5 text-[14px] leading-relaxed">
                <span className="mt-2 size-1.5 shrink-0 rounded-full bg-brand" />
                <CiteText text={b} claims={claims} onOpen={onOpen} />
              </li>
            ))}
          </ul>
          {ins?.verdict.summary && (
            <details className="group mt-4 rounded-lg bg-background-subtle/60 px-3 py-2 text-sm">
              <summary className="cursor-pointer text-xs font-medium text-muted group-open:mb-2">The analyst&apos;s executive summary</summary>
              <CiteText block text={ins.verdict.summary} claims={claims} onOpen={onOpen} className="text-[13px] leading-relaxed" />
            </details>
          )}
        </Card>
        {ins && <QualityCard q={ins.quality} onEvidence={() => goTab("evidence")} />}
      </div>

      {ins && ins.key_numbers.length > 0 && (
        <section aria-label="Key numbers">
          <h2 className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
            Key numbers
            <InfoTip>Straight from the claim ledger, with the period and whether the verifier confirmed it. Changes are computed by fincalc from the two cited figures.</InfoTip>
          </h2>
          <div className="stagger grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
            {ins.key_numbers.map((t) => (
              <KeyTile key={`${t.label}-${t.claim_id}`} t={t} claims={claims} onOpen={onOpen} />
            ))}
          </div>
        </section>
      )}

      {ins?.accuracy && <AccuracyCard acc={ins.accuracy} claims={claims} onOpen={onOpen} />}

      {extra}

      <div className="grid gap-5 lg:grid-cols-2">
        <CaseCard title="Reasons for" icon={<ThumbsUp className="size-4" />} tone="gain" points={ins?.pros ?? []} claims={claims} onOpen={onOpen}
          empty="The synthesis has no structured reasons; see Bull vs bear in the full report." />
        <CaseCard title="Reasons against" icon={<ThumbsDown className="size-4" />} tone="loss" points={ins?.cons ?? []} claims={claims} onOpen={onOpen}
          empty="The synthesis has no structured reasons; see Bull vs bear in the full report." />
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card title="Top risks" icon={<AlertTriangle className="size-4" />}
          subtitle={risks.length ? `Ranked by the report${risks.some((r) => r.severity) ? ", with its own severity" : " (most serious first)"}` : undefined}>
          {risks.length === 0 ? (
            <p className="text-sm text-muted">This report has no ranked risk list; the reasons against above cover the downside.</p>
          ) : (
            <ol className="space-y-3">
              {(allRisks ? risks : risks.slice(0, 5)).map((r) => (
                <li key={r.rank} className="flex gap-3">
                  <span className="num grid size-6 shrink-0 place-items-center rounded-full bg-background-subtle text-[11px] font-semibold ring-1 ring-inset ring-border">{r.rank}</span>
                  <div className="min-w-0 text-sm">
                    <p className="flex flex-wrap items-center gap-1.5 font-medium">
                      {r.title}
                      {r.severity && <Badge tone={SEV_TONE(r.severity)}>{r.severity}</Badge>}
                    </p>
                    {r.text && (
                      <p className="mt-0.5 line-clamp-3 text-[13px] leading-relaxed text-muted">
                        <CiteText text={r.text} claims={claims} onOpen={onOpen} />
                      </p>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          )}
          {risks.length > 5 && (
            <button type="button" onClick={() => setAllRisks((a) => !a)} className="mt-3 text-xs font-medium text-brand hover:underline">
              {allRisks ? "Show the top 5" : `Show all ${risks.length} risks`}
            </button>
          )}
        </Card>
        <div className="space-y-5">
          <Card title="What to do" icon={<ClipboardCheck className="size-4" />} subtitle="The report's action checklist; tick steps off as you go (saved in this browser)">
            {ins?.checklist.length ? (
              <Checklist runId={report.run_id} items={ins.checklist} claims={claims} onOpen={onOpen} />
            ) : (
              <p className="text-sm text-muted">No structured checklist; see the action section of the full report.</p>
            )}
          </Card>
          {ins?.verdict.condition && (
            <Card title="What would change the view" icon={<Compass className="size-4" />}>
              <p className="text-sm leading-relaxed">
                <CiteText text={ins.verdict.condition} claims={claims} onOpen={onOpen} />
              </p>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
