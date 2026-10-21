// Smoke tests (#248): the states the app must never get wrong, on desktop and on a 390 px phone (projects in
// playwright.config.ts). Assertions use roles and loose patterns rather than exact copy, so wording changes don't
// break them; what they pin down is that an unknown or stale value is labelled as such.
import { type Page, expect, test } from "@playwright/test";

import { ARCHIVED_WATCH, ARCHIVED_WATCHES, BOND_DETAIL, BOND_SNAPSHOT, CLAIM, DOC_LINES, GOAL_PLAN_CPI, GOAL_PLAN_CPI_UNKNOWN, GOAL_PLAN_LOWER, REPORT, SIGNAL, SNAPSHOT, TAX, WEALTH } from "./fixtures";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

test("portfolio: unknown cost, stale price and incomplete tax are labelled", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/portfolio/tax": TAX,
    "GET /api/connections/summary": [],
  });
  await page.goto("/portfolio");
  const holdings = page.getByRole("region", { name: "Holdings" });
  await expect(holdings.getByText("Example Opening Balance Fund")).toBeVisible();
  await expect(holdings.getByText(/cost unknown/i).first()).toBeVisible();
  // #263: the demerged holding's return reads "incomplete", not a percentage or a bare dash
  const demerged = holdings.getByRole("row").filter({ hasText: "Example Demerge Ltd" });
  await expect(demerged.getByText(/^incomplete$/)).toBeVisible();
  await expect(page.getByText(/some costs are unknown/i)).toBeVisible();
  // the fund's price is a statement NAV from June: shown with its date, never as today's price
  await expect(holdings.getByText(/statement NAV|stale/i)).toBeVisible();
  await expect(holdings.getByText(/30 Jun/)).toBeVisible();
  // #295: the fund's units carry their statement's date, flagged when old
  await expect(holdings.getByText(/units as of 28 Jun.*\(CAMS CAS\)/)).toBeVisible();
  await expect(holdings.getByText(/units date unknown/)).toBeVisible(); // the opening-balance fund: no statement date
  // #297: expected SIP instalments are an estimate beside the units, and a separate line under the totals
  await expect(holdings.getByText(/units estimated for 1 of 2 instalments.*waiting for statement/)).toBeVisible();
  await expect(page.getByText(/Estimated: .*4,000 in 2 SIP instalments.*estimated value unknown.*not in the totals/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);

  await page.goto("/portfolio#tax");
  await expect(page.getByText(/tax incomplete/i)).toBeVisible();
  await expect(page.getByText(/^incomplete$/i)).toBeVisible(); // the year's total is not shown as a figure
  if (phone(page)) await expectNoHorizontalOverflow(page);
  // cards outside this test's scope fall back to their error / unknown states
  api.expectClean(["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
    "GET /api/disclosures/tracked", "GET /api/portfolio/ais"]);
});

test("tax: an earlier loss with an unknown filing status is shown as unknown, never applied (#344)", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/portfolio/tax": TAX,
    "GET /api/connections/summary": [],
    "PUT /api/portfolio/tax/losses": { returns_filed_on_time: { "2025": true }, brought_forward: [] },
  });
  await page.goto("/portfolio#tax");
  await expect(page.getByText(/earlier losses not counted/i)).toBeVisible();
  const ledger = page.getByRole("region", { name: "Carry-forward ledger" });
  await expect(ledger.getByText(/filing unknown/i)).toBeVisible();
  await expect(ledger.getByText("FY 2032-33")).toBeVisible(); // usable for 8 years after the loss year
  await page.getByRole("button", { name: "On time" }).click();
  await expect.poll(() => api.count("PUT /api/portfolio/tax/losses")).toBe(1);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
    "GET /api/disclosures/tracked", "GET /api/portfolio/ais"]);
});

test("portfolio: a matured bond says so and offers to record its redemption (#294)", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/portfolio": BOND_SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/connections/summary": [],
    "GET /api/portfolio/holdings/5": BOND_DETAIL,
  });
  await page.goto("/portfolio");
  const holdings = page.getByRole("region", { name: "Holdings" });
  const ncd = holdings.getByRole("row").filter({ hasText: "Example Finance NCD" });
  await expect(ncd.getByText(/matured — record redemption/).first()).toBeVisible();
  await expect(ncd.getByText(/matured: value unknown/)).toBeVisible(); // never the last traded price
  const sgb = holdings.getByRole("row").filter({ hasText: "SGB 2020-21" });
  await expect(sgb.getByText(/matures 11 Aug 2028/)).toBeVisible();
  await expect(sgb.getByText(/next coupon 11 Feb 2027/)).toBeVisible();
  await expect(page.getByText(/past maturity: record the redemption/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  await ncd.click();
  const panel = page.getByTestId("bond-panel");
  await expect(panel.getByText(/Record redemption \/ coupon/)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Record" })).toBeVisible();
  api.expectClean(["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
    "GET /api/disclosures/tracked", "GET /api/portfolio/ais", "GET /api/portfolio/tax"]);
});

test("research: a blocked report is marked not published, and a citation opens its evidence", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/runs/1/report": REPORT,
    "GET /api/runs/1/claims": [CLAIM],
    "GET /api/runs/1/pack": { files: [], entries: [] },
    "GET /api/documents/7/lines": DOC_LINES,
  });
  await page.goto("/runs/1/report#report");
  await expect(page.getByText(/not published/i).first()).toBeVisible();
  await expect(page.getByText(/C40 is contradicted/)).toBeVisible();
  await page.getByRole("button", { name: /claim 12/ }).first().click();
  const panel = phone(page) ? page.getByRole("dialog") : page.locator("aside").filter({ hasText: "Evidence for" });
  await expect(panel.getByText(/Evidence for/)).toBeVisible();
  await expect(panel.getByText(/p14 L230/)).toBeVisible();
  await expect(panel.getByText(/quote found/i)).toBeVisible();
  await expect(panel.getByText("Revenue from operations for Fiscal 2026 was Rs. 3,912.40 million").last()).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(["GET /api/runs/1/insights", "GET /api/runs/1/suggestion"]);
});

test("signals: an informational signal is labelled as no proven edge, never as a call", async ({ page }) => {
  const api = await mockApi(page, { "POST /api/signals/stock/EXTEX": SIGNAL });
  await page.goto("/stocks/EXTEX");
  await expect(page.getByText(/informational/i).first()).toBeVisible();
  await expect(page.getByText(/no proven edge/i).first()).toBeVisible();
  await expect(page.getByText(/^(BUY|ACCUMULATE)$/)).toHaveCount(0); // the composite's action is not shown as the call
  if (phone(page)) await expectNoHorizontalOverflow(page);
  // the stock page's market cards read exchange data the fixtures don't carry: they show their error states
  api.expectClean(["GET /api/stocks/EXTEX", "GET /api/watches", "GET /api/market/status"]);
});

test("connections: the Groww card says which market data Groww supplies and today's calls, never a secret", async ({ page }) => {
  const groww = {
    key: "groww", label: "Groww", account: "Groww", auth_kind: "totp", capabilities: ["holdings", "positions", "trades", "funds"],
    fields: [{ name: "api_key", label: "TOTP API key", secret: true, required: true, help: "" }], configured: true, enabled: true,
    auto_sync: true, config: { api_key: "••••••••", api_key_set: true }, status: "connected", next_step: "Syncs after the close.",
    token_set: true, connected_as: null, token_expires_at: "2026-10-10T06:00:00+05:30", last_sync_at: "2026-10-09T16:05:00+05:30",
    last_error: null, positions: [], funds: null, last_summary: null,
    t1_semantics: { result: "separate", day: "2026-10-12", evidence: { separate: 2, included: 0, unknown: 0 } },
    orders_poll: { at: "2026-10-09T10:45:00+05:30", status: "ok", orders: 1, added: 1, updated: 0 },
  };
  const api = await mockApi(page, {
    "GET /api/profile": { capital_per_ipo_inr: "15000", risk_appetite: "medium", horizon: "listing", tax_slab_pct: "30",
      category: "retail", holdings: [], rules: [], notes: "" },
    "GET /api/connections": { connections: [groww], inbox: { path: "/tmp/inbox", pending: [] } },
    "GET /api/connections/log": [],
    "GET /api/connections/groww/market-data": {
      active: true, session: "connected", phase: "open", instruments_day: "2026-10-09",
      supplies: [{ kind: "prices", label: "Stock prices for the portfolio" }, { kind: "mutual_funds", label: "Not supplied: mutual-fund NAVs stay on AMFI" }],
      last_answered: { prices: "2026-10-09T10:15:00+05:30" },
      calls_today: { day: "2026-10-09", total: 42, categories: [
        { category: "live", label: "Live and historical data", calls: 37, per_second: 10, per_minute: 300, daily_cap: null },
        { category: "auth", label: "Login (token)", calls: 1, per_second: 5, per_minute: 30, daily_cap: 150 }] },
      close_check: { checked: 26, matched: 25, since: "2026-10-09", last_day: "2026-10-12", tolerance_inr: 0.01 },
    },
  });
  await page.goto("/profile#connections");
  const card = page.getByTestId("groww-market-data");
  await expect(card.getByText(/market data from groww/i)).toBeVisible();
  await expect(card.getByText(/in use/i)).toBeVisible();
  await expect(card.getByText(/mutual-fund NAVs stay on AMFI/i)).toBeVisible();
  await expect(card.getByText(/calls today/i)).toBeVisible();
  await expect(card.getByText(/Login \(token\) 1 of 150/)).toBeVisible();
  await expect(card.getByTestId("groww-close-check")).toContainText("Groww close matched the official close on 25 of 26 stock-days");
  await expect(page.getByTestId("groww-t1")).toContainText("T1 handling: confirmed separate on 2026-10-12");
  await expect(page.getByText(/Orders read in session/)).toContainText("1 new, 0 updated");
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(["GET /api/"]); // the profile page's other cards may stay unanswered here
});

test("wealth: a goal run on the priced part reads 'at least' / 'at most' and names the unpriced holding", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/wealth": WEALTH,
    "GET /api/wealth/goals/1/plan": GOAL_PLAN_LOWER,
  });
  await page.goto("/wealth#goals");
  await expect(page.getByText(/lower bound: part of the earmarked portfolio has no price/i)).toBeVisible();
  await expect(page.getByText(/Example Beta Ltd/)).toBeVisible();
  await expect(page.getByText(/45\.5 % of the earmarked portfolio/)).toBeVisible();
  await expect(page.getByText(/^at least ₹10,300$/)).toBeVisible(); // set aside today
  await expect(page.getByText(/^at most ₹7,400$/)).toBeVisible(); // SIP for 75 %
  await expect(page.getByText(/^unknown$/)).toBeVisible(); // SIP for 90 %: none found from the priced part, not "not reachable"
  await expect(page.getByText(/not reachable/i)).toHaveCount(0);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("wealth: a CPI-based goal shows MoSPI's rate, its premium over general CPI and the vintage used", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/wealth": WEALTH, "GET /api/wealth/goals/1/plan": GOAL_PLAN_CPI });
  await page.goto("/wealth#goals");
  await expect(page.getByText(/MoSPI CPI education: 7\.25 % a year/)).toBeVisible();
  await expect(page.getByText(/\+1\.75 pp/)).toBeVisible(); // over general CPI
  await expect(page.getByText(/base 2024=100 to Sept? 2026/)).toBeVisible(); // the vintage used
  await expect(page.getByText(/CPI education average \+2 pp/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("wealth: an unreadable CPI series reads as unknown with the reason and the labelled fixed rate", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/wealth": WEALTH, "GET /api/wealth/goals/1/plan": GOAL_PLAN_CPI_UNKNOWN });
  await page.goto("/wealth#goals");
  await expect(page.getByText(/^CPI inflation unknown$/)).toBeVisible();
  await expect(page.getByText(/HTTP 503\); simulated at your fixed 6 % a year instead/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("monitor: an archived IPO watch is read once, never polled, and shows its listing gain", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-12T05:00:00Z") });
  const api = await mockApi(page, {
    "GET /api/watches": ARCHIVED_WATCHES,
    "GET /api/watches/77": ARCHIVED_WATCH,
    "GET /api/companies": [],
  });
  await page.goto("/monitor");
  await page.getByRole("tab", { name: /^Archive/ }).click(); // an archived IPO is in the Archive group, not Active
  await expect(page.getByText("Example Listed Ltd").first()).toBeVisible();
  await expect(page.getByText("+20.00%").first()).toBeVisible(); // the listing gain replaces the subscription meter
  await page.goto("/monitor/77");
  await expect(page.getByRole("heading", { name: "Example Listed Ltd" })).toBeVisible();
  await page.clock.fastForward("05:00"); // five minutes: a polled page would have fetched five more times
  await page.waitForTimeout(200);
  expect(api.count("GET /api/watches/77")).toBe(1);
  expect(api.count("GET /api/watches/77/live")).toBe(0);
  api.expectClean(["GET /api/"]);
});
