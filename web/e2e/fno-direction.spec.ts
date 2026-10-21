// The direction panel on /fno (#327): with no accepted model it shows the market-implied probabilities labelled
// "no edge", NO TRADE with each candidate's EV, the out-of-sample table and calibration chart, and logs a forecast on
// demand; switching the horizon refetches. Made-up numbers; no API, database or broker.
import { type Page, expect, test } from "@playwright/test";

import type { Chain } from "../src/components/fno/model";
import { DIRECTION } from "./fixtures-fno";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const q = (p: string) => ({ last_price: p, iv: "14.0", oi: "5000", change_in_oi: "10", volume: "1000", bid: p, ask: p });
const CHAIN: Chain = {
  symbol: "NIFTY", expiry: "2026-10-20", underlying: "25000", as_of: "2026-10-13T09:30:00+05:30", lot_size: 65, atm_strike: "25000",
  atm_iv: { call: "14.0", put: "14.5" }, pcr_oi: "1.10", max_pain: "25000", source_label: "NSE",
  rows: [24800, 24900, 25000, 25100, 25200].map((k) => ({ strike: String(k), call: q("100"), put: q("90") })),
};
const STATE = DIRECTION;
const ROUTES = {
  // a week ahead of today: the page drops expiries before today, so fixed dates went stale after 20-Oct-2026
  "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: [new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10)], strikes: [] },
  "GET /api/fno/NIFTY/chain": CHAIN,
  "GET /api/fno/risk-notice": { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" },
  "GET /api/fno/direction/NIFTY": STATE(5),
  "POST /api/fno/direction/NIFTY/log": { logged: [{ id: 1 }, { id: 2 }], skipped: null, as_of: "2026-10-12" },
};
const OPTIONAL = ["GET /api/market/status", "GET /api/fno/positions", "GET /api/fno/paper", "GET /api/fno/NIFTY/events", "GET /api/fno/NIFTY/iv", "GET /api/fno/NIFTY/context"];

test("fno: the direction panel says no edge, NO TRADE, shows out-of-sample accuracy and logs a forecast", async ({ page }) => {
  const api = await mockApi(page, ROUTES);
  await page.goto("/fno");
  const panel = page.locator("section").filter({ has: page.getByRole("heading", { name: "CALL or PUT?" }) });
  await expect(panel.getByText("no edge: showing market-implied probabilities")).toBeVisible();
  await expect(panel.getByText("NO TRADE: no measured edge")).toBeVisible();
  await expect(panel.getByText("Long put (ATM)")).toBeVisible();
  // #356 (c): a positive EV from the market's own probabilities is a pricing residual, never coloured as a gain
  const residual = panel.getByRole("row").filter({ hasText: "Bull put credit spread" });
  await expect(residual.getByRole("cell", { name: "₹212", exact: true }).first()).not.toHaveClass(/text-gain/);
  await expect(residual.getByText("incl. pricing residual ₹332 (not edge)")).toBeVisible();
  await expect(panel.getByRole("img", { name: /Calibration: Logistic and Market-implied/ })).toBeVisible();
  await expect(panel.getByText(/Walk-forward from 2024-07-01/)).toBeVisible();
  // #354 h: a live hit rate carries its Wilson 95% CI; the registration "matches spec", not "code"
  await expect(panel.getByText("50% (95% CI 9–91%)")).toBeVisible();
  await expect(panel.getByText(/matches spec/)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Best non-directional plan" })).toBeVisible();
  await panel.getByRole("button", { name: "Log today's forecast" }).click();
  await expect(panel.getByText("Logged 2 forecasts from the 2026-10-12 close.")).toBeVisible();
  expect(api.count("POST /api/fno/direction/NIFTY/log")).toBe(1);
  await panel.getByRole("tab", { name: "20d" }).click();
  await expect.poll(() => api.count("GET /api/fno/direction/NIFTY")).toBeGreaterThan(1);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(OPTIONAL);
});
