"use client";

import {
  BellRing, BookOpenCheck, BriefcaseBusiness, CalendarDays, CircleHelp, FlaskConical, IndianRupee, ListChecks, Monitor,
  Moon, Palette, Plus, ShieldAlert, Sparkles, Sun, Trash2, UserRound,
} from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";

import {
  applyReduceMotion, Avatar, Labelled, LinkButton, AVATAR_COLORS, AVATAR_GRADIENT, SaveBar, Switch, useDraft, withDefaults,
} from "@/components/profile/common";
import { NotificationsCard } from "@/components/profile/notifications";
import { TimeFramesCard } from "@/components/profile/time-frames";
import { NAV, type Theme, useTheme } from "@/components/shell";
import {
  Badge, Button, Callout, Card, cx, EmptyState, ErrorNote, Field, InfoTip, inputClass, PageHeader, Segmented, Skeleton,
  Stat,
} from "@/components/ui";
import {
  api, day, type Decision, type Preferences, type Profile, type ProfileStats, type RunSummary, useApi, type WatchSummary, when,
} from "@/lib/api";
import { DEFAULT_TIME_FRAMES, DEFAULT_WATCH } from "@/lib/timeframes";

const CATEGORIES: { value: Profile["category"]; label: string; range: string; note: string }[] = [
  { value: "retail", label: "Retail (RII)", range: "Up to ₹2 lakh", note: "Lottery of one minimum lot when oversubscribed." },
  { value: "shni", label: "Small NII (sHNI)", range: "₹2 – 10 lakh", note: "One-third of the NII book; lottery of the minimum sHNI bid." },
  { value: "bhni", label: "Big NII (bHNI)", range: "Above ₹10 lakh", note: "Two-thirds of the NII book; higher capital, lower odds." },
];

const CATEGORY_CAP: Record<Profile["category"], [number, number | null]> = {
  retail: [0, 200000], shni: [200000, 1000000], bhni: [1000000, null],
};

const TAX_SLABS = ["0", "5", "10", "15", "20", "30"];

const LANDINGS = NAV.flatMap((g) => g.items).filter((n) =>
  ["/", "/ipos", "/stocks", "/funds", "/bonds", "/fno", "/runs", "/monitor", "/journal"].includes(n.href));

const inr = (v: string | number | null | undefined) => {
  const n = Number(v);
  return Number.isFinite(n) ? `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}` : "—";
};

/** "₹2,50,000" → "2.5 lakh", for a readable hint under money inputs. */
const inWords = (v: string) => {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return null;
  if (n >= 1e7) return `${+(n / 1e7).toFixed(2)} crore`;
  if (n >= 1e5) return `${+(n / 1e5).toFixed(2)} lakh`;
  if (n >= 1e3) return `${+(n / 1e3).toFixed(1)} thousand`;
  return null;
};

const isNum = (v: string | null | undefined) => v != null && /^\d+(\.\d+)?$/.test(v.trim());

function Label({ children, tip }: { children: ReactNode; tip: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1">
      {children}
      <InfoTip>{tip}</InfoTip>
    </span>
  );
}

export function ProfilePage() {
  const { data, error, reload } = useApi<Profile>("/api/profile");
  const stats = useProfileStats();
  const loaded = data ? withDefaults(data) : null;
  const { draft: p, setDraft, dirty, discard, commit } = useDraft<Profile>(loaded);
  const { theme, setTheme } = useTheme();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const savedMotion = data?.preferences?.reduce_motion;
  useEffect(() => { if (savedMotion != null) applyReduceMotion(savedMotion); }, [savedMotion]);

  if (!p) {
    return (
      <div>
        <PageHeader icon={<UserRound className="size-5" />} title="Profile" description="Who you are as an investor, and how the app should look for you." />
        {error ? <ErrorNote error={error} onRetry={reload} /> : <ProfileSkeleton />}
      </div>
    );
  }

  const set = <K extends keyof Profile>(k: K, v: Profile[K]) => setDraft({ ...p, [k]: v });
  const prefs = p.preferences!;
  const setPref = <K extends keyof Preferences>(k: K, v: Preferences[K]) => set("preferences", { ...prefs, [k]: v });

  const invalid =
    !isNum(p.capital_per_ipo_inr) ? "Capital per IPO must be a number of rupees, e.g. 15000." :
    !isNum(p.tax_slab_pct) || Number(p.tax_slab_pct) > 50 ? "Tax slab must be a percentage between 0 and 50." :
    p.holdings.some((h) => !h.symbol.trim()) ? "Every holding needs a symbol (or remove the empty row)." :
    p.holdings.some((h) => h.value_inr && !isNum(h.value_inr)) ? "Holding values must be numbers of rupees." :
    (p.display_name ?? "").length > 60 ? "Your name can be at most 60 characters." :
    p.fno_capital_inr != null && !isNum(p.fno_capital_inr) ? "F&O capital must be a number of rupees (0 = not set)." :
    p.fno_max_loss_pct != null && (!isNum(p.fno_max_loss_pct) || Number(p.fno_max_loss_pct) <= 0 || Number(p.fno_max_loss_pct) > 100) ? "Max loss per strategy must be a percentage above 0 and at most 100." :
    p.fno_brokerage_per_order_inr != null && (!isNum(p.fno_brokerage_per_order_inr) || Number(p.fno_brokerage_per_order_inr) > 1000) ? "Brokerage per order must be between ₹0 and ₹1,000." :
    prefs.watch?.quiet_start && prefs.watch.quiet_start === prefs.watch.quiet_end ? "Quiet hours must start and end at different times." : null;

  const save = async () => {
    setSaving(true);
    try {
      const body = { ...p, holdings: p.holdings.map((h) => ({ ...h, value_inr: h.value_inr || null, sector: h.sector || null })) };
      const saved = withDefaults(await api<Profile>("/api/profile", { method: "PUT", body: JSON.stringify(body) }));
      commit(saved);
      applyReduceMotion(!!saved.preferences?.reduce_motion);
      setSaveError(null);
      setSavedAt(Date.now());
      stats.reload();
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  const onDiscard = () => {
    discard();
    applyReduceMotion(!!loaded?.preferences?.reduce_motion, false);
    setSaveError(null);
  };

  const [lo, hi] = CATEGORY_CAP[p.category];
  const cap = Number(p.capital_per_ipo_inr);
  const capWarn = isNum(p.capital_per_ipo_inr) && (cap > (hi ?? Infinity) || (cap > 0 && cap <= lo))
    ? `${inr(cap)} is outside the ${CATEGORIES.find((c) => c.value === p.category)!.range.toLowerCase()} range for this category. Suggestions cap lots at whichever limit is lower.`
    : null;
  const s = stats.data;
  const skipRules = p.rules.filter((r) => r.action === "skip").length;

  return (
    <div className="pb-4">
      <PageHeader
        icon={<UserRound className="size-5" />}
        title="Profile"
        description="Who you are as an investor. Suggestions use this to size bids and pick your category; your rules then gate them."
        actions={
          <>
            <LinkButton href="/rules" variant="secondary" icon={<ListChecks className="size-3.5" />}>My rules</LinkButton>
            <LinkButton href="/help#getting-started" variant="ghost" icon={<CircleHelp className="size-3.5" />}>How this works</LinkButton>
          </>
        }
      />

      {/* ---------------------------------------------------------------- identity */}
      <section className="relative mb-6 overflow-hidden rounded-2xl border border-border bg-card shadow-card animate-fade-up">
        <div className={cx("h-20 bg-gradient-to-r opacity-90 sm:h-24", AVATAR_GRADIENT[p.avatar_color ?? "brand"])} />
        <div className="px-4 pb-5 sm:px-6">
          <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
            <Avatar name={p.display_name || "Investor"} color={p.avatar_color} size="lg" className="-mt-8 ring-4 ring-card sm:-mt-10" />
            <div className="min-w-0 flex-1 basis-60 pt-3">
              <label className="sr-only" htmlFor="display-name">Your name</label>
              <input
                id="display-name"
                value={p.display_name ?? ""}
                maxLength={60}
                onChange={(e) => set("display_name", e.target.value)}
                placeholder="Add your name"
                className="w-full max-w-md rounded-lg border border-transparent bg-transparent px-2 py-1 -ml-2 text-xl font-semibold tracking-tight transition placeholder:text-muted/60 hover:border-border focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20 sm:text-2xl"
              />
              <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                <span className="inline-flex items-center gap-1">
                  <CalendarDays className="size-3.5" />
                  {s?.first_run_at ? <>Researching since <span className="text-foreground">{day(s.first_run_at)}</span></> : stats.error ? "Activity unavailable" : s ? "No research runs yet" : "…"}
                </span>
                {s?.profile_updated_at && <span>Profile saved {when(s.profile_updated_at)}</span>}
              </p>
            </div>
            <div className="flex flex-wrap gap-1.5">
              <Badge tone="brand">{CATEGORIES.find((c) => c.value === p.category)?.label}</Badge>
              <Badge tone={p.risk_appetite === "high" ? "warn" : p.risk_appetite === "low" ? "info" : "accent"}>{p.risk_appetite} risk</Badge>
              <Badge tone="neutral">{inr(p.capital_per_ipo_inr)} per IPO</Badge>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="flex items-center gap-1 text-xs text-muted"><Palette className="size-3.5" /> Avatar colour</span>
            {AVATAR_COLORS.map((c) => (
              <button
                key={c}
                type="button"
                aria-label={`Avatar colour ${c}`}
                aria-pressed={(p.avatar_color ?? "brand") === c}
                onClick={() => set("avatar_color", c)}
                className={cx(
                  "size-6 rounded-full bg-gradient-to-br ring-offset-2 ring-offset-card transition hover:scale-110",
                  AVATAR_GRADIENT[c],
                  (p.avatar_color ?? "brand") === c && "ring-2 ring-foreground/60",
                )}
              />
            ))}
          </div>
        </div>
      </section>

      {/* ---------------------------------------------------------------- activity */}
      <div className="stagger mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {stats.error ? (
          <div className="col-span-2 lg:col-span-4"><ErrorNote error={stats.error} onRetry={stats.reload} /></div>
        ) : !s ? (
          Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[104px] rounded-xl" />)
        ) : (
          <>
            <Stat label="Research runs" value={s.runs} icon={<FlaskConical className="size-4" />} hint={`${s.runs_done} finished`} href="/runs" />
            <Stat label="Decisions" value={s.decisions} icon={<BookOpenCheck className="size-4" />} tone="accent" hint={`${s.applied} applied`} href="/journal"
              help="Suggestions you asked for, recorded in your decision journal with what you did and the outcome." />
            <Stat label="Watches" value={s.watches} icon={<BellRing className="size-4" />} tone="info" hint={`${s.active_watches} active`} href="/monitor"
              help="IPOs and stocks the monitor checks on a schedule (subscription, allotment, listing, lock-ins)." />
            <Stat label="Personal rules" value={p.rules.length} icon={<ListChecks className="size-4" />} tone="warn" hint={`${skipRules} skip · ${p.rules.length - skipRules} warn`} href="/rules" />
          </>
        )}
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-6">
          {/* ------------------------------------------------------------ investor profile */}
          <Card
            icon={<IndianRupee className="size-4" />}
            title="Investor profile"
            subtitle="Used for every personal suggestion. Python applies these limits after the AI answers, so they always hold."
          >
            <div className="space-y-5">
              <div>
                <p className="mb-2 flex items-center gap-1 text-xs font-medium text-muted">
                  Investor category
                  <InfoTip>
                    SEBI splits every main-board IPO into buckets. Retail individuals (RII) bid up to ₹2 lakh; non-institutional
                    investors (NII, or HNI) bid more and are split into small (sHNI, ₹2–10 lakh) and big (bHNI, above ₹10 lakh).
                    The suggestion always uses the category you pick here.
                  </InfoTip>
                </p>
                <div role="radiogroup" className="grid gap-2 sm:grid-cols-3">
                  {CATEGORIES.map((c) => {
                    const on = p.category === c.value;
                    return (
                      <button key={c.value} type="button" role="radio" aria-checked={on} onClick={() => set("category", c.value)}
                        className={cx("rounded-xl border p-3 text-left transition duration-150",
                          on ? "border-brand bg-brand-soft shadow-glow" : "border-border hover:border-border-strong hover:bg-card-hover")}>
                        <span className="flex items-center justify-between gap-2">
                          <span className="text-sm font-semibold">{c.label}</span>
                          <span className={cx("grid size-4 place-items-center rounded-full ring-1 ring-inset", on ? "bg-brand ring-brand" : "ring-border-strong")}>
                            {on && <span className="size-1.5 rounded-full bg-brand-fg" />}
                          </span>
                        </span>
                        <span className="mt-0.5 block text-xs font-medium text-brand">{c.range}</span>
                        <span className="mt-1 block text-[11px] leading-snug text-muted">{c.note}</span>
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <Labelled
                  label={<Label tip="The most you want to put into one IPO application. Suggestions never recommend more lots than this buys at the upper price band; your category's SEBI limit also applies.">Capital per IPO</Label>}
                  hint={inWords(p.capital_per_ipo_inr) ? `= ₹${inWords(p.capital_per_ipo_inr)}` : "In rupees, e.g. 15000"}
                >
                  <div className="relative">
                    <span className="pointer-events-none absolute inset-y-0 left-3 grid place-items-center text-sm text-muted">₹</span>
                    <input aria-label="Capital per IPO in rupees" className={cx(inputClass, "num w-full pl-7")} inputMode="decimal" value={p.capital_per_ipo_inr}
                      onChange={(e) => set("capital_per_ipo_inr", e.target.value.replace(/[,\s₹]/g, ""))} />
                  </div>
                </Labelled>
                <Labelled
                  label={<Label tip="Your income-tax slab. The advisor reads it when comparing after-tax outcomes. Note: gains on listed shares sold within a year are short-term capital gains, taxed at a flat rate whatever your slab.">Tax slab</Label>}
                  hint="Percent of income tax you pay"
                >
                  <div className="flex items-center gap-2">
                    <div className="relative w-24 shrink-0">
                      <input aria-label="Tax slab in percent" className={cx(inputClass, "num w-full pr-7")} inputMode="decimal" value={p.tax_slab_pct}
                        onChange={(e) => set("tax_slab_pct", e.target.value.replace(/[%\s]/g, ""))} />
                      <span className="pointer-events-none absolute inset-y-0 right-3 grid place-items-center text-sm text-muted">%</span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {TAX_SLABS.map((t) => (
                        <button key={t} type="button" onClick={() => set("tax_slab_pct", t)}
                          className={cx("num rounded-md px-1.5 py-0.5 text-[11px] ring-1 ring-inset transition",
                            Number(p.tax_slab_pct) === Number(t) ? "bg-brand-soft text-brand ring-brand/30" : "text-muted ring-border hover:text-foreground")}>
                          {t}%
                        </button>
                      ))}
                    </div>
                  </div>
                </Labelled>
              </div>
              {capWarn && <Callout tone="warn">{capWarn}</Callout>}

              <div className="grid gap-4 sm:grid-cols-2">
                <Labelled label={<Label tip="How much short-term loss you can live with. Low risk makes the advisor stricter about weak demand and expensive valuations; high risk lets it consider speculative issues.">Risk appetite</Label>}>
                  <div><Segmented value={p.risk_appetite} onChange={(v) => set("risk_appetite", v)}
                    options={[{ value: "low", label: "Low" }, { value: "medium", label: "Medium" }, { value: "high", label: "High" }]} /></div>
                </Labelled>
                <Labelled label={<Label tip="How long you plan to hold after listing. 'Listing' means you sell on listing day, so demand and subscription matter most; 'Long term' weighs the business and valuation more.">Horizon</Label>}>
                  <div><Segmented value={p.horizon} onChange={(v) => set("horizon", v)}
                    options={[{ value: "listing", label: "Listing day" }, { value: "short", label: "Months" }, { value: "long", label: "Years" }]} /></div>
                </Labelled>
              </div>

              <Labelled
                label={<Label tip="Anything the advisor should know: e.g. 'avoid NBFCs', 'I already hold two cable companies'. The AI reads it; your rules, not notes, are what Python enforces.">Notes for the advisor</Label>}
                hint={`${p.notes.length} characters`}
              >
                <textarea aria-label="Notes for the advisor" className={cx(inputClass, "h-auto min-h-20 w-full py-2")} rows={3} value={p.notes}
                  placeholder="e.g. Prefer profitable companies; avoid issues that are mostly offer-for-sale."
                  onChange={(e) => set("notes", e.target.value)} />
              </Labelled>
            </div>
          </Card>

          {/* ------------------------------------------------------------ F&O budget */}
          {p.fno_capital_inr != null && (
            <Card icon={<ShieldAlert className="size-4" />} title="F&O risk budget"
              help="Used only by the F&O page and its signal: a strategy passes only if its maximum loss is within this share of your F&O capital. Analysis only; no orders are placed."
              subtitle="SEBI found 87.7% of individual F&O traders lost money in FY26. Keep this small.">
              <div className="grid gap-4 sm:grid-cols-3">
                <Labelled label={<Label tip="Money you have set aside for F&O and could lose without affecting your goals. 0 means not set: the F&O signal then makes no call.">F&amp;O capital</Label>}
                  hint={inWords(p.fno_capital_inr) ? `= ₹${inWords(p.fno_capital_inr)}` : "0 = not set"}>
                  <div className="relative">
                    <span className="pointer-events-none absolute inset-y-0 left-3 grid place-items-center text-sm text-muted">₹</span>
                    <input aria-label="F&O capital in rupees" className={cx(inputClass, "num w-full pl-7")} inputMode="decimal" value={p.fno_capital_inr}
                      onChange={(e) => set("fno_capital_inr", e.target.value.replace(/[,\s₹]/g, ""))} />
                  </div>
                </Labelled>
                <Labelled label={<Label tip="The most one strategy may lose, as a share of your F&O capital. The research roadmap suggests 2% or less.">Max loss per strategy</Label>} hint="% of F&O capital">
                  <div className="relative w-28">
                    <input aria-label="Max loss per strategy in percent" className={cx(inputClass, "num w-full pr-7")} inputMode="decimal" value={p.fno_max_loss_pct ?? "2"}
                      onChange={(e) => set("fno_max_loss_pct", e.target.value.replace(/[%\s]/g, ""))} />
                    <span className="pointer-events-none absolute inset-y-0 right-3 grid place-items-center text-sm text-muted">%</span>
                  </div>
                </Labelled>
                <Labelled label={<Label tip="Your broker's flat charge per executed order. Used in the cost breakdown (statutory charges come from a dated table).">Brokerage per order</Label>} hint="₹ per executed order">
                  <div className="relative w-28">
                    <span className="pointer-events-none absolute inset-y-0 left-3 grid place-items-center text-sm text-muted">₹</span>
                    <input aria-label="Brokerage per order in rupees" className={cx(inputClass, "num w-full pl-7")} inputMode="decimal" value={p.fno_brokerage_per_order_inr ?? "20"}
                      onChange={(e) => set("fno_brokerage_per_order_inr", e.target.value.replace(/[,\s₹]/g, ""))} />
                  </div>
                </Labelled>
              </div>
              <div className="mt-4 grid gap-4 sm:grid-cols-2">
                <Labelled label={<Label tip="Defined risk means the worst case is known in advance (spreads, condors, bought options). Naked short options can lose far more than the premium.">Strategies allowed</Label>}>
                  <div><Segmented value={p.fno_defined_risk_only === false ? "any" : "defined"} onChange={(v) => set("fno_defined_risk_only", v === "defined")}
                    options={[{ value: "defined", label: "Defined risk only" }, { value: "any", label: "Any" }]} /></div>
                </Labelled>
                <Labelled label={<Label tip="Your experience with futures and options. Shown with the F&O signal; it does not loosen any limit.">Experience</Label>}>
                  <div><Segmented value={p.fno_experience ?? "none"} onChange={(v) => set("fno_experience", v as Profile["fno_experience"])}
                    options={[{ value: "none", label: "None" }, { value: "some", label: "Some" }, { value: "experienced", label: "Experienced" }]} /></div>
                </Labelled>
              </div>
            </Card>
          )}

          {/* ------------------------------------------------------------ time frames & watch windows */}
          <TimeFramesCard tf={prefs.time_frames ?? DEFAULT_TIME_FRAMES} watch={prefs.watch ?? DEFAULT_WATCH}
            onTf={(v) => setPref("time_frames", v)} onWatch={(v) => setPref("watch", v)} />

          {/* ------------------------------------------------------------ notifications (own save: secrets live outside the profile) */}
          <div id="notifications" className="scroll-mt-20">
            <NotificationsCard />
          </div>

          {/* ------------------------------------------------------------ holdings */}
          <Card
            icon={<BriefcaseBusiness className="size-4" />}
            title="Current holdings"
            help="Shares you already own. The advisor uses them to flag concentration, e.g. a second IPO in a sector you already hold heavily."
            subtitle="Optional. Helps the advisor spot sector concentration."
            actions={
              <Button variant="secondary" icon={<Plus className="size-3.5" />}
                onClick={() => set("holdings", [...p.holdings, { symbol: "", sector: "", value_inr: "" }])}>
                Add
              </Button>
            }
          >
            {!p.holdings.length ? (
              <EmptyState icon={<BriefcaseBusiness className="size-5" />} title="No holdings added">
                Add the stocks you own (symbol, sector and rough value) so suggestions can warn about concentration.
              </EmptyState>
            ) : (
              <div className="space-y-2">
                <div className="hidden grid-cols-[1fr_1fr_1fr_auto] gap-2 px-1 text-[11px] font-medium uppercase tracking-wider text-muted sm:grid">
                  <span>Symbol</span><span>Sector</span><span>Value (₹)</span><span className="w-8" />
                </div>
                {p.holdings.map((h, i) => {
                  const setH = (patch: Partial<typeof h>) => set("holdings", p.holdings.map((x, j) => (j === i ? { ...x, ...patch } : x)));
                  return (
                    <div key={i} className="grid grid-cols-2 gap-2 rounded-lg border border-border p-2 animate-fade-in sm:grid-cols-[1fr_1fr_1fr_auto] sm:border-0 sm:p-0">
                      <input aria-label="Symbol" placeholder="e.g. TCS" className={cx(inputClass, "col-span-2 uppercase sm:col-span-1")} value={h.symbol}
                        onChange={(e) => setH({ symbol: e.target.value.toUpperCase() })} />
                      <input aria-label="Sector" placeholder="e.g. IT services" className={inputClass} value={h.sector ?? ""}
                        onChange={(e) => setH({ sector: e.target.value })} />
                      <input aria-label="Value in rupees" placeholder="50000" inputMode="decimal" className={cx(inputClass, "num")} value={h.value_inr ?? ""}
                        onChange={(e) => setH({ value_inr: e.target.value.replace(/[,\s₹]/g, "") })} />
                      <button type="button" aria-label={`Remove ${h.symbol || "holding"}`}
                        onClick={() => set("holdings", p.holdings.filter((_, j) => j !== i))}
                        className="col-span-2 grid h-9 place-items-center rounded-lg text-muted transition hover:bg-loss-soft hover:text-loss sm:col-span-1 sm:w-9">
                        <Trash2 className="size-4" />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </div>

        <div className="space-y-6">
          {/* ------------------------------------------------------------ preferences */}
          <Card icon={<Sparkles className="size-4" />} title="Preferences" subtitle="How the dashboard looks and behaves for you.">
            <div className="space-y-5">
              <Labelled label="Theme">
                <ThemePicker theme={theme} setTheme={setTheme} />
                <span className="block text-[11px] text-muted">Applies instantly and is remembered on this browser.</span>
              </Labelled>
              <Field label="Start page" hint="The page you want to open first. Saved with your profile.">
                <select className={cx(inputClass, "w-full")} value={prefs.default_landing}
                  onChange={(e) => setPref("default_landing", e.target.value as Preferences["default_landing"])}>
                  {LANDINGS.map((n) => <option key={n.href} value={n.href}>{n.label}</option>)}
                </select>
              </Field>
              <Labelled
                label={<Label tip="Indian style writes big amounts in lakh and crore (1,00,000 = 1 lakh; 1,00,00,000 = 1 crore). International style uses thousand and million (1 million = 10 lakh).">Big numbers</Label>}
                hint="Saved with your profile; pages switch over as they adopt it."
              >
                <div><Segmented value={prefs.number_format} onChange={(v) => setPref("number_format", v)}
                  options={[{ value: "lakh_crore", label: "₹12.5 lakh" }, { value: "million", label: "₹1.25 million" }]} /></div>
              </Labelled>
              <div className="space-y-4 border-t border-border pt-4">
                <Switch checked={prefs.compact_tables} onChange={(v) => setPref("compact_tables", v)}
                  label="Compact tables" description="Tighter rows to see more at once. Saved with your profile." />
                <Switch checked={prefs.reduce_motion}
                  onChange={(v) => { setPref("reduce_motion", v); applyReduceMotion(v, false); }}
                  label="Reduce motion" description="Turn off entrance animations and transitions. Previews now; kept after you save." />
              </div>
            </div>
          </Card>

          <Card icon={<ListChecks className="size-4" />} title="Rules that gate suggestions" actions={<Link href="/rules" className="text-xs font-medium text-brand hover:underline">Edit</Link>}>
            {p.rules.length ? (
              <ul className="space-y-1.5 text-sm">
                {p.rules.slice(0, 5).map((r) => (
                  <li key={r.id} className="flex items-center gap-2">
                    <Badge tone={r.action === "skip" ? "loss" : "warn"}>{r.action}</Badge>
                    <span className="truncate text-muted">{r.description || `${r.metric} ${r.op} ${r.value}`}</span>
                  </li>
                ))}
                {p.rules.length > 5 && <li className="text-xs text-muted">+{p.rules.length - 5} more</li>}
              </ul>
            ) : (
              <p className="text-sm text-muted">No rules yet. <Link href="/rules" className="text-brand underline-offset-2 hover:underline">Add a rule</Link> such as “skip if QIB demand is below 1x”.</p>
            )}
          </Card>
        </div>
      </div>

      <SaveBar dirty={dirty} saving={saving} error={saveError} invalid={invalid} savedAt={savedAt} onSave={save} onDiscard={onDiscard} />
    </div>
  );
}

/** GET /api/profile/stats, falling back to counting the lists when the API predates that endpoint. */
function useProfileStats() {
  const direct = useApi<ProfileStats>("/api/profile/stats");
  const old = direct.error?.startsWith("Not Found") ?? false;
  const runs = useApi<RunSummary[]>(old ? "/api/runs?limit=500" : null);
  const decisions = useApi<Decision[]>(old ? "/api/decisions" : null);
  const watches = useApi<WatchSummary[]>(old ? "/api/watches" : null);
  if (!old) return direct;
  const error = runs.error ?? decisions.error ?? watches.error;
  const ready = runs.data && decisions.data && watches.data;
  const firstRun = runs.data?.map((r) => r.created_at).filter(Boolean).sort()[0] ?? null;
  const data: ProfileStats | null = ready ? {
    runs: runs.data!.length, runs_done: runs.data!.filter((r) => r.status === "done").length,
    decisions: decisions.data!.length, applied: decisions.data!.filter((d) => d.user_action === "applied").length,
    watches: watches.data!.length, active_watches: watches.data!.filter((w) => w.active).length,
    first_run_at: firstRun, profile_updated_at: null,
  } : null;
  return { data, error, reload: () => { runs.reload(); decisions.reload(); watches.reload(); } };
}

function ThemePicker({ theme, setTheme }: { theme: Theme; setTheme: (t: Theme) => void }) {
  const opts: { value: Theme; label: string; icon: ReactNode; swatch: string }[] = [
    { value: "light", label: "Light", icon: <Sun className="size-3.5" />, swatch: "bg-[#f5f7fb]" },
    { value: "dark", label: "Dark", icon: <Moon className="size-3.5" />, swatch: "bg-[#070b14]" },
    { value: "system", label: "System", icon: <Monitor className="size-3.5" />, swatch: "bg-gradient-to-r from-[#f5f7fb] from-50% to-[#070b14] to-50%" },
  ];
  return (
    <div role="radiogroup" aria-label="Theme" className="grid grid-cols-3 gap-2">
      {opts.map((o) => {
        const on = theme === o.value;
        return (
          <button key={o.value} type="button" role="radio" aria-checked={on} onClick={() => setTheme(o.value)}
            className={cx("rounded-lg border p-1.5 text-left transition", on ? "border-brand shadow-glow" : "border-border hover:border-border-strong")}>
            <span className={cx("block h-10 rounded-md ring-1 ring-inset ring-border", o.swatch)} />
            <span className={cx("mt-1.5 flex items-center gap-1 px-0.5 text-xs font-medium", on ? "text-brand" : "text-muted")}>{o.icon}{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function ProfileSkeleton() {
  return (
    <div className="space-y-6">
      <Skeleton className="h-44 w-full rounded-2xl" />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[104px] rounded-xl" />)}
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
        <Skeleton className="h-96 rounded-xl" />
        <Skeleton className="h-72 rounded-xl" />
      </div>
    </div>
  );
}
