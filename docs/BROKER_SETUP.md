# Connecting your broker accounts (read-only)

FinResearch can read your holdings, open positions and trades from your broker so the Portfolio page, alerts and
suggestions reflect your real money. It **only reads**: the app contains no code that places, changes or cancels an
order (`finresearch/portfolio/connectors/base.py` allowlists GET paths per broker plus the single login POST, and the
tests fail if any connector grows an order-shaped method or path).

Everything happens on your Mac. API keys, secrets, TOTP seeds and tokens are stored only in your local database (the
`broker_connection` table), are shown masked in the UI and API, are redacted from every error and log line, are never
committed and are never sent to the AI.

Setup lives in **Profile → Connections** (`http://127.0.0.1:3100/profile#connections`).

## Which one should I use?

| Source | What it gives | Cost | Daily login | Hands-free? | Verdict |
|---|---|---|---|---|---|
| **Groww Trade API** | Holdings (no price), positions, **today's** orders, funds. No mutual funds | Paid: ₹499 + GST/month early-bird (₹2000 standard) | TOTP key: the app logs in itself each morning (session ends 06:00) | Yes | Implemented. Worth it only if you pay for the subscription |
| **Dhan HQ** | Holdings, positions, **trade history by date range**, funds | Free | PIN + TOTP: the app logs in itself (24 h tokens) — or paste a 24 h token | Yes | Implemented. Best free, hands-free option |
| **Upstox** | Holdings (with price), positions, **3 financial years of trades** | Free | One browser login a day (session ends 03:30) | No | Implemented. Best history |
| **Zerodha Kite Connect** | Holdings, positions, **today's** trades, **Coin mutual funds**, funds | Free "Personal" plan | One browser login a day (session ends 06:00) | No | Implemented. Only broker API with MF holdings |
| Angel One SmartAPI | Holdings, positions, trade book | Free (unverified) | PIN + TOTP | Yes | Not implemented: docs site is JS-only, response fields could not be verified |
| Fyers | Holdings, positions, trade book | Free (unverified) | Auth code | Partly | Not implemented: docs URLs returned 404 |
| RBI Account Aggregator | Bank, deposits, MF, insurance, pension (equities not listed) | – | Consent in an AA app | No | **Not possible** for an individual app: data flows only to regulated FIUs |
| **CAS PDFs** (CAMS/KFintech, NSDL/CDSL) | Full MF transaction history; demat holdings (no cost) | Free | Request by email / monthly e-CAS | Parsing yes | Implemented: statement inbox folder + Import tab |
| **Broker exports** (tradebooks, holdings statements) | Trades with dates/prices; holdings with average cost | Free | Manual download | No | Implemented: Import tab and inbox |

Static IP: SEBI's retail-API framework (circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013, 4-Feb-2025) is about
order flow. Dhan's docs state explicitly that a static IP is needed only to place/modify/cancel orders and "no such IP
whitelisting is required" to read orders and trades. The other brokers' docs pages did not mention it for read-only use
(not verified). This app never calls an order API, so a static IP should not be needed.

## How a sync merges into your portfolio

1. **Baseline** — a holding the app has no history for in that broker account (and that you do not already hold in a
   Manual or CAS account) gets one opening row: the broker's quantity at its **average cost**, purchase date unknown.
   P&L works at once; tax shows "date unknown" until you enter the date or import the tradebook. When the broker also
   returns trade history (Upstox: 3 financial years; Dhan: the last year), those trades become the lots and a baseline
   covers only the units bought before the history starts (none if the trades explain the whole holding). Baselines are their
   own import on the Portfolio → Import tab, so you can delete them in one click. When you later import a full
   tradebook for that account, the baseline is ignored automatically (the older history replaces it).
2. **Trades** — each executed equity trade becomes a buy/sell in the broker's account ("Groww", "Zerodha", …, the same
   account the CSV importers use). Re-syncing is idempotent. A trade already present from another source is skipped
   and a partial overlap is **not** added and is listed as a conflict to review (the rules below).
3. **Nothing is overwritten** — manual, CAS and CSV rows are never edited or deleted by a sync. Differences between the
   broker's quantities and your lots are listed (Profile → Connections → "differences to review" and the sync log).
4. Positions and cash are shown on the Connections card; they never become lots.
5. Best first sync: **before 09:15 or on a holiday** (no trades today), so the baseline and today's trades cannot overlap.
   A buy made today usually appears in the broker's holdings only the next day; the reconciliation shows that one-day
   difference and it clears on the next sync.

## The same trade from two sources (every import path)

A tradebook upload, a CAS, a manual entry, an API sync, the statement inbox and a holdings statement all go through one
check before anything is written (`portfolio/dedupe.py`; #236). For each instrument (ISIN, NSE symbol, BSE code or AMFI
scheme code), account, trade day and side, the incoming rows are compared with rows that came from *another* source
(re-importing the same source is caught separately):

1. **Same id** — the same exchange trade id, or the same order id with the same total units → already present.
2. **Different exchange trade ids** (Zerodha [U: Kite's trade id taken to be the exchange's], Dhan) → different
   trades, never merged. Groww's API ids are Groww's own order ids, not the exchange's, so for Groww (and Upstox,
   unverified) only a shared id counts.
3. **Same quantity at a price within 0.5 %** → already present, matched one row to one row: two real buys of 10 on
   the same day against one stored buy of 10 leave one of them new.
4. **What is left adds up** to the same units at the same average price (several fills against one order, or one
   manual entry) → already present.
5. **Anything else** left on both sides (6 units here, 10 there; the same units at another price) is a **conflict**:
   not added, listed on the import result for you to review. Nothing is ever merged silently or overwritten.

Only the same account is compared, plus manual entries in any account (a manual buy is often kept under "Manual"):
the same buy in two demat accounts is two trades. The import preview says "already present from zerodha_api" (or
manual, groww, cas …) for such rows instead of "new", and lists the conflicts. A manual entry is checked against
earlier imports too: the form answers "already present from …" and offers **Add anyway** for a genuine second trade.

**Mutual funds:** a broker's fund holdings (Kite Coin) become a baseline only for a fund the app does not hold yet. When
a CAS arrives later and its history covers the baseline's units on the baseline's day, the baseline is ignored by the
lots, XIRR and history (it stays stored; deleting the CAS import brings it back). A CAS with fewer units than the
baseline is a conflict to review.

Schedule: with the monitor running (`uv run finresearch serve`, the default), each enabled connection syncs once per
trading day after **16:00 IST** (missed days are caught up; a failed scheduled attempt is not retried until the next
day, so a wrong TOTP secret cannot burn Groww's 150-logins-a-day limit), and the statement inbox is scanned every 5
minutes.
**Sync now** runs one immediately. When a browser-login broker's session has ended, the connection shows "log in
again", the scheduled sync stops, and one in-app alert per day says so (forwarded to your phone if you forward
warnings in Profile → Notifications).

## Groww (TOTP, hands-free, paid)

1. Subscribe to the Trading API: groww.in → Profile → **Trading APIs** (₹499 + GST a month early-bird at the time of
   writing; the docs call ₹2000 + GST the standard price).
2. On the same page create an API key of type **TOTP**. Copy the **TOTP API key** (a long token) and the **TOTP
   secret** (the base32 text shown with the QR code; if only a QR is shown, most authenticator apps can reveal the
   text, or use the "can't scan?" option). Do not use the 6-digit code — the app makes the code itself each morning.
3. Profile → Connections → Groww → Set up: paste both → **Save** → **Log in now** → **Sync now**.
4. Groww's order list covers **today only**: history builds up from daily syncs. For older trades, download Groww's
   stock order history (Reports) and import it on Portfolio → Import (it is recognised automatically). Groww mutual
   funds are not in the Trade API: use a CAS.
5. Unverified until your first real sync (the docs did not show them): the name of the token field in the login
   answer (the app tries `token`, `access_token`, `accessToken`) and the order-list key (`order_list`/`orders`). If
   the first sync fails, the error on the card says which step.


**Market data from Groww (#267).** While the Groww connection is on and its session is valid, the app also reads
market data from the same paid API, first, and falls back to NSE/BSE (AMFI for funds) on any error or unknown
instrument; every price says where it came from ("Groww LTP", "Groww daily close", "NSE quote", ...):
- portfolio prices: the batched LTP (50 stocks a call) in session, Groww's daily close after 16:00 IST;
- the latest value on index and stock charts in session; the F&O option chain (with IV; change in OI, bid and ask
  are not in Groww's chain and show as unknown);
- the daily top-up of the price history used by performance, risk and the charts (older history and any range across
  a recorded split/bonus stay on the exchange's raw closes until Groww's adjustment rule is verified).
Calls share one budget per Groww rate category across every FinResearch process (Live Data 10/s and 300/min, Non
Trading 20/s and 500/min, Auth 5/s and 30/min, at most 140 of the 150 daily logins); LTP is cached 15 s in session,
closes until the next open (no calls on weekends or holidays once cached), the instruments file is read once a day.
When the session has expired during market hours, one background login a day runs through the normal sync (only if
"Sync daily after the close" is on). The Groww card in Profile → Connections shows what Groww is supplying and today's
calls by category. Expect roughly 120-130 calls on a trading day for 20 stocks (simulated with the real client).
## Dhan (TOTP, hands-free, free)

1. web.dhan.co → My Profile → note your **Client ID**.
2. Hands-free: enable TOTP in Dhan (Setup TOTP) and keep the **base32 secret**; you also need your Dhan **PIN**.
   Enter Client ID + PIN + TOTP secret. Or, without TOTP: generate an **access token** (My Profile → Access DhanHQ
   APIs, valid 24 hours) and paste it — you will need to paste a new one each day.
3. **Save** → **Log in now** → **Sync now**. The first sync reads the last year of trade history and uses it for the
   lots (a baseline covers only units bought before that); older trades: import a tradebook.
4. Unverified until the first real login: the exact parameter names of `auth.dhan.co/app/generateAccessToken`
   (sent as `dhanClientId`, `pin`, `totp` query parameters). If the automatic login fails, paste a token instead.

## Upstox (browser login once a day, free)

1. account.upstox.com/developer/apps → **New app**. Redirect URL: copy it from Profile → Connections → Upstox →
   Set up ("Redirect URL for your Upstox app"), e.g. `http://127.0.0.1:8710/api/connections/upstox/callback` — it
   must match exactly (host and port of the API you use).
2. Paste the **API key** and **API secret** → **Save** → **Connect**. Log in on Upstox; you are sent back to Profile
   with "Connected". Press **Sync now** (the first sync reads the last 3 financial years of trades and builds your lots
   from them, with real purchase dates and prices).
3. Each day after 03:30 the session ends: press **Reconnect** before the evening sync (or Sync now after).

## Zerodha Kite Connect (browser login once a day, free Personal plan)

1. developers.kite.trade → create an app on the **Personal** plan (free; portfolio APIs, no market data). Redirect
   URL: copy it from Profile → Connections → Zerodha → Set up, e.g.
   `http://127.0.0.1:8710/api/connections/zerodha/callback`.
2. Paste the **API key** and **API secret** → **Save** → **Connect** → log in to Kite → back on Profile → **Sync now**.
3. Kite returns **today's trades only**: history builds up from daily syncs; import Console tradebooks (Console →
   Reports → Tradebook, up to 365 days a file) for older trades. Coin mutual funds are read and reconciled against
   your CAS; a fund the app does not track anywhere gets a baseline in "Zerodha MF".
4. The session ends at 06:00 each day: press **Reconnect** before the evening sync.

## Without any API: the statement inbox

1. Profile → Connections → **Statement inbox**: turn it on. The folder is shown (by default
   `data/portfolio/inbox/` in the app folder; it is gitignored and owner-only).
2. Drop in any of:
   - CAMS/KFintech **detailed** CAS PDF (request at mfs.kfintech.com or camsonline.com: "Detailed", with
     transactions). Imported as transactions.
   - NSDL/CDSL **e-CAS** PDF (monthly by email): demat holdings only (no cost), compared with your lots and logged.
   - Broker **tradebooks**: Zerodha Console tradebook (CSV/XLSX), Groww stock order history (XLSX), Upstox trade book.
   - Broker **holdings statements**: Zerodha Console holdings (XLSX), Groww holdings statement, or any sheet with ISIN,
     quantity and average-price columns → the same baseline/reconciliation rules as an API sync.
3. PDFs need the password (usually your PAN in capitals). Save it in the inbox settings **only if you want** hands-free
   PDF imports; otherwise PDFs wait in the folder and you import them on Portfolio → Import.
4. Imported files move to `inbox/processed/`, unreadable ones to `inbox/failed/`; the sync log says why. The same file
   twice is recognised and not imported again.

## Sources read (30-Sep-2026)

Groww: groww.in/trade-api, /trade-api/docs/curl, /curl/portfolio, /curl/orders, /curl/margin, /curl/user,
/docs/python-sdk (+ /portfolio, /changelog, /annexures), pypi.org/pypi/growwapi. Zerodha: kite.trade/docs/connect/v3/
(+ /user/, /portfolio/, /orders/, /mutual-funds/, /exceptions/), zerodha.com/products/api/, Zerodha support (Console
tradebook, 365 days). Upstox: upstox.com/developer/api-documentation/{authentication, get-token, get-holdings,
get-positions, get-historical-trades}/, upstox.com/trading-api/. Dhan: dhanhq.co/docs/v2/ (+ /authentication/,
/portfolio/, /statements/, /orders/, /funds/), dhanhq.co. Angel One: github.com/angel-one/smartapi-python (README,
smartConnect.py). Fyers: pypi.org/pypi/fyers-apiv3. SEBI circular page (4-Feb-2025, header only). RBI Master
Direction NBFC-AA (rbi.org.in, id=10598), sahamati.org.in/faq. KFintech CAS page, cdslindia.com CAS login,
github.com/codereverser/casparser (README, types.py), pypi.org/pypi/casparser. Pages that failed to load (Fyers docs,
NSE circulars, NSDL CAS, Upstox extended-token and static-IP pages) are marked unverified above.

## Importing files by hand (Portfolio → Import)

Drop the files anywhere on the Import tab, or click the dashed box and choose several at once. Each file goes to the right importer:

- **Spreadsheets** (XLSX/CSV) preview straight away.
- **PDFs** wait for their password in a masked field on their own row. The password goes once to the local API (127.0.0.1) and is never stored, logged or sent anywhere else. If you'd rather not type it in a browser, use `uv run finresearch portfolio import-cas <file>` in your own Terminal. It asks with a hidden prompt.

What to import:

1. **Groww:** import `Stocks_Order_History_…xlsx` first, then `Stocks_Holdings_Statement_…xlsx`. The holdings statement fills any holding the order history does not explain.
2. **Mutual funds:** the CAMS/KFintech CAS PDF.
3. **NSDL/CDSL e-CAS PDF:** used as a units check only.

**Corporate actions:** "Sync corporate actions" adds splits and bonuses. A demerger, rights issue, merger or
amalgamation, scheme of arrangement, ISIN change, buyback, capital reduction or consolidation on a stock you held on its
ex-date is not modelled: it is recorded on the holding (type, ex-date, NSE/BSE subject and source URL), the holding's
cost shows as unknown and every tax year with a sale on or after the ex-date shows as incomplete, until you enter the
cost allocation by hand and mark it resolved with a note (or add a manual transaction that resolves it). The app never
guesses a cost-split ratio.

P&L and capital-gains reports are refused with an explanation: the app computes these itself from the order history. Old `.xls` files must be saved as `.xlsx` or `.csv` first.
