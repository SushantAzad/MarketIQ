# Phase 8 calculation contract

`financial-formulas-v1` uses a private Decimal context with 38 significant digits and ROUND_HALF_EVEN. Source amounts remain exact strings; division and fractional powers may round to that precision. Float operands, nonfinite numbers, and unsupported currency units are refused. Results are calculated metrics, distinct from reported facts or generated prose.

## Formulas and conventions

- Growth and YoY: `(current / previous - 1) * 100`, expressed in percent. The baseline must be positive; negative or zero baselines return unavailable. Ordinary growth compares the explicitly supplied periods. YoY additionally requires one-year spacing.
- CAGR: `((current / previous) ** (1 / years) - 1) * 100`. Requires annual observations, positive starting value, nonnegative ending value, and an integer count of fiscal years. A zero endpoint returns -100%. Year spacing allows a 14-day tolerance against 365.2425 days/year for fiscal calendars.
- Gross, operating, and net margins: the respective reported profit divided by revenue, times 100. Revenue must be positive.
- Free cash flow: operating cash flow minus payments for property, plant and equipment. Capex is a nonnegative payment magnitude; a missing observation is never treated as zero. FCF growth applies the growth formula to two derived FCF results, retaining both dependency chains.
- Current ratio: current assets divided by positive current liabilities, in ratio units. Negative current assets are refused.
- Debt/equity: `(ShortTermBorrowings + LongTermDebtCurrent + LongTermDebtNoncurrent) / StockholdersEquity`, in ratio units. All three debt components must be explicitly reported and nonnegative; equity must be positive. This is a versioned component-based interest-bearing debt definition, not total liabilities, and does not add separate lease obligations or infer omitted debt components.
- ROE/ROA: period net income divided by the arithmetic mean of opening/closing equity or assets, times 100. Both balances must be positive. Opening date is exactly the day before the income period starts; closing date is the income period end. Returns are not annualized, including quarterly returns.
- Supported EBITDA definition: net income plus interest expense, income tax expense/benefit, depreciation, and intangible amortization. The query uses the exact US-GAAP concepts `NetIncomeLoss`, `InterestExpense`, `IncomeTaxExpenseBenefit`, `Depreciation`, and `AmortizationOfIntangibleAssets`. A tax benefit remains negative. Expense addbacks must be nonnegative. No combined depletion/accretion tag, issuer-adjusted EBITDA, or absent component is substituted. Many issuers therefore return unavailable under this deliberately narrow definition.
- Discrete quarter: subtract the previous cumulative observation from the current one, requiring the same metric, fiscal start, currency, issuer and cutoff. Supported pairs are six months minus Q1, nine months minus six months, and annual minus nine months. Endpoints must be 70–110 days apart. Both source facts remain in the lineage; source facts are never rewritten.

## Selection and trace

Every request specifies a ticker, reporting basis and exact period end. Comparative calculations also require an explicit comparison period end. `--input-metric` chooses the reported metric for growth/YoY/CAGR/discrete-quarter calculations and defaults to revenue. Other formulas have fixed operands. Currency defaults to USD; no currency conversion occurs.

All operands are selected in one PostgreSQL REPEATABLE READ transaction at one timezone-aware cutoff. Existing fact-selection rules restrict reads to the latest snapshot actually observed by that cutoff and reported availability at or before the cutoff. Restatements create new calculation results; saved earlier results remain unchanged. No source refresh occurs.

Same-period formulas require exactly matching start/end dates and basis. Growth comparisons require equal period basis, non-overlapping durations, and duration lengths within seven days. These fiscal-calendar tolerances are versioned conventions, not an assertion that periods have identical day counts. Actual period dates are retained; no calendar-year conversion or cross-issuer comparison is performed.

Results contain the formula/version, precision, value/unit or unavailable reason, exact operands, dates, cutoff, freshness and original source records (fact IDs, SEC concepts, filing URLs/accessions, snapshot hashes and locators). A missing component remains visible with its own selection failure reason. Zero/negative denominators and incompatible contexts return unavailable.

`financial_metrics` stores the response and NUMERIC value. `metric_inputs` links each named operand to exactly one reported fact or derived metric, enforced by foreign keys and a check constraint. Identical complete requests/input records yield the same content-derived run ID. Changed freshness/source metadata can produce a new record even when the numeric value is unchanged. Unknown issuers return unavailable without creating an issuer or calculation row. Legacy metric rows may lack a response.

Migration `599d1b9a7129` extends the existing tables; it does not rewrite source facts. Downgrade refuses to discard derived-metric dependencies. No free-form research routing or frontend integration is added here; Phase 9 handles query routing.

## CLI

```powershell
uv run --locked --directory backend python -m app.database migrate
uv run --locked --directory backend python -m app.database calculate NVDA net_margin --basis annual --period-end 2026-01-25
uv run --locked --directory backend python -m app.database calculate NVDA yoy --input-metric revenue --basis annual --period-end 2026-01-25 --comparison-end 2025-01-26
uv run --locked --directory backend python -m app.database calculate MSFT fcf --basis annual --period-end 2026-06-30
uv run --locked --directory backend python -m app.database show-calculation <run-id>
```

Use dates present in your stored source snapshots. A missing period yields unavailable rather than an older-period fallback. `--as-of` accepts an ISO timestamp with a timezone.

Formula references: [SEC financial-statement guide](https://www.sec.gov/about/reports-publications/investorpubsbegfinstmtguide) and [SEC non-GAAP interpretations, including EBITDA](https://www.sec.gov/rules-regulations/staff-guidance/corporation-finance-interpretations/non-gaap-financial-measures). FCF and EBITDA are explicitly defined non-GAAP calculations, not universal issuer-reported measures.
