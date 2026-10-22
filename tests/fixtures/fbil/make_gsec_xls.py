"""Generator for gsec_synthetic_2022.xls: a SYNTHETIC OLE2 (.xls) workbook in the layout of FBIL's pre-2023 G-sec
file (sheets G-Sec / Par-Yield / SDL / Special; the Par-Yield header row 'FBIL GSec Base/Par Yield' | '03 Jan, 2022').
The yields are made up: 4.00 % at 0.25 years rising 0.10 percentage points per quarter-year tenor, annualised =
semi-annual x 1.01. Not run by the tests (xlwt is not a dependency); re-create with
`uv run --no-project --with xlwt==1.3.0 python tests/fixtures/fbil/make_gsec_xls.py`."""

from pathlib import Path

import xlwt

wb = xlwt.Workbook()
wb.add_sheet("G-Sec").write(0, 0, "synthetic")
ws = wb.add_sheet("Par-Yield")
ws.write(0, 0, "Financial Benchmarks India Private Ltd ")
ws.write(2, 0, "FBIL GSec Base/Par Yield")
ws.write(2, 1, "03 Jan, 2022")
ws.write(3, 0, "Calculating Agent: FIMMDA")
for c, h in enumerate(("Tenor (Year)", "YTM% p.a.(Semi-Annual)", "YTM % p.a.(Annualized)")):
    ws.write(4, c, h)
for i in range(8):  # 0.25 .. 2.0 years
    semi = 4.0 + 0.1 * i
    ws.write(5 + i, 0, 0.25 * (i + 1))
    ws.write(5 + i, 1, semi)
    ws.write(5 + i, 2, semi * 1.01)
wb.add_sheet("SDL").write(0, 0, "synthetic")
wb.add_sheet("Special").write(0, 0, "synthetic")
wb.save(str(Path(__file__).with_name("gsec_synthetic_2022.xls")))
