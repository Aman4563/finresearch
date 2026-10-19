// The F&O backtest page (#307): a failing strategy says so plainly with its failed criteria, a candidate shows its
// evidence and the rule's current legs, an unknown value reads "unknown" (never 0), and with no saved run the page
// says how to make one. Made-up numbers; no API or database involved.
import { type Page, expect, test } from "@playwright/test";

import { BACKTESTS as DATA, BT_FAILS as fails, NOTICE, REG } from "./fixtures-fno";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

test("fno backtests: candidate with setup, failing strategy plainly labelled, unknowns not zero", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/backtests": DATA, "GET /api/fno/risk-notice": NOTICE });
  await page.goto("/fno/backtests");
  await expect(page.getByText(/Pre-registered 2026-10-12 · 8 variants/)).toBeVisible();
  await expect(page.getByText(/sell 24300 put @ ~88.5/)).toBeVisible();
  await expect(page.getByText(/margin ≈ —/)).toBeVisible(); // unknown margin: a dash, never ₹0
  const failing = page.locator("[id='nifty_dcs_m']");
  await expect(failing.getByText("fails: OOS deflated Sharpe")).toBeVisible();
  await expect(failing.getByText(/OOS deflated Sharpe: unknown/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("fno backtests: no saved run explains how to make one; nothing passed says so", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/backtests": { registration: REG, run: null }, "GET /api/fno/risk-notice": NOTICE });
  await page.goto("/fno/backtests");
  await expect(page.getByText("No backtest saved yet")).toBeVisible();
  await expect(page.getByText(/finresearch fno backtest run/)).toBeVisible();
  api.expectClean();
  await page.unrouteAll({ behavior: "ignoreErrors" });
  const none = { ...DATA, run: { ...DATA.run!, candidates: [], strategies: [fails] } };
  const api2 = await mockApi(page, { "GET /api/fno/backtests": none, "GET /api/fno/risk-notice": NOTICE });
  await page.goto("/fno/backtests");
  await expect(page.getByText(/No strategy passed every pre-registered criterion/)).toBeVisible();
  api2.expectClean();
});
