// Rule vocabulary, mirroring finresearch.suggest.profile (Metric, Op) and how suggest/rules.py evaluates them.
import type { Rule } from "@/lib/api";

export type MetricInfo = {
  key: string;
  label: string;
  /** noun phrase for the sentence preview: "Skip if <phrase> is below 1x" */
  phrase: string;
  description: string;
  unit: "times" | "inr" | "shares" | "lots" | "days" | "flag";
  group: "Demand" | "Price and lots" | "Timing and quality";
};

export const METRICS: MetricInfo[] = [
  { key: "qib_times", label: "QIB subscription", phrase: "QIB subscription", unit: "times", group: "Demand",
    description: "How many times the qualified institutional buyers' portion (mutual funds, banks, insurers, FPIs; excluding anchors) is subscribed, combined NSE + BSE. Often read as the 'smart money' signal." },
  { key: "nii_times", label: "NII subscription", phrase: "NII (HNI) subscription", unit: "times", group: "Demand",
    description: "Subscription of the non-institutional (HNI) portion: individuals and firms bidding above ₹2 lakh." },
  { key: "rii_times", label: "Retail subscription", phrase: "retail (RII) subscription", unit: "times", group: "Demand",
    description: "Subscription of the retail portion (bids up to ₹2 lakh). Very high numbers mean low allotment odds." },
  { key: "total_times", label: "Total subscription", phrase: "total subscription", unit: "times", group: "Demand",
    description: "Subscription of the whole issue across all categories." },
  { key: "price_band_upper", label: "Upper price band", phrase: "the upper price band", unit: "inr", group: "Price and lots",
    description: "The top of the price band, in ₹ per share. Bids at cut-off pay this price." },
  { key: "lot_size", label: "Lot size", phrase: "the lot size", unit: "shares", group: "Price and lots",
    description: "Shares in one lot (the minimum bid). You bid in multiples of it." },
  { key: "lot_cost", label: "Cost of one lot", phrase: "one lot's cost", unit: "inr", group: "Price and lots",
    description: "Lot size × upper price band, in ₹: the money one lot blocks in your bank account." },
  { key: "max_lots_by_capital", label: "Lots I can afford", phrase: "the lots my capital buys", unit: "lots", group: "Price and lots",
    description: "How many lots your 'capital per IPO' (Profile) buys at the upper price band." },
  { key: "bidding_days_left", label: "Bidding days left", phrase: "bidding days left", unit: "days", group: "Timing and quality",
    description: "Exchange working days from today to the issue close, both included (1 on the last day, 0 once closed)." },
  { key: "gate_ok", label: "Report passed the gate", phrase: "the verification gate result", unit: "flag", group: "Timing and quality",
    description: "1 when the research report passed FinResearch's deterministic verification gate, 0 when it did not." },
];

export const METRIC = Object.fromEntries(METRICS.map((m) => [m.key, m])) as Record<string, MetricInfo>;

export const OPS: { value: string; label: string; phrase: string }[] = [
  { value: "<", label: "is below", phrase: "is below" },
  { value: "<=", label: "is at most", phrase: "is at most" },
  { value: ">", label: "is above", phrase: "is above" },
  { value: ">=", label: "is at least", phrase: "is at least" },
  { value: "==", label: "equals", phrase: "equals" },
  { value: "!=", label: "is not", phrase: "is not" },
];
const OP = Object.fromEntries(OPS.map((o) => [o.value, o]));

export const UNIT_SUFFIX: Record<MetricInfo["unit"], string> = {
  times: "x", inr: "₹", shares: "shares", lots: "lots", days: "days", flag: "0 or 1",
};

export function formatValue(metric: string, value: string) {
  const m = METRIC[metric];
  const n = Number(value);
  if (!m || value.trim() === "" || !Number.isFinite(n)) return value || "…";
  switch (m.unit) {
    case "times": return `${n}x`;
    case "inr": return `₹${n.toLocaleString("en-IN")}`;
    case "shares": return `${n.toLocaleString("en-IN")} ${n === 1 ? "share" : "shares"}`;
    case "lots": return `${n} ${n === 1 ? "lot" : "lots"}`;
    case "days": return `${n} ${n === 1 ? "day" : "days"}`;
    default: return String(n);
  }
}

/** A plain-English reading of a rule: "Skip if QIB subscription is below 1x". */
export function sentence(r: Pick<Rule, "metric" | "op" | "value" | "action">) {
  const verb = r.action === "skip" ? "Skip" : "Warn";
  if (r.metric === "gate_ok") {
    const failsWhen = (r.op === "<" && Number(r.value) === 1) || (r.op === "==" && Number(r.value) === 0) || (r.op === "!=" && Number(r.value) === 1);
    if (failsWhen) return `${verb} if the report did not pass the verification gate`;
  }
  const m = METRIC[r.metric];
  return `${verb} if ${m?.phrase ?? r.metric} ${OP[r.op]?.phrase ?? r.op} ${formatValue(r.metric, r.value)}`;
}

export const isNumber = (v: string) => /^-?\d+(\.\d+)?$/.test(v.trim());

export type Template = Omit<Rule, "description"> & { title: string; why: string };

export const TEMPLATES: Template[] = [
  { id: "qib-floor", title: "Institutions must show up", metric: "qib_times", op: "<", value: "1", action: "skip",
    why: "Skip issues the big institutions don't fully subscribe." },
  { id: "gate", title: "Only verified reports", metric: "gate_ok", op: "<", value: "1", action: "skip",
    why: "Never act on a report that failed the verification gate." },
  { id: "one-lot", title: "Must afford one lot", metric: "max_lots_by_capital", op: "<", value: "1", action: "skip",
    why: "Skip when one lot costs more than your capital per IPO." },
  { id: "weak-demand", title: "Weak overall demand", metric: "total_times", op: "<", value: "2", action: "warn",
    why: "Warn when the whole issue is less than 2x subscribed." },
  { id: "retail-frenzy", title: "Retail frenzy", metric: "rii_times", op: ">", value: "50", action: "warn",
    why: "Warn when retail is over 50x: allotment odds are slim." },
  { id: "pricey-lot", title: "Expensive lot", metric: "lot_cost", op: ">", value: "15000", action: "warn",
    why: "Warn when one lot blocks more than ₹15,000." },
  { id: "last-day", title: "Last bidding day", metric: "bidding_days_left", op: "<=", value: "1", action: "warn",
    why: "Warn on the final day so you approve the UPI mandate in time." },
  { id: "nii-cold", title: "HNIs stay away", metric: "nii_times", op: "<", value: "1", action: "warn",
    why: "Warn when the NII (HNI) book is under-subscribed." },
];

/** A unique id: the template id, or rule-N one past the highest existing one. Deleting a rule never causes a repeat. */
export function uniqueId(rules: { id: string }[], base?: string) {
  const ids = new Set(rules.map((r) => r.id));
  if (base && !ids.has(base)) return base;
  if (base) {
    let k = 2;
    while (ids.has(`${base}-${k}`)) k += 1;
    return `${base}-${k}`;
  }
  let n = Math.max(0, ...rules.map((r) => Number(/^rule-(\d+)$/.exec(r.id)?.[1] ?? 0))) + 1;
  while (ids.has(`rule-${n}`)) n += 1;
  return `rule-${n}`;
}

/** Problems per rule index (empty when the rule set can be saved). */
export function validate(rules: Rule[]): Record<number, string[]> {
  const out: Record<number, string[]> = {};
  const seen = new Map<string, number>();
  rules.forEach((r, i) => {
    const errs: string[] = [];
    const id = r.id.trim();
    if (!id) errs.push("Give the rule an id.");
    else if (id.length > 40) errs.push("The id can be at most 40 characters.");
    else if (seen.has(id)) errs.push(`The id “${id}” is already used by rule ${seen.get(id)! + 1}.`);
    if (id) seen.set(id, seen.has(id) ? seen.get(id)! : i);
    if (!METRIC[r.metric]) errs.push("Pick a metric.");
    if (!isNumber(String(r.value))) errs.push("The value must be a number.");
    if (errs.length) out[i] = errs;
  });
  return out;
}
