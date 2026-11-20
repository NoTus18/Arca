# Decisions

Short notes on design choices and why I made them.

## Transactions are made of legs
Each transaction has one or more legs, and each leg is one movement of cash or
shares. A buy is a shares leg, a cost leg and sometimes a fee leg. One structure
fits buys, dividends, fees and currency conversions from every app, where fixed
columns or a separate conversions table would not.

## DuckDB instead of PostgreSQL
Single user, mostly analytical queries, and the whole database is one file that's
easy to delete and rebuild. No server to run.

## Balances are calculated, not stored
Holdings and cash balances are views over the legs, so they can't drift out of
sync with the transactions.

## Imports rebuild from the raw files
Each import deletes the account's data and recreates it from `data/raw/`.
Overlapping exports can't double count, and fixing an importer bug only needs a
re-run.

## Each leg keeps its own source ID
One IBKR dividend can come from several rows: the payment, its withholding tax
and later corrections. Each leg stores the ID of the row it came from, so every
leg can be traced back to its export, even when one transaction spans several
rows or files.

## transactions.instrument_id
A dividend's income leg is cash, so it can't hold an instrument. This nullable
column records which security a buy or dividend is about.

## No foreign key from transaction_legs to transactions
Each import replaces an account's data inside one database transaction, so a
failed import leaves the old data untouched instead of half an account. DuckDB
can't delete rows that a foreign key points to in the same transaction as the
rows pointing to them, so the import would always fail with the key in place.
`save_transactions` checks for legs without a transaction before committing
instead, and it's the only code that writes legs.

## The instrument reference file stays private
Prices need a ticker for each ISIN, which the exports don't reliably give, so
the mapping is written by hand. It lists every security I hold, so the real
file lives in `data/reference/`, and only an example is in Git.

## close for valuation, adj_close for returns
Prices are downloaded with both. `close` is only adjusted for splits, which is
right for valuing holdings, since dividends are already recorded as cash.
`adj_close` also includes dividends, so using it for valuation would count them
twice. It's kept for comparing total returns later.

## Decimal for money, float only for charts
Amounts are Decimal from the importer to the analysis functions, so sums don't pick
up rounding errors. Plotly needs JSON, so `num` in `dashboard/data.py`
turns values into floats for chart data only, and nothing is added up after
that.

## Imports skip brokers that haven't changed
`run_imports.py` hashes each broker's CSV files and its importer code, and
stores both in `import_runs`. A broker is re-imported only when one of them
changes, or with `--force`. Deleting the database re-imports everything.

## Realised gains at average cost
Each holding has one average price. A sale takes its share of the cost at that
price, and the rest is the realised gain. It's simpler than tracking lots and
enough to see how a holding did.

## The dashboard opens the database read-only, per page load
DuckDB allows one writer per file. The dashboard connects read-only and only
while a page loads, so `update_all.py` can write while the dashboard is open.
If a page loads mid-update, it says the database is busy rather than failing.

## Gaps are filled with the latest value
Markets and the ECB don't publish every day. The daily views use the latest
price and rate on or before each day, and keep `price_date` and `rate_date`, so
it's always visible how old a value is.

## Any combination of accounts
Every view keeps `account_id` and stores no combined totals. Analysis functions
take a list of accounts and add them up at the end, so any combination works,
and percentages are always calculated from the selected accounts.

## Country weights: geographic regions, MSCI market type, Unclassified kept
Funds are looked through to countries with weights in `exposures`. Sectors and
stock countries come from Yahoo Finance on every update; fund countries come
from factsheets in `data/reference/exposures.csv`, because no issuer offers a
stable download (iShares broke its holdings file link in a site redesign).
Each factsheet row keeps its date, and the Data page flags weights older than a
year.

Regions are purely geographic, from `reference/countries.csv`, so Mexico is
Latin America and Taiwan is Asia-Pacific however its market is classed.
Developed vs emerging is a separate view, and it needs one source because index
providers disagree (FTSE calls South Korea and Poland developed, MSCI emerging).
I use MSCI, which reviews its list each June.

Weight a factsheet doesn't list is stored as Unclassified, shown last and in
grey, and never spread over the listed countries: spreading it would invent
data, since a fund's unlisted part is often a different region from its top
ten.
