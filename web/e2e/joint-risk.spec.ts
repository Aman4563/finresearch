// One risk view across both books on the Risk tab (#397): loaded only on request, D_total and L(m) shown per book
// and together, an unknown part named (never 0), the hedge trigger stated, and the pre-trade check's change.
// Synthetic only.
import { type Page, expect, test } from "@playwright/test";

import { SNAPSHOT } from "./fixtures";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const m = (reason: string) => ({ value: null, unit: "fraction", reason, n: 10, note: null, how: null });
const RISK = {
  disclaimer: "Personal analysis, not advice.", privacy: "Local only.", excluded: [], warnings: [], notes: [], start_reason: "",
  reason: null, as_of: "2026-10-20", start: "2026-10-01", available: true, value: 3500,
  risk_free: { rate: 0.06, label: "e2e", as_of: null, source: null, fallback: false, user_set: false },
  realised: {
    window: { start: "2026-10-01", end: "2026-10-20", returns: 10 },
    volatility: m("needs 60 daily returns"), beta: m("needs 120"), tracking_error: m("needs 120"), sharpe: m("needs 250"),
    sortino: m("needs 250"), var_1d: m("needs 250"), cvar_1d: m("needs 250"), var_21d: m("needs 250"), cvar_21d: m("needs 250"),
    max_drawdown: { ...m("e2e"), peak: "2026-10-01", trough: "2026-10-01", recovered: null, days_to_recover: null, underwater_days: 0, current: 0 },
  },
  hypothetical: { note: "e2e", reason: "needs 120 common days", positions: [], sectors: [] },
  stress: [],
};
const delta = (h: number | null, f: number | null, reasons: string[] = []) => {
  const t = h == null || f == null ? null : h + f;
  return { holdings: h, fno: f, total: t, complete: t != null, reasons,
    per_pct: { holdings: h == null ? null : h / 100, fno: f == null ? null : f / 100, total: t == null ? null : t / 100 } };
};
const loss = (move: number, h: number | null, f: number | null, reasons: string[] = []) => {
  const t = h == null || f == null ? null : h + f;
  return { move, holdings: h, fno: f, total: t, reasons, fno_share: t != null && t < 0 ? f! / t : null, share_reason: null };
};
const JOINT = {
  as_of: "2026-10-22T11:00:00+05:30", assume_beta1: false, disclaimer: "Personal analysis, not advice.", privacy: "Local only (e2e).",
  method: "D_total = e2e.", beta_note: "Holdings use NIFTYBEES betas (e2e).", notes: ["Groww is not connected: only paper positions are counted."],
  holdings: [{ key: "NSE:AAA", name: "Example Alpha Ltd", value: 1000000, beta: 1.2, how: "beta 1.20 (250 days)" }],
  holdings_reason: null, book: { known: true, legs: 1, reason: null },
  d_total: delta(1200000, 600000),
  losses: [loss(-0.15, -180000, -150000), loss(-0.1, -120000, -90000), loss(0.05, 60000, 20000)],
  hedge_trigger: { share: 90000 / 210000, fired: true, threshold: 0.25, reasons: [], note: "The costed hedge calculator is built only once F&O is at least 25 %." },
  replays: [{ day: "2024-06-04", label: "4-Jun-2024 election-result day", nifty_move: -0.0593, iv30_change: 4.46, holdings: null, fno: -70000, total: null,
    reasons: ["Example Fund: no price path that day (no own prices)"], notes: [] }],
  forward_test: { qualifying_days: 2, needed: 20, status: "not evaluable yet: 2 of 20 qualifying days", since: "2026-10-12" },
};
const UNKNOWN = { ...JOINT, d_total: delta(null, 600000, ["Example New Listing Ltd: no beta (under a year of prices, needs 120 days for a beta)"]),
  losses: [loss(-0.1, null, -90000, ["Example New Listing Ltd: no beta (under a year of prices, needs 120 days for a beta)"])],
  hedge_trigger: { ...JOINT.hedge_trigger, share: null, fired: null } };
const PRE = {
  as_of: "2026-10-22T11:00:00+05:30", notes: [], move: -0.1,
  d_before: delta(1200000, 600000), d_after: delta(1200000, 900000), l_before: loss(-0.1, -120000, -90000), l_after: loss(-0.1, -120000, -140000),
  d_change: 300000, l_change: -50000, trade_delta: 300000, trade_l: -50000, trade_reasons: [],
};
const IGNORE = ["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
  "GET /api/disclosures/tracked", "GET /api/portfolio/ais", "GET /api/portfolio/tax"];

async function open(page: Page, joint: unknown) {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/connections/summary": [],
    "GET /api/portfolio/analytics/risk": RISK,
    "GET /api/portfolio/analytics/joint-risk": joint,
    "POST /api/portfolio/analytics/joint-risk/pretrade": PRE,
  });
  await page.goto("/portfolio#risk");
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: /^Both books together/ }) });
  return { api, card };
}

test("risk: both books together, on request, with F&O's share and the hedge trigger (#397)", async ({ page }) => {
  const { api, card } = await open(page, JOINT);
  await expect(card.getByRole("button", { name: "Combine both books" })).toBeVisible();
  expect(api.count("GET /api/portfolio/analytics/joint-risk")).toBe(0); // nothing is fetched until asked
  await card.getByRole("button", { name: "Combine both books" }).click();
  const d = card.getByRole("region", { name: "NIFTY-equivalent delta" });
  await expect(d.getByRole("row", { name: /Both \(D_total\) \+₹18,000/ })).toBeVisible();
  const l = card.getByRole("region", { name: "Joint stress L(m)" });
  await expect(l.getByRole("row", { name: /-10% −?-?₹1,20,000 −?-?₹90,000 −?-?₹2,10,000 42\.9%/ })).toBeVisible();
  await expect(card.getByText(/F&O ≥ 25% of the −10 % loss/)).toBeVisible();
  await expect(card.getByText(/not evaluable yet: 2 of 20 qualifying days/)).toBeVisible();
  const r = card.getByRole("region", { name: "Historical days on both books" });
  await expect(r.getByRole("row", { name: /election-result day .* unknown/ })).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(IGNORE);
});

test("risk: a holding without a beta leaves both books' figure unknown and named, never 0 (#397)", async ({ page }) => {
  const { api, card } = await open(page, UNKNOWN);
  await card.getByRole("button", { name: "Combine both books" }).click();
  const d = card.getByRole("region", { name: "NIFTY-equivalent delta" });
  await expect(d.getByRole("row", { name: /Both \(D_total\) unknown/ })).toBeVisible();
  await expect(card.getByRole("list", { name: "What is missing" }).getByText(/Example New Listing Ltd: no beta/)).toBeVisible();
  await expect(card.getByText(/hedge trigger unknown/)).toBeVisible();
  api.expectClean(IGNORE);
});

test("risk: the pre-trade check shows how D_total and L(-10%) change (#397)", async ({ page }) => {
  const { api, card } = await open(page, JOINT);
  await card.getByRole("button", { name: "Combine both books" }).click();
  await card.getByLabel("Expiry").fill("2026-11-24");
  await card.getByLabel("Strike").fill("24000");
  await card.getByLabel("Units (− = sell)").fill("-75");
  await card.getByRole("button", { name: "Check the change" }).click();
  const t = card.getByRole("region", { name: "Pre-trade change" });
  await expect(t.getByRole("row", { name: /D_total, ₹ per 1 % NIFTY move \+₹18,000 \+₹21,000 \+₹3,000/ })).toBeVisible();
  await expect(t.getByRole("row", { name: /L\(−10 %\) .*₹2,10,000 .*₹2,60,000 .*₹50,000/ })).toBeVisible();
  expect(api.count("POST /api/portfolio/analytics/joint-risk/pretrade")).toBe(1);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(IGNORE);
});
