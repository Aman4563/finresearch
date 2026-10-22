// The F&O monthly review (#383): unknown shortfall and unknown explain days read "unknown" (never ₹0 or 0%), the
// deferred behavioural metrics say how many closed trades they wait for. Made-up trades; no API or broker involved.
import { type Page, expect, test } from "@playwright/test";

import type { FnoReview } from "../src/components/fno/review-types";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

const REVIEW: FnoReview = {
  month: "2026-10", start: "2026-10-01", end: "2026-11-01",
  adherence: { closed: 2, counts: { planned_target: 1, no_plan: 1 }, rate: 1, with_stop: 0, beyond_stop_count: 0, mean_beyond_stop: null, window_hours: 24 },
  shortfall: {
    trades: [
      { paper_id: 7, card_log_id: null, price: null, fees: null, total: null, base: null, pct: null, why: "not taken from a card" },
      { paper_id: 8, card_log_id: 41, price: 240, fees: 58.42, total: 298.42, base: 5137.5, pct: 5.809, why: null },
    ], known: 1, mean_total: 298.42, mean_pct: 5.809,
  },
  mix: { trades: 2, premium_sold_share: 0.29, same_day_expiry_share: 0, mean_hold_share: 0.0238 },
  explain: {
    days: [
      { position: "paper:8", day: "2026-10-21", previous: "2026-10-20", actual: 750, max_loss: -5377.5, within: true, why: null,
        greeks: { delta: 700, gamma: 20, vega: 60, theta: -40, explained: 740, residual: 10, unexplained: 0.0133 },
        revaluation: { spot: 715, vol: 61, time: -28, model: 748, residual: 2 } },
      { position: "paper:8", day: "2026-10-22", previous: "2026-10-21", actual: null, max_loss: null, within: null,
        why: "a leg's price is unknown on one of the two days", greeks: null, revaluation: null },
    ],
    summary: { position_days: 2, unknown: 1, decided: 1, within: 1, share_within: 1, target_share: 0.8, meets_target: true },
  },
  deferred: { closed_trades: 3, starts_at: 100, status: "deferred" },
  notes: ["Paper trades only for shortfall and adherence."],
};

test("fno review: unknowns read unknown, deferred metrics say why", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/review": REVIEW });
  await page.goto("/fno/review");
  await expect(page.getByRole("heading", { name: "F&O monthly review" })).toBeVisible();
  await expect(page.getByText(/unknown \(not taken from a card\)/)).toBeVisible();
  await expect(page.getByText(/unknown \(a leg's price is unknown/)).toBeVisible();
  await expect(page.getByText(/known for 1 of 2 entries/)).toBeVisible();
  await expect(page.getByText(/deferred until 100 closed trades \(3 so far\)/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});
