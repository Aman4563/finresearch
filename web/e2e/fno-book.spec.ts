// The F&O paper book (#308): an unmarked open trade reads "unknown / not marked yet" (never ₹0), a trade waiting for
// its settlement price says so, and the results count every closed trade. Made-up trades; no API or broker involved.
import { type Page, expect, test } from "@playwright/test";

import type { PaperBook, PaperTrade } from "../src/components/fno/book-types";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

const leg = (id: number, side: "buy" | "sell", strike: number, entry: number) => ({
  id, right: "call" as const, strike, side, lots: 1, qty: 75, ref_price: entry, entry_price: entry,
  trading_symbol: `NIFTY26OCT${strike}CE`, last_price: null, last_price_at: null, exit_price: null,
});
const base: PaperTrade = {
  id: 1, symbol: "NIFTY", expiry: "2026-10-27", label: "e2e spread", status: "open", opened_at: "2026-10-12T04:30:00+00:00",
  days_to_expiry: 15, lot_size: 75, slippage_pct: 1, brokerage_per_order: 20, spot_at_entry: 24950,
  price_source: "Groww LTP", net_premium: 5377.5, entry_costs: 58.42, mtm_gross: null, mtm_at: null, mtm_source: null,
  mtm_net_if_closed: null, closed_at: null, close_reason: null, exit_costs: null, exit_detail: {}, settlement_price: null,
  settlement_source: null, gross_pnl: null, net_pnl: null, note: null, legs: [leg(1, "buy", 25000, 121.2), leg(2, "sell", 25200, 49.5)],
};
const BOOK: PaperBook = {
  trades: [
    base,
    { ...base, id: 2, status: "awaiting_settlement", expiry: "2026-10-06", days_to_expiry: -6,
      note: "waiting for NSE's close of NIFTY on 06-Oct-2026 to settle (checked 07-Oct 03:30 UTC)" },
    { ...base, id: 3, status: "closed", closed_at: "2026-10-09T09:00:00+00:00", close_reason: "square_off", exit_costs: 67.95,
      gross_pnl: -127.5, net_pnl: -253.87 },
  ],
  counts: { open: 1, awaiting_settlement: 1, closed: 1, all: 3 },
  results: { closed: 1, wins: 0, losses: 1, win_rate: 0, expectancy: -253.87, avg_win: null, avg_loss: -253.87, total_net: -253.87,
    max_drawdown: 253.87, curve: [{ closed_at: "2026-10-09T09:00:00+00:00", id: 3, cumulative: -253.87 }] },
  method: "Every paper trade is kept: none can be deleted.", rules: [], default_slippage_pct: 0.5,
};

test("fno paper book: unmarked, awaiting and closed trades are labelled; results count them", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/paper": BOOK, "GET /api/fno/risk-notice": { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" } });
  await page.goto("/fno/paper");
  await expect(page.getByText(/none can be deleted/i).first()).toBeVisible();
  const open = page.locator("[id='1']");
  await expect(open.getByText(/net if closed now unknown/)).toBeVisible(); // not marked yet: never ₹0
  await expect(open.getByText(/not marked yet/)).toBeVisible();
  await expect(page.locator("[id='2']").getByText(/waiting for the settlement price/)).toBeVisible();
  await expect(page.locator("[id='2']").getByText(/waiting for NSE's close/)).toBeVisible();
  await expect(page.locator("[id='3']").getByText(/net −₹254/)).toBeVisible();
  await expect(page.getByText(/0 won · 1 lost/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});
