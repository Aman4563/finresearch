---
name: rhp-navigator
description: Where to find things in an Indian RHP/DRHP (SEBI ICDR layout), which grep patterns work, and the parsing traps. Use before reading offer documents.
---

# Navigating an Indian offer document

**Getting around.** Call `list_sections(document_id)` first. Sections are canonical ids with line and page ranges; read them with `read_section` in 400-line pages. `read_lines_tool` and `grep_document` output starts every line with its number, so cite those numbers exactly.

| Need | Section (canonical id) | Grep hints |
|---|---|---|
| Issue size, fresh vs OFS, sellers | THE_OFFER, CAPITAL_STRUCTURE, OFFER_STRUCTURE | `Offer for Sale`, `Selling Shareholder`, `weighted average cost of acquisition` |
| Price history, pre-IPO rounds, WACA | CAPITAL_STRUCTURE, BASIS_FOR_OFFER_PRICE | `price per equity share`, `Weighted average cost`, `secondary transaction` |
| Use of proceeds | OBJECTS_OF_THE_OFFER | `Net Proceeds`, `schedule of deployment`, `repayment` |
| KPIs, peer P/E, RoNW, NAV | BASIS_FOR_OFFER_PRICE | `Key Performance Indicators`, `listed industry peers`, `RoNW`, `Net Asset Value` |
| Customers and concentration | RISK_FACTORS, OUR_BUSINESS | `Largest customer`, `top 10 customers` |
| Restated P&L, BS, CF | RESTATED_FINANCIALS | use `extract_table` on the statement's line range |
| Exceptional items | RESTATED_FINANCIALS notes | `Exceptional item`, then read the note |
| Related parties | RESTATED_FINANCIALS notes | `Related party`, `Key Managerial Personnel` |
| Contingent liabilities | RESTATED_FINANCIALS notes | `Contingent liabilit`, `capital commitment` |
| Auditor remarks (CARO) | RISK_FACTORS, restated notes | `CARO`, `quarterly returns`, `statements filed with banks` |
| Litigation counts and amounts | LITIGATION | `Criminal`, `Tax`, `Regulatory`, `Material civil` |
| Lock-in | CAPITAL_STRUCTURE | `locked-in`, `Regulation 16`, `Regulation 17` |
| Promoter holding pre/post | CAPITAL_STRUCTURE; price-band ad | `Pre-Offer`, `Post-Offer` |

**Traps**
- **Units:** amounts are usually ₹ million. The price-band ad often uses ₹ crore or ₹ lakh. Say which.
- **Wrapped headers and shifted rows:** tables wrap headers over 3–4 lines and shift some rows sideways, so never read columns by eye. Use `extract_table`.
- **Period labels:** the summary tables and the statements can label periods differently ("As at" vs "For the year ended"). Match by date.
- **OCR'd pages:** scanned annual reports and financial statements are OCR'd, so digits can be garbled. Prefer the RHP text layer, and cross-check OCR'd figures before citing them.
- **DRHP vs RHP:** the RHP supersedes the DRHP. Use the DRHP only for what changed, or for older periods.
