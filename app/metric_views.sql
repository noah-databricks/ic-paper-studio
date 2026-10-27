-- Draft Unity Catalog metric views for the Quillwood IC paper demo.
-- Replace ${catalog}.${schema} before running. Tables are loaded from content/out/structured/*.csv.

CREATE OR REPLACE VIEW ${catalog}.${schema}.mv_deal_metrics
WITH METRICS
LANGUAGE YAML
AS $$
version: 1.1
comment: "Credit metrics for real estate deals: leverage, coverage and equity. One row per deal in the source table."
source: ${catalog}.${schema}.deals
dimensions:
  - name: Deal ID
    expr: deal_id
  - name: Deal
    expr: deal_name
    synonyms: ["project", "deal name"]
  - name: Sponsor
    expr: sponsor
  - name: Sub-sector
    expr: sub_sector
  - name: State
    expr: state
  - name: Status
    expr: status
  - name: Lifecycle stage
    expr: lifecycle_stage
    comment: "screening, ic or variation"
measures:
  - name: Facility limit
    expr: SUM(facility_limit)
    format: {type: currency, currency_code: AUD}
  - name: Peak debt
    expr: SUM(peak_debt)
    format: {type: currency, currency_code: AUD}
  - name: Total development cost
    expr: SUM(tdc)
    format: {type: currency, currency_code: AUD}
  - name: As if complete value
    expr: SUM(as_if_complete_value)
    format: {type: currency, currency_code: AUD}
  - name: LVR
    expr: SUM(facility_limit) / NULLIF(SUM(as_if_complete_value), 0)
    comment: "Loan to value ratio: facility limit over as-if-complete valuation."
    synonyms: ["loan to value", "LVR"]
    format: {type: percentage, decimal_places: {type: exact, places: 1}}
  - name: LTC
    expr: SUM(facility_limit) / NULLIF(SUM(tdc), 0)
    comment: "Loan to cost ratio: facility limit over total development cost."
    synonyms: ["loan to cost", "LTC"]
    format: {type: percentage, decimal_places: {type: exact, places: 1}}
  - name: Equity percentage
    expr: SUM(total_equity) / NULLIF(SUM(tdc), 0)
    synonyms: ["equity contribution", "equity %"]
    format: {type: percentage, decimal_places: {type: exact, places: 1}}
  - name: Stabilised ICR
    expr: SUM(stabilised_noi) / NULLIF(SUM(peak_debt * refi_rate_assumed), 0)
    comment: "Projected stabilised interest cover: NOI over interest on peak debt at the assumed refinance rate."
    synonyms: ["ICR", "interest cover"]
    format: {type: number, decimal_places: {type: exact, places: 2}}
$$;

CREATE OR REPLACE VIEW ${catalog}.${schema}.mv_portfolio_exposure
WITH METRICS
LANGUAGE YAML
AS $$
version: 1.1
comment: "Quillwood loan book exposure by sector, state and sponsor, including pipeline deals."
source: ${catalog}.${schema}.portfolio_exposure
dimensions:
  - name: Sector
    expr: sector
  - name: Sub-sector
    expr: sub_sector
  - name: State
    expr: state
  - name: Sponsor
    expr: sponsor
  - name: Fund
    expr: fund
  - name: Status
    expr: status
    comment: "Performing or Pipeline."
measures:
  - name: Committed exposure
    expr: SUM(facility_limit)
    format: {type: currency, currency_code: AUD}
  - name: Drawn exposure
    expr: SUM(drawn_balance)
    format: {type: currency, currency_code: AUD}
  - name: Loan count
    expr: COUNT(1)
  - name: Concentration
    expr: SUM(facility_limit) / MAX(book_total_commitments)
    comment: "Share of total committed exposure (including pipeline) for the current grouping. book_total_commitments is the whole-book total repeated on every row."
    synonyms: ["concentration %", "share of book"]
    format: {type: percentage, decimal_places: {type: exact, places: 1}}
$$;

CREATE OR REPLACE VIEW ${catalog}.${schema}.mv_corporate_credit
WITH METRICS
LANGUAGE YAML
AS $$
version: 1.1
comment: "Credit metrics for corporate borrowers: EBITDA, net debt, leverage and interest cover (pro forma for new deals, LTM for existing loans)."
source: ${catalog}.${schema}.deals
filter: sector = 'Corporate'
dimensions:
  - name: Deal ID
    expr: deal_id
  - name: Deal
    expr: deal_name
  - name: Sector
    expr: sector
  - name: Sub-sector
    expr: sub_sector
  - name: Lifecycle stage
    expr: lifecycle_stage
  - name: Status
    expr: status
measures:
  - name: EBITDA
    expr: SUM(ebitda)
    format: {type: currency, currency_code: AUD}
  - name: Net debt
    expr: SUM(net_debt)
    format: {type: currency, currency_code: AUD}
  - name: Leverage
    expr: SUM(net_debt) / NULLIF(SUM(ebitda), 0)
    comment: "Net debt over EBITDA (times)."
    synonyms: ["net leverage", "net debt / EBITDA"]
    format: {type: number, decimal_places: {type: exact, places: 2}}
  - name: Interest cover
    expr: SUM(ebitda) / NULLIF(SUM(interest_expense), 0)
    comment: "EBITDA over interest expense (times)."
    synonyms: ["ICR", "interest coverage"]
    format: {type: number, decimal_places: {type: exact, places: 2}}
  - name: Facility limit
    expr: SUM(facility_limit)
    format: {type: currency, currency_code: AUD}
$$;
