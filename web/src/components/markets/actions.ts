// Plan-consuming and state-changing actions shared by the list and detail pages (always behind a confirmation).

import { api } from "@/lib/api";

/** The company slug for a stock key: an NSE symbol (added from NSE's equity list the first time) or "BSE:<code>" for a
 * BSE-only stock (added from BSE's scrip master). */
export const ensureStock = async (key: string) =>
  (await api<{ slug: string }>("/api/companies", {
    method: "POST",
    body: JSON.stringify(key.toUpperCase().startsWith("BSE:") ? { bse_code: key.slice(4) } : { nse_symbol: key }),
  })).slug;

export const ensureFund = async (schemeCode: string) =>
  (await api<{ slug: string }>("/api/funds", { method: "POST", body: JSON.stringify({ scheme_code: schemeCode }) })).slug;

export const ensureBond = async (isin: string) =>
  (await api<{ slug: string }>("/api/bonds", { method: "POST", body: JSON.stringify({ isin }) })).slug;

/** Starts a research run and returns its id. Callers confirm first: a run uses the Claude plan window. */
export const startResearch = async (slug: string, kind: "stock_report" | "fund_report" | "bond_report") =>
  (await api<{ run_id: number }>("/api/runs", { method: "POST", body: JSON.stringify({ company: slug, kind }) })).run_id;

export const watchStock = async (key: string) => {
  const slug = await ensureStock(key);
  await api("/api/watches", { method: "POST", body: JSON.stringify({ company: slug, kind: "stock" }) });
  return slug;
};
