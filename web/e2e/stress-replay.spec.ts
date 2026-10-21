// Historical stress replay on the Risk tab (#345): loaded only on request, every figure from the fixture, and an
// unmapped holding listed with its value and reason, never as a 0 % move. Synthetic names and values only.
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
const REPLAY = {
  available: true, as_of: "2026-10-20", method: "Today's holdings replayed day by day (e2e).",
  scenarios: [
    { id: "gfc2008", label: "Global financial crisis (Lehman), Sep-Nov 2008", note: "e2e", window: ["2008-09-01", "2008-11-30"],
      available: false, reason: "Nifty 50 in index_eod: no close before 01-Sep-2008; NIFTYBEES: no close", unmapped: [] },
    { id: "election2024", label: "Election-result day, 3-5 Jun 2024", note: "Result-day fall.", window: ["2024-06-03", "2024-06-05"],
      available: true, base: "2024-05-31", index_source: "Nifty 50 close (NSE index file, local index_eod)",
      index: { move: 0.01, drawdown: -0.0583 }, mapped_value: 3000, unmapped_value: 500, mapped_share: 0.8571,
      loss: { inr: -92.5, pct: -0.0308, day: "2024-06-05" }, worst_day: { date: "2024-06-04", pct: -0.1112, inr: -346.99 },
      drawdown: { peak: "2024-06-03", trough: "2024-06-04", pct: -0.1112, inr: -346.99 },
      recovery: { date: null, days: null, searched_to: "2025-06-05", note: "not back at the 03-Jun-2024 peak by 05-Jun-2025" },
      contributors: [{ key: "NSE:BBB", name: "Example Beta Ltd", inr: -246.99, share: 0.7118 }],
      positions: [{ key: "NSE:BBB", name: "Example Beta Ltd", value: 2000, how: "beta 2.00 × Nifty 50", move: -0.03, drawdown_inr: -246.99 },
        { key: "NSE:AAA", name: "Example Own Ltd", value: 1000, how: "own prices", move: 0, drawdown_inr: -100 }],
      unmapped: [{ key: "MF:9", name: "Example Liquid Fund", value: 500, reason: "no own NAVs for the window; not equity, so no beta mapping" }],
      vix: { start: 16, peak: 26, peak_date: "2024-06-04" } },
  ],
};

test("risk: the stress replay runs on request and lists unmapped holdings with their reason (#345)", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/connections/summary": [],
    "GET /api/portfolio/analytics/risk": RISK,
    "GET /api/portfolio/analytics/stress-replay": REPLAY,
  });
  await page.goto("/portfolio#risk");
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: /Historical stress replay/ }) });
  await expect(card.getByRole("button", { name: "Run the replay" })).toBeVisible();
  expect(api.count("GET /api/portfolio/analytics/stress-replay")).toBe(0); // nothing is fetched until asked
  await card.getByRole("button", { name: "Run the replay" }).click();
  await expect(card.getByText(/peak to trough −11\.1%|peak to trough -11\.1%/)).toBeVisible();
  await expect(card.getByText(/1 unmapped/)).toBeVisible();
  await card.getByRole("button", { name: /Election-result day/ }).click();
  await expect(card.getByText(/To the window's end \(5 Jun 2024\): -3\.1% \(.₹93\) on the ₹3,000 that could be mapped, 85\.7% of today's holdings/)).toBeVisible();
  await expect(card.getByText(/Unmapped: not in the figures above/)).toBeVisible();
  await expect(card.getByText(/Example Liquid Fund/)).toBeVisible();
  await expect(card.getByText(/not back at the 03-Jun-2024 peak/)).toBeVisible();
  await card.getByRole("button", { name: /Global financial crisis/ }).click();
  await expect(card.getByText(/no close before 01-Sep-2008/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
    "GET /api/disclosures/tracked", "GET /api/portfolio/ais", "GET /api/portfolio/tax"]);
});
