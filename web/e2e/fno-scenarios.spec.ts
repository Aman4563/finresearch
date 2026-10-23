// The F&O book's scenarios page (#394): the spot × vol grid as a heatmap per horizon, reverse stress, the historical
// scan and its ES97.5, episodes, gap and expiry-day scenarios, liquidity, exposures and betas with SE and as-of.
// Unknown figures read "unknown" with the reason, never ₹0. Made-up legs and P&Ls; no API, broker or database.
import { expect, test } from "@playwright/test";

import * as F from "./fixtures-fno";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const routes = (body: unknown) => ({ "GET /api/fno/scenarios": body, "GET /api/fno/risk-notice": F.NOTICE });

test("book scenarios: heatmap by horizon, ES, episodes, betas and liquidity", async ({ page }) => {
  const api = await mockApi(page, routes(F.SCENARIOS));
  await page.goto("/fno/scenarios");
  await expect(page.getByRole("heading", { name: "Book scenarios" })).toBeVisible();
  const grid = page.getByRole("table", { name: "P&L by NIFTY move and vol shift" });
  // -5 % and +0 vol points, one session: scPnl(-0.05, 0, "1d") = -2250 + 150 - 1500 = -3600
  await expect(grid.getByRole("cell", { name: /−₹3,600/ }).first()).toBeVisible();
  await page.getByRole("tab", { name: "5 sessions" }).click();
  await expect(grid.getByRole("cell", { name: /−₹3,050/ }).first()).toBeVisible(); // +700 instead of +150
  await expect(page.getByText(/ES97\.5/).first()).toBeVisible();
  await expect(page.getByText("₹19,650").first()).toBeVisible();
  await expect(page.getByText(/1,180 start days/).first()).toBeVisible();
  await expect(page.getByText(/waits for the vault/).first()).toBeVisible();
  await expect(page.getByRole("row", { name: /BANKNIFTY.*1\.15.*0\.09.*9 Oct 2026/ })).toBeVisible();
  await expect(page.getByText(/Liquidity-adjusted stress needs P1\.1/)).toBeVisible();
  // #480: each expiry's carry (its scenarios roll the forward at it, keeping the market's dividend) and its own
  // discount rate (#479: the RBI repo-rate proxy at the expiry's tenor, named; never FBIL or a bare 6.5 %)
  await expect(page.getByText(/NIFTY 27 Oct 2026: forward 24,980 \(put-call parity fit over 5 strikes' mids\); carry 4\.50% a year \(put-call parity fit over 5 strikes' mids against the chain's underlying\), implied dividend yield 0\.99%; discount rate 5\.49% \(RBI repo-rate proxy: repo 5\.50 % in force from 07-Oct-2026\)/)).toBeVisible();
  await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("book scenarios: unknown cells and ES read unknown with the reason", async ({ page }) => {
  const api = await mockApi(page, routes(F.UNKNOWN_SCENARIOS));
  await page.goto("/fno/scenarios");
  const grid = page.getByRole("table", { name: "P&L by NIFTY move and vol shift" });
  await expect(grid.getByRole("cell", { name: "unknown" }).first()).toBeVisible();
  await expect(grid.getByRole("cell", { name: /₹0\b/ })).toHaveCount(0);
  await expect(page.getByText(/RELIANCE: beta to NIFTY unknown/).first()).toBeVisible();
  await expect(page.getByText(/fewer than the 250 needed/).first()).toBeVisible();
  await expect(page.getByText(/a future \(the chain quotes no futures\)/)).toBeVisible();
  // #480: an expiry whose carry cannot be read says so; its scenarios are unknown, never rolled at the rate
  await expect(page.getByText(/BANKNIFTY 27 Oct 2026: forward 51,820 .*carry unknown \(no reliable put-call parity fit/)).toBeVisible();
  api.expectClean();
});

test("book scenarios: an empty book says so instead of a grid of zeros", async ({ page }) => {
  const api = await mockApi(page, routes(F.EMPTY_SCENARIOS));
  await page.goto("/fno/scenarios");
  await expect(page.getByText("No open legs")).toBeVisible();
  await expect(page.getByRole("table", { name: "P&L by NIFTY move and vol shift" })).toHaveCount(0);
  api.expectClean();
});
