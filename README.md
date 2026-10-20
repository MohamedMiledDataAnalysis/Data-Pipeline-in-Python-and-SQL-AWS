# FX Rates to SQL Server: a simple ETL script with data quality checks

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![SQL Server](https://img.shields.io/badge/database-SQL%20Server-red)
![Licence](https://img.shields.io/badge/licence-MIT-green)

A single Python script that extracts daily exchange rates from a public API, transforms the JSON into a pandas DataFrame, checks data quality, and loads the data into Microsoft SQL Server using credentials kept outside the code.

## What it does

```
 Frankfurter API (ECB exchange rates)
        │
        │  1. EXTRACT     HTTP GET, JSON response
        ▼
 Nested JSON  {"2026-10-01": {"USD": 1.09, "GBP": 0.84}, ...}
        │
        │  2. TRANSFORM   one row per date and currency
        ▼
 pandas DataFrame
        │
        │  3. CHECK       data types first, then business rules
        │                 any failure → stop, nothing loaded
        ▼
 Typed DataFrame
        │
        │  4. LOAD        one transaction: delete the period, insert the rows
        ▼
 SQL Server table dbo.fx_rate
```

By default the script loads the last 60 days of euro exchange rates against USD, GBP, CHF, JPY and CAD.

## Data quality checks

The checks run in two stages. Business rules need correct types (you cannot test whether the text `"abc"` is above zero), so they run only when every type check has passed.

**Stage A: structure and data types**

| Check | Rejects, for example |
|---|---|
| Dataset is not empty | an empty API response |
| All expected columns present | a renamed field |
| No missing values | `null` for a rate |
| `rate_date` is text YYYY-MM-DD and a real date | `"2026-02-30"`, `"20/10/2026"` |
| `base_currency` and `quote_currency` are text | `123` |
| `rate` is a number | `"1.09"` (text), `true` (boolean) |
| Each column converts to its SQL type | date, string, string, float |

When a type check fails, the report shows up to three offending values with their Python type, for example `bad values in rate: '1.09' (str), True (bool)`.

**Stage B: business rules**

| Check | Why |
|---|---|
| Rate is strictly positive | an exchange rate is a price |
| Currency code has 3 capital letters | ISO 4217 format |
| Base currency is EUR | a rate against another base would be wrong by a full exchange rate |
| No duplicate date and currency | one rate per currency per day |
| Date within the requested period | the API must return what was asked for |
| Date is a weekday | the European Central Bank does not publish at weekends |
| All requested currencies present | a missing currency means the extraction is broken |
| Daily move above 5% | **warning only**: such a move is rare but can be genuine |

If any check fails (warnings excepted), the script stops with exit code 1 and nothing is written to the database.

## Requirements

- Python 3.10 or later
- Microsoft [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
- A SQL Server database (local, on a server, or Azure SQL Database) and a login allowed to create a table and insert rows

## Installation

```bash
git clone https://github.com/YOUR-USERNAME/fx-to-sqlserver.git
cd fx-to-sqlserver
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration: credentials

Credentials are never written in the code. The script reads them from environment variables, or from a `.env` file in the same folder.

1. Copy the template:
   ```bash
   cp .env.example .env            # Windows: copy .env.example .env
   ```
2. Edit `.env`:
   ```ini
   SQL_SERVER=localhost
   SQL_DATABASE=FxRates
   SQL_USER=my_login
   SQL_PASSWORD=my_password
   SQL_DRIVER=ODBC Driver 18 for SQL Server
   ```

| Variable | Required | Notes |
|---|---|---|
| `SQL_SERVER` | yes | e.g. `localhost`, `myserver.database.windows.net` |
| `SQL_DATABASE` | yes | the database must already exist |
| `SQL_USER` | no | leave empty to use **Windows authentication** |
| `SQL_PASSWORD` | with `SQL_USER` | special characters (`@`, `#`, `/`) are escaped automatically |
| `SQL_DRIVER` | no | default: `ODBC Driver 18 for SQL Server` |

`.env` is listed in `.gitignore`, so your password is never committed to GitHub. A variable set in the real environment takes precedence over the same variable in `.env`.

## Usage

```bash
python fx_to_sqlserver.py
```

Example output:

```
[1/4] Extracting https://api.frankfurter.dev/v1/2026-08-21..2026-10-20
      43 dates received
[2/4] Transforming JSON into a DataFrame
      215 rows, 5 currencies
[3/4] Checking data quality
      -- Structure and data types
      PASS       Dataset is not empty
      PASS       All expected columns present
      PASS       No missing values
      PASS       rate_date is text YYYY-MM-DD
      PASS       base_currency is text
      PASS       quote_currency is text
      PASS       rate is a number
      PASS       rate_date converted to date
      PASS       base_currency converted to string
      PASS       quote_currency converted to string
      PASS       rate converted to float
      -- Business rules
      PASS       Rate is strictly positive
      PASS       Currency code has 3 letters
      PASS       Base currency is EUR
      PASS       No duplicate date/currency
      PASS       Date within requested period
      PASS       Date is a weekday
      PASS       All currencies present
[4/4] Loading into SQL Server table dbo.fx_rate
      215 rows loaded
Done.
```

To change the period or the currencies, edit the settings at the top of the script:

```python
BASE_CURRENCY = "EUR"
CURRENCIES = ["USD", "GBP", "CHF", "JPY", "CAD"]
END_DATE = date.today()
START_DATE = END_DATE - timedelta(days=60)   # keep the period within 90 days
TABLE_NAME = "dbo.fx_rate"
```

Keep the period within 90 days: for longer periods the API returns weekly instead of daily rates.

## The SQL table

All the SQL is written in T-SQL at the top of the script, in three constants: `CREATE_TABLE_SQL`, `DELETE_SQL` and `INSERT_SQL`. The `load()` function runs them in that order.

**1. Create the table on the first run** (`CREATE_TABLE_SQL`):

```sql
IF OBJECT_ID(N'dbo.fx_rate', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fx_rate (
        rate_date       DATE        NOT NULL,
        base_currency   VARCHAR(3)  NOT NULL,
        quote_currency  VARCHAR(3)  NOT NULL,
        rate            FLOAT       NOT NULL,
        CONSTRAINT PK_fx_rate PRIMARY KEY (rate_date, base_currency, quote_currency)
    );
END
```

`OBJECT_ID` returns `NULL` when no table has that name, so the table is created once and existing data is never touched. The primary key stops duplicates at database level, as a second line of defence behind the quality checks.

**2. Delete the period being reloaded** (`DELETE_SQL`), then **3. insert the new rows** (`INSERT_SQL`):

```sql
DELETE FROM dbo.fx_rate
WHERE rate_date BETWEEN :start_date AND :end_date
  AND base_currency = :base
  AND quote_currency IN :currencies;

INSERT INTO dbo.fx_rate (rate_date, base_currency, quote_currency, rate)
VALUES (:rate_date, :base_currency, :quote_currency, :rate);
```

The `:name` markers are parameters: the values are sent to SQL Server separately from the SQL text, which prevents SQL injection. The insert runs once with the full list of rows, as a bulk insert (`fast_executemany`).

**Re-running is safe.** The three statements run in one transaction. Running the script twice gives the same table, never twice the rows. If any statement fails, the whole transaction is rolled back and the existing data is kept.

Example queries:

```sql
-- Latest rate per currency
SELECT quote_currency, rate_date, rate
FROM (
    SELECT quote_currency, rate_date, rate,
           ROW_NUMBER() OVER (PARTITION BY quote_currency ORDER BY rate_date DESC) AS rn
    FROM dbo.fx_rate
) AS latest
WHERE rn = 1;

-- Day-on-day change in %
SELECT quote_currency, rate_date, rate,
       100.0 * (rate - LAG(rate) OVER (PARTITION BY quote_currency ORDER BY rate_date))
             / LAG(rate) OVER (PARTITION BY quote_currency ORDER BY rate_date) AS pct_change
FROM dbo.fx_rate
ORDER BY quote_currency, rate_date;
```

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `Set SQL_SERVER and SQL_DATABASE` | `.env` is missing or not in the folder you run the script from |
| `Can't open lib 'ODBC Driver 18 for SQL Server'` | install the ODBC driver, or set `SQL_DRIVER` to the version installed (e.g. `ODBC Driver 17 for SQL Server`) |
| `Login failed for user` | wrong user or password, or SQL authentication is disabled on the server |
| `Cannot open database "FxRates"` | create the database first: `CREATE DATABASE FxRates;` |
| `Data quality checks failed: nothing loaded.` | read the FAIL lines above it: they name the check and show the offending values |

## Project structure

```
fx-to-sqlserver/
├── fx_to_sqlserver.py   the ETL script: extract, transform, check, load
├── requirements.txt     Python dependencies
├── .env.example         credentials template (copy to .env)
├── .gitignore           keeps .env and local files out of git
├── LICENSE
└── README.md
```

## Data source

[Frankfurter](https://frankfurter.dev) is a free, open-source API serving the European Central Bank's euro foreign exchange reference rates, published around 16:00 CET on each working day. No API key is required.

## Author

**Mohamed Miled** — data analyst and engineer, twelve years in structured finance and risk data (DBRS Morningstar, Lombard Odier, US Bank, Sfil). MSc Mathematics, Université Pierre-et-Marie-Curie.

## Licence

MIT
