"use client";

import {
  Ban, BellRing, Check, CircleHelp, Siren, Smartphone, Cpu, DatabaseZap, ListChecks, Plus, ShieldCheck, Sparkles, Trash2, TriangleAlert, UserRound,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Labelled, LinkButton, SaveBar, useDraft, withDefaults } from "@/components/profile/common";
import { AlertRulesPanel, KIND_LABEL, newAlertRule, validateAlertRules } from "@/components/rules/alert-rules";
import {
  isNumber, METRIC, METRICS, OPS, sentence, TEMPLATES, uniqueId, UNIT_SUFFIX, validate,
} from "@/components/rules/metrics";
import {
  Badge, Button, Card, cx, EmptyState, ErrorNote, Field, InfoTip, inputClass, PageHeader, Segmented, Skeleton, Stat,
} from "@/components/ui";
import {
  type AlertKind, type AlertRegistry, type AlertRule, type AlertRuleState, api, type NotificationSettings, type Profile, type Rule,
  useApi, type WatchSummary,
} from "@/lib/api";

const KINDS: AlertKind[] = ["ipo", "stock", "fund", "bond", "fno", "portfolio"];
const FNO_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY"];

/** The asset tab from ?kind=… (so alert links and the profile can deep-link a tab). */
function useKindTab(): [AlertKind, (k: AlertKind) => void] {
  const [tab, setTab] = useState<AlertKind>("ipo");
  useEffect(() => {
    const k = new URLSearchParams(window.location.search).get("kind") as AlertKind | null;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- read the tab from the URL once on mount
    if (k && KINDS.includes(k)) setTab(k);
  }, []);
  const choose = (k: AlertKind) => {
    setTab(k);
    const url = new URL(window.location.href);
    url.searchParams.set("kind", k);
    window.history.replaceState(null, "", url);
  };
  return [tab, choose];
}

const GROUPS = ["Demand", "Price and lots", "Timing and quality", "Signal"] as const;

export function RulesPage() {
  const { data, error, reload } = useApi<Profile>("/api/profile");
  const loaded = data ? withDefaults(data) : null;
  const { draft: p, setDraft, dirty, discard, commit } = useDraft<Profile>(loaded);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);

  const [tab, setTab] = useKindTab();
  const registry = useApi<AlertRegistry>("/api/alert-rules/registry");
  const states = useApi<AlertRuleState[]>("/api/alert-rules/state", 60000);
  const notifications = useApi<NotificationSettings>("/api/notifications");
  const watches = useApi<WatchSummary[]>("/api/watches");
  const companies = useApi<{ slug: string; name: string }[]>("/api/companies");

  const problems = useMemo(() => (p ? validate(p.rules) : {}), [p]);
  const alertRules = useMemo(() => p?.alert_rules ?? [], [p]);
  const alertProblems = useMemo(() => validateAlertRules(alertRules, registry.data?.metrics ?? []), [alertRules, registry.data]);
  const [savedRules, setSavedRules] = useState<AlertRule[] | null>(null);
  const savedAlertIds = useMemo(() => {
    const saved = new Map((savedRules ?? data?.alert_rules ?? []).map((r) => [r.id, JSON.stringify(r)]));
    return new Set(alertRules.filter((r) => saved.get(r.id) === JSON.stringify(r)).map((r) => r.id));
  }, [savedRules, data, alertRules]);
  const suggestions = useMemo(() => {
    const w = watches.data ?? [];
    const slugs = (companies.data ?? []).map((c) => c.slug);
    const stocks = w.filter((x) => x.kind === "stock").map((x) => x.key);
    return {
      ipo: w.filter((x) => x.kind === "ipo" && x.nse_symbol).map((x) => x.nse_symbol!),
      stock: stocks,
      fund: slugs.filter((x) => x.startsWith("mf-")).map((x) => x.slice(3)),
      bond: slugs.filter((x) => x.startsWith("bond-")).map((x) => x.slice(5).toUpperCase()),
      fno: [...FNO_UNDERLYINGS, ...stocks.filter((x) => !x.startsWith("BSE:"))],
      portfolio: [],
    } as Record<AlertKind, string[]>;
  }, [watches.data, companies.data]);

  const header = (
    <PageHeader
      icon={<ListChecks className="size-5" />}
      title="Rules"
      description="Your red lines for IPO suggestions, and alerts for every asset you follow. Python checks them against live data; the AI never does."
      actions={
        <>
          <LinkButton href="/help#how-it-works" variant="ghost" icon={<CircleHelp className="size-3.5" />}>How rules work</LinkButton>
          {p && tab === "ipo" && <Button icon={<Plus className="size-3.5" />} onClick={() => addRule()}>Add rule</Button>}
          {p && tab !== "ipo" && registry.data && (
            <Button icon={<Plus className="size-3.5" />} onClick={() => {
              const m = registry.data!.metrics.find((x) => x.kind === tab);
              if (m) addAlert(newAlertRule(tab, m, alertRules));
            }}>Add alert</Button>
          )}
        </>
      }
    />
  );

  if (!p) {
    return (
      <div>
        {header}
        {error ? <ErrorNote error={error} onRetry={reload} /> : (
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-[104px] rounded-xl" />)}</div>
            <div className="grid gap-4 lg:grid-cols-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-56 rounded-xl" />)}</div>
          </div>
        )}
      </div>
    );
  }

  function addRule(from?: Omit<Rule, "description"> & { title?: string; why?: string }) {
    if (!p) return;
    const r: Rule = from
      ? { id: uniqueId(p.rules, from.id), metric: from.metric, op: from.op, value: from.value, action: from.action, description: sentence(from) }
      : { id: uniqueId(p.rules), metric: "qib_times", op: "<", value: "1", action: "skip", description: "" };
    setDraft({ ...p, rules: [...p.rules, r] });
    setFresh(r.id);
    setTimeout(() => document.getElementById(`rule-${p.rules.length}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
  }
  const setRule = (i: number, patch: Partial<Rule>) =>
    setDraft({ ...p, rules: p.rules.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  const removeRule = (i: number) => setDraft({ ...p, rules: p.rules.filter((_, j) => j !== i) });
  function addAlert(r: AlertRule) {
    if (!p) return;
    setDraft({ ...p, alert_rules: [...alertRules, r] });
  }
  const setAlert = (i: number, r: AlertRule | null) =>
    setDraft({ ...p, alert_rules: r ? alertRules.map((x, j) => (j === i ? r : x)) : alertRules.filter((_, j) => j !== i) });

  const nProblems = Object.keys(problems).length + Object.keys(alertProblems).length;
  const invalid = nProblems ? `${nProblems} rule${nProblems > 1 ? "s need" : " needs"} fixing before you can save.` : null;

  const save = async () => {
    setSaving(true);
    try {
      // PUT replaces the whole profile: send the loaded profile with only the rules changed
      const body = {
        ...p,
        rules: p.rules.map((r) => ({ ...r, id: r.id.trim(), value: String(r.value).trim(), description: r.description.trim() || sentence(r) })),
        alert_rules: alertRules.map((r) => ({ ...r, id: r.id.trim(), value: String(r.value).trim(), instrument: r.instrument?.trim() || null, description: r.description.trim() })),
      };
      const saved = withDefaults(await api<Profile>("/api/profile", { method: "PUT", body: JSON.stringify(body) }));
      commit(saved);
      setSavedRules(saved.alert_rules ?? []);
      states.reload();
      setSaveError(null);
      setSavedAt(Date.now());
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const alertPanel = (kind: AlertKind) => registry.error ? <ErrorNote error={registry.error} onRetry={registry.reload} /> : !registry.data ? (
    <div className="grid gap-4 lg:grid-cols-2">{[0, 1].map((i) => <Skeleton key={i} className="h-72 rounded-xl" />)}</div>
  ) : (
    <AlertRulesPanel kind={kind} registry={registry.data} allRules={alertRules} states={states.data ?? []}
      rules={alertRules.map((rule, index) => ({ rule, index })).filter((x) => x.rule.kind === kind)}
      suggestions={suggestions[kind]} notifications={notifications.data} savedIds={savedAlertIds} problems={alertProblems}
      onChange={setAlert} onAdd={addAlert} />
  );
  const skip = p.rules.filter((r) => r.action === "skip").length;
  const have = (t: (typeof TEMPLATES)[number]) =>
    p.rules.some((r) => r.metric === t.metric && r.op === t.op && Number(r.value) === Number(t.value) && r.action === t.action);

  return (
    <div className="pb-4">
      {header}

      <div className="mb-6 overflow-x-auto">
        <Segmented value={tab} onChange={setTab} size="md"
          options={KINDS.map((k) => {
            const n = k === "ipo" ? p.rules.length + alertRules.filter((r) => r.kind === "ipo").length : alertRules.filter((r) => r.kind === k).length;
            return { value: k, label: <span className="inline-flex items-center gap-1.5">{KIND_LABEL[k]}{n > 0 && <span className="num rounded-full bg-background px-1.5 text-[10px] text-muted">{n}</span>}</span> };
          })} />
      </div>

      {tab === "ipo" ? (
        <>
        <div className="stagger mb-6 grid grid-cols-3 gap-3">
          <Stat label="Rules" value={p.rules.length} icon={<ListChecks className="size-4" />} hint="always checked" />
          <Stat label="Skip rules" value={skip} icon={<Ban className="size-4" />} tone="loss" hint="force SKIP"
            help="If a skip rule fires, the suggestion becomes SKIP with 0 lots, whatever the AI said. If its data is not available yet, the suggestion becomes conditional." />
          <Stat label="Warn rules" value={p.rules.length - skip} icon={<TriangleAlert className="size-4" />} tone="warn" hint="add a warning"
            help="A warn rule never changes the action; it shows a warning next to the suggestion when it fires." />
        </div>

        <HowRulesWork />

        <div className="mt-6 grid grid-cols-[minmax(0,1fr)] gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
          <div className="space-y-4">
            {!p.rules.length ? (
              <EmptyState icon={<ListChecks className="size-5" />} title="No rules yet"
                action={<Button icon={<Plus className="size-3.5" />} onClick={() => addRule(TEMPLATES[0])}>Add “skip if QIB is below 1x”</Button>}>
                Without rules, suggestions are only limited by your capital and category. Start with a template on the right, or add your own.
              </EmptyState>
            ) : (
              <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
                {p.rules.map((r, i) => (
                  <RuleCard key={i} index={i} rule={r} errors={problems[i]} fresh={fresh === r.id}
                    onChange={(patch) => setRule(i, patch)} onRemove={() => removeRule(i)} />
                ))}
              </div>
            )}
          </div>

          <div className="space-y-4">
            <Card icon={<Sparkles className="size-4" />} title="Templates" subtitle="Common rules, one click to add. Tweak the numbers after.">
              <ul className="space-y-2">
                {TEMPLATES.map((t) => {
                  const added = have(t);
                  return (
                    <li key={t.id}>
                      <button type="button" disabled={added} onClick={() => addRule(t)}
                        className={cx("group flex w-full items-start gap-3 rounded-lg border p-2.5 text-left transition",
                          added ? "border-border opacity-60" : "border-border hover:border-brand/50 hover:bg-brand-soft/40")}>
                        <span className={cx("mt-0.5 grid size-6 shrink-0 place-items-center rounded-md",
                          t.action === "skip" ? "bg-loss-soft text-loss" : "bg-warn-soft text-warn")}>
                          {t.action === "skip" ? <Ban className="size-3.5" /> : <TriangleAlert className="size-3.5" />}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm font-medium">{t.title}</span>
                          <span className="block text-xs text-muted">{t.why}</span>
                          <span className="mt-1 block text-[11px] font-medium text-brand">{sentence(t)}</span>
                        </span>
                        <span className="mt-0.5 shrink-0 text-muted transition group-hover:text-brand">
                          {added ? <Check className="size-4 text-gain" /> : <Plus className="size-4" />}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </Card>
            <Card icon={<UserRound className="size-4" />} title="Capital and category" subtitle="Lot limits come from your profile, not from rules.">
              <p className="text-sm text-muted">
                Suggestions never exceed the lots your capital per IPO buys, or your category’s SEBI limit. Change those on your{" "}
                <Link href="/profile" className="text-brand underline-offset-2 hover:underline">profile</Link>.
              </p>
            </Card>
          </div>
        </div>

          <section className="mt-10">
            <h2 className="mb-1 flex items-center gap-2 text-base font-semibold"><BellRing className="size-4 text-info" />Alerts for watched IPOs</h2>
            <p className="mb-4 text-sm text-muted">Notify-only: these never change a suggestion. Checked against the monitor’s subscription snapshots.</p>
            {alertPanel("ipo")}
          </section>
        </>
      ) : (
        <>
          <AlertKindStats kind={tab} rules={alertRules} states={states.data ?? []} />
          {alertPanel(tab)}
        </>
      )}

      <SaveBar dirty={dirty} saving={saving} error={saveError} invalid={invalid} savedAt={savedAt} onSave={save}
        onDiscard={() => { discard(); setSaveError(null); }} label="Your rule changes are not saved yet" />
    </div>
  );
}

function AlertKindStats({ kind, rules, states }: { kind: AlertKind; rules: AlertRule[]; states: AlertRuleState[] }) {
  const mine = rules.filter((r) => r.kind === kind);
  const ids = new Set(mine.map((r) => r.id));
  const firing = states.filter((s) => ids.has(s.rule_id) && s.status === "fired").length;
  const phone = mine.filter((r) => r.channels.length > 0).length;
  return (
    <div className="stagger mb-6 grid grid-cols-3 gap-3">
      <Stat label={`${{ ipo: "IPO", stock: "Stock", fund: "Fund", bond: "Bond", fno: "F&O", portfolio: "Portfolio" }[kind]} alerts`} value={mine.length} icon={<BellRing className="size-4" />} tone="info" hint={`${mine.filter((r) => r.enabled).length} on`} />
      <Stat label="Firing now" value={firing} icon={<Siren className="size-4" />} tone={firing ? "warn" : "neutral"} hint="condition true"
        help="Rules whose condition is true at the last check. Each fires once, then waits until it clears (and the cooldown passes) before it can alert again." />
      <Stat label="To your phone" value={phone} icon={<Smartphone className="size-4" />} tone="brand" hint="with a channel"
        help="Rules that send to ntfy, Telegram or the Mac besides the in-app bell." />
    </div>
  );
}

function HowRulesWork() {
  const steps = [
    { icon: <Cpu className="size-4" />, title: "The AI suggests", text: "Apply or skip, how many lots, and why, from the verified report." },
    { icon: <DatabaseZap className="size-4" />, title: "Python checks your rules", text: "Each rule is evaluated against live NSE/BSE data and the report, never by the AI." },
    { icon: <ShieldCheck className="size-4" />, title: "Your rules win", text: "A skip rule that fires forces SKIP; unknown data makes it conditional; warn rules add a warning." },
  ];
  return (
    <Card padded={false} className="overflow-hidden">
      <ol className="grid divide-y divide-border sm:grid-cols-3 sm:divide-x sm:divide-y-0">
        {steps.map((s, i) => (
          <li key={s.title} className="relative flex gap-3 p-4 sm:p-5">
            <span className="grid size-8 shrink-0 place-items-center rounded-full bg-gradient-to-br from-brand to-accent text-white shadow-glow">{s.icon}</span>
            <div>
              <p className="text-sm font-semibold"><span className="num mr-1 text-brand">{i + 1}.</span>{s.title}</p>
              <p className="mt-0.5 text-xs text-muted">{s.text}</p>
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function RuleCard({ index, rule: r, errors, fresh, onChange, onRemove }: {
  index: number; rule: Rule; errors?: string[]; fresh: boolean; onChange: (p: Partial<Rule>) => void; onRemove: () => void;
}) {
  const m = METRIC[r.metric];
  const value = String(r.value);
  return (
    <section id={`rule-${index}`}
      className={cx("rounded-xl border bg-card shadow-card transition duration-200 hover:border-border-strong",
        errors ? "border-loss/50" : "border-border", fresh && "animate-scale-in")}>
      <div className={cx("flex items-start gap-3 rounded-t-xl border-b border-border px-4 py-3",
        r.action === "skip" ? "bg-loss-soft/60" : "bg-warn-soft/60")}>
        <span className={cx("mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg", r.action === "skip" ? "bg-loss text-white" : "bg-warn text-white")}>
          {r.action === "skip" ? <Ban className="size-4" /> : <TriangleAlert className="size-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold leading-snug">{sentence({ ...r, value })}</p>
          {r.description && r.description !== sentence({ ...r, value }) && <p className="mt-0.5 truncate text-xs text-muted">{r.description}</p>}
        </div>
        <button type="button" aria-label={`Delete rule ${r.id}`} onClick={onRemove}
          className="grid size-8 shrink-0 place-items-center rounded-lg text-muted transition hover:bg-loss-soft hover:text-loss">
          <Trash2 className="size-4" />
        </button>
      </div>

      <div className="space-y-3 p-4">
        <Labelled label={<span className="inline-flex items-center gap-1">If this metric<InfoTip>{m?.description ?? "Pick what the rule looks at."}</InfoTip></span>}>
          <select aria-label="Metric" className={cx(inputClass, "w-full")} value={r.metric} onChange={(e) => onChange({ metric: e.target.value })}>
            {GROUPS.map((g) => (
              <optgroup key={g} label={g}>
                {METRICS.filter((x) => x.group === g).map((x) => <option key={x.key} value={x.key}>{x.label}</option>)}
              </optgroup>
            ))}
          </select>
          {m && <span className="block text-[11px] leading-snug text-muted">{m.description}</span>}
        </Labelled>
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
          <Field label="Comparison">
            <select className={cx(inputClass, "w-full")} value={r.op} onChange={(e) => onChange({ op: e.target.value })}>
              {OPS.map((o) => <option key={o.value} value={o.value}>{o.label} ({o.value})</option>)}
            </select>
          </Field>
          <Field label="Value">
            <div className="relative">
              <input className={cx(inputClass, "num w-full pr-14", !isNumber(value) && "border-loss focus:border-loss focus:ring-loss/20")}
                inputMode="decimal" value={value} aria-invalid={!isNumber(value)}
                onChange={(e) => onChange({ value: e.target.value.replace(/[,\s₹x]/gi, "") })} />
              {m && <span className="pointer-events-none absolute inset-y-0 right-3 grid place-items-center text-[11px] text-muted">{UNIT_SUFFIX[m.unit]}</span>}
            </div>
          </Field>
        </div>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <Labelled label="Then">
            <div><Segmented value={r.action} onChange={(v) => onChange({ action: v })}
              options={[{ value: "skip", label: "Skip the IPO" }, { value: "warn", label: "Just warn me" }]} /></div>
          </Labelled>
          <Badge tone={r.action === "skip" ? "loss" : "warn"}>{r.action === "skip" ? "forces SKIP" : "adds a warning"}</Badge>
        </div>
        <details className="group rounded-lg border border-border px-3 py-2 text-xs">
          <summary className="cursor-pointer list-none text-muted transition hover:text-foreground">
            <span className="inline-block transition group-open:rotate-90">›</span> Id and note <span className="num ml-1 text-foreground">{r.id || "—"}</span>
          </summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-[140px_minmax(0,1fr)]">
            <Field label="Rule id" hint="Unique, shown in suggestions.">
              <input className={cx(inputClass, "num w-full")} maxLength={40} value={r.id} onChange={(e) => onChange({ id: e.target.value })} />
            </Field>
            <Field label="Note (optional)" hint="Shown with the suggestion when the rule fires; left blank, the sentence above is used.">
              <input className={cx(inputClass, "w-full")} value={r.description} placeholder={sentence({ ...r, value })}
                onChange={(e) => onChange({ description: e.target.value })} />
            </Field>
          </div>
        </details>
        {errors && (
          <ul role="alert" className="space-y-0.5 rounded-lg bg-loss-soft px-3 py-2 text-xs text-loss">
            {errors.map((e) => <li key={e}>{e}</li>)}
          </ul>
        )}
      </div>
    </section>
  );
}
