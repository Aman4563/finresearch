// Plan-consuming and state-changing actions shared by the list and detail pages (always behind a confirmation).

import { api } from "@/lib/api";

/** The company slug for an NSE symbol, adding the company from NSE's equity list the first time. */
export const ensureStock = async (symbol: string) =>
  (await api<{ slug: string }>("/api/companies", { method: "POST", body: JSON.stringify({ nse_symbol: symbol }) })).slug;

export const ensureFund = async (schemeCode: string) =>
  (await api<{ slug: string }>("/api/funds", { method: "POST", body: JSON.stringify({ scheme_code: schemeCode }) })).slug;

export const ensureBond = async (isin: string) =>
  (await api<{ slug: string }>("/api/bonds", { method: "POST", body: JSON.stringify({ isin }) })).slug;

/** Starts a research run and returns its id. Callers confirm first: a run uses the Claude plan window. */
export const startResearch = async (slug: string, kind: "stock_report" | "fund_report" | "bond_report") =>
  (await api<{ run_id: number }>("/api/runs", { method: "POST", body: JSON.stringify({ company: slug, kind }) })).run_id;

export const watchStock = async (symbol: string) => {
  const slug = await ensureStock(symbol);
  await api("/api/watches", { method: "POST", body: JSON.stringify({ company: slug, kind: "stock" }) });
  return slug;
};
