"use client";

import { useEffect, useState } from "react";

import { Button, Card, ErrorNote } from "@/components/ui";
import { api, type Profile, type Rule, useApi } from "@/lib/api";

const METRICS = ["qib_times", "nii_times", "rii_times", "total_times", "price_band_upper", "lot_size", "lot_cost",
  "max_lots_by_capital", "bidding_days_left", "gate_ok"];
const OPS = ["<", "<=", ">", ">=", "==", "!="];

const field = "rounded border border-border bg-background px-2 py-1 text-sm";

export default function ProfilePage() {
  const { data, error } = useApi<Profile>("/api/profile");
  const [p, setP] = useState<Profile | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- seed the editable copy once the profile loads
  useEffect(() => { if (data && !p) setP(data); }, [data, p]);

  if (!p) return <ErrorNote error={error} />;
  const set = <K extends keyof Profile>(k: K, v: Profile[K]) => setP({ ...p, [k]: v });
  const setRule = (i: number, r: Partial<Rule>) => set("rules", p.rules.map((x, j) => (j === i ? { ...x, ...r } : x)));

  const save = async () => {
    try {
      setP(await api<Profile>("/api/profile", { method: "PUT", body: JSON.stringify(p) }));
      setSaved(new Date().toLocaleTimeString());
      setSaveError(null);
    } catch (e) {
      setSaveError((e as Error).message);
    }
  };

  return (
    <div className="space-y-4">
      <Card title="Investor profile" actions={<Button onClick={save}>Save</Button>}>
        <ErrorNote error={saveError} />
        {saved && <p className="mb-2 text-xs text-emerald-600">Saved at {saved}</p>}
        <div className="grid gap-3 text-sm md:grid-cols-3">
          <label className="flex flex-col gap-1">
            Capital per IPO (₹)
            <input className={field} inputMode="decimal" value={p.capital_per_ipo_inr} onChange={(e) => set("capital_per_ipo_inr", e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            Category
            <select className={field} value={p.category} onChange={(e) => set("category", e.target.value as Profile["category"])}>
              <option value="retail">Retail (up to ₹2 lakh)</option>
              <option value="shni">Small NII (₹2–10 lakh)</option>
              <option value="bhni">Big NII (above ₹10 lakh)</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            Horizon
            <select className={field} value={p.horizon} onChange={(e) => set("horizon", e.target.value as Profile["horizon"])}>
              <option value="listing">Listing gains</option>
              <option value="short">Short term (months)</option>
              <option value="long">Long term (years)</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            Risk appetite
            <select className={field} value={p.risk_appetite} onChange={(e) => set("risk_appetite", e.target.value as Profile["risk_appetite"])}>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            Tax slab (%)
            <input className={field} inputMode="decimal" value={p.tax_slab_pct} onChange={(e) => set("tax_slab_pct", e.target.value)} />
          </label>
          <label className="flex flex-col gap-1 md:col-span-3">
            Notes for the advisor
            <textarea className={field} rows={2} value={p.notes} onChange={(e) => set("notes", e.target.value)} />
          </label>
        </div>
      </Card>

      <Card
        title="Personal rules (checked by Python against live data, never by the model)"
        actions={
          <Button onClick={() => set("rules", [...p.rules, { id: `rule-${p.rules.length + 1}`, description: "", metric: "qib_times", op: "<", value: "1", action: "skip" }])}>
            Add rule
          </Button>
        }
      >
        <table className="w-full text-sm">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-1">Id</th>
              <th>If</th>
              <th />
              <th />
              <th>Then</th>
              <th>Description</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {p.rules.map((r, i) => (
              <tr key={i} className="border-t border-border">
                <td className="py-1">
                  <input className={`${field} w-28`} value={r.id} onChange={(e) => setRule(i, { id: e.target.value })} />
                </td>
                <td>
                  <select className={field} value={r.metric} onChange={(e) => setRule(i, { metric: e.target.value })}>
                    {METRICS.map((m) => <option key={m}>{m}</option>)}
                  </select>
                </td>
                <td>
                  <select className={field} value={r.op} onChange={(e) => setRule(i, { op: e.target.value })}>
                    {OPS.map((o) => <option key={o}>{o}</option>)}
                  </select>
                </td>
                <td>
                  <input className={`${field} w-20`} inputMode="decimal" value={r.value} onChange={(e) => setRule(i, { value: e.target.value })} />
                </td>
                <td>
                  <select className={field} value={r.action} onChange={(e) => setRule(i, { action: e.target.value as Rule["action"] })}>
                    <option value="skip">skip</option>
                    <option value="warn">warn</option>
                  </select>
                </td>
                <td>
                  <input className={`${field} w-full`} value={r.description} onChange={(e) => setRule(i, { description: e.target.value })} />
                </td>
                <td className="text-right">
                  <button type="button" className="text-rose-600" onClick={() => set("rules", p.rules.filter((_, j) => j !== i))}>
                    remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
