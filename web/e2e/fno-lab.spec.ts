// The F&O Strategy Lab (#318): the variants count and the locked split are stated, a finished run shows its
// overfitting warnings before the numbers and the library beside it, a refused spec shows the server's reason, and a
// paper rule set lists its signals. Made-up numbers; no API or database involved.
import { type Page, expect, test } from "@playwright/test";

import { LAB, LAB_PAPER as PAPER, LAB_VIEW as VIEW, NOTICE } from "./fixtures-fno";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

test("fno lab: variants count, locked split, run warnings first, library beside it, paper signals", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/lab": LAB, "GET /api/fno/lab/paper": { rules: PAPER },
    "GET /api/fno/lab/runs/7": VIEW, "GET /api/fno/risk-notice": NOTICE });
  await page.goto("/fno/lab");
  await expect(page.getByText(/10 variants tried so far: 8 pre-registered \+ 2 from the lab/)).toBeVisible();
  await expect(page.getByText(/out of sample from 2024-07-01 \(locked/)).toBeVisible();
  await expect(page.getByText("2 versions seen out of sample")).toBeVisible();
  await page.getByRole("button", { name: /#7 OOS done/ }).click();
  await expect(page.getByText(/Only the first look at the out-of-sample years was clean/)).toBeVisible();
  await expect(page.getByText(/N = 10 variants: 8 pre-registered \+ 2 from the lab/)).toBeVisible();
  await expect(page.getByRole("cell", { name: "NIFTY put credit spread, monthly" })).toBeVisible();
  await expect(page.getByRole("link", { name: "#9 open" })).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("fno lab: a refused spec shows the server's reason", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/lab": { ...LAB, strategies: [] }, "GET /api/fno/lab/paper": { rules: [] },
    "GET /api/fno/risk-notice": NOTICE,
    "POST /api/fno/lab/strategies": [422, { detail: "only 200 days of stored history; a strategy needs at least 540" }] });
  await page.goto("/fno/lab");
  await expect(page.getByText("No strategy yet")).toBeVisible();
  await page.getByRole("button", { name: "Save strategy" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /at least 540/ })).toBeVisible();
  api.expectClean();
});
