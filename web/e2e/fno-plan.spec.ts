// F&O trade plans and market context (#317): the context panel labels every indicator with its evidence and says it
// is not a signal; "Suggest setups" shows an AVOID card with its failed backtest and a qualified ENTER card; the
// one-click action records a paper trade and arms its exit alerts. Made-up chain and plans; no API or broker.
import { type Page, expect, test } from "@playwright/test";

import { AVOID, CHAIN, CONTEXT, ENTER, NOTICE } from "./fixtures-fno";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

const ROUTES = {
  // a week ahead of today: the page drops expiries before today, so fixed dates went stale after 20-Oct-2026
  "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: [new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10)], strikes: [] },
  "GET /api/fno/NIFTY/chain": CHAIN, "GET /api/fno/risk-notice": NOTICE, "GET /api/fno/NIFTY/context": CONTEXT,
  "GET /api/fno/NIFTY/suggest": { symbol: "NIFTY", today: "2026-10-13", cards: [AVOID, ENTER], note: "The library's pre-registered recipes. Not a recommendation." },
  "POST /api/fno/paper": { id: 7, legs: [] }, "POST /api/fno/exit-rules": { id: 1 },
};
const OPTIONAL = ["GET /api/market/status", "GET /api/fno/positions", "GET /api/fno/paper", "GET /api/fno/NIFTY/events", "GET /api/fno/NIFTY/iv"];

test("fno: market context labels evidence; suggest shows AVOID with the failed backtest and a qualified ENTER", async ({ page }) => {
  const api = await mockApi(page, ROUTES);
  await page.goto("/fno");
  const ctx = page.locator("section").filter({ has: page.getByRole("heading", { name: "Market context" }) });
  await expect(page.getByText(/Context, not signals/)).toBeVisible();
  await expect(ctx.getByText("Trend: uptrend (emerging)")).toBeVisible();
  await expect(ctx.getByText("evidence: weak / contested").first()).toBeVisible();
  await expect(ctx.getByText(/Contracts long − short/)).toBeVisible();
  await page.getByRole("button", { name: "Suggest setups" }).click();
  const avoid = page.getByRole("article", { name: /NIFTY put credit spread/ });
  await expect(avoid.getByTestId("plan-verdict")).toHaveText("AVOID");
  await expect(avoid.getByTestId("plan-evidence")).toContainText("FAILED out-of-sample: expectancy ₹-1,789/trade, t=-1.3");
  const enter = page.getByRole("article", { name: /short iron condor/ });
  await expect(enter.getByTestId("plan-verdict")).toHaveText("ENTER (no proven edge — size small)");
  await expect(enter.getByText(/1× these legs/)).toBeVisible();
  await expect(enter.getByText(/buy back at ₹127.50\/share/)).toBeVisible();
  await enter.getByRole("button", { name: "Paper trade + alerts" }).click();
  await expect(enter.getByRole("status")).toContainText("Paper trade #7 recorded with 3 exit alerts");
  expect(api.count("POST /api/fno/paper")).toBe(1);
  expect(api.count("POST /api/fno/exit-rules")).toBe(3);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(OPTIONAL);
});
