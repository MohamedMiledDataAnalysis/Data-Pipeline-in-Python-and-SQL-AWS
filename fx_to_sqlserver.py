"""
fx_to_sqlserver.py
------------------
A simple ETL script in five steps:

    1. EXTRACT    daily exchange rates from a public API (JSON)
    2. TRANSFORM  the JSON into a pandas DataFrame
    3. CHECK      data quality: data types first, then business rules;
                  stop if the data is not fit to load
    4. LOAD       the data into a SQL Server table, using credentials
    5. EXPORT     the same data to AWS S3 as CSV, Excel and JSON files

Steps 4 and 5 are each optional: a step runs when its settings are present
(SQL_SERVER for step 4, S3_BUCKET for step 5). At least one must be set.

Source: Frankfurter API (European Central Bank reference rates), free, no API key.

Credentials are NEVER written in the code. They are read from environment
variables (or from a .env file next to the script):

    SQL Server (step 4)
    SQL_SERVER             e.g. localhost, myserver.database.windows.net
    SQL_DATABASE           e.g. FxRates
    SQL_USER               leave empty to use Windows authentication
    SQL_PASSWORD
    SQL_DRIVER             default: ODBC Driver 18 for SQL Server

    AWS S3 (step 5)
    S3_BUCKET              e.g. my-company-data-lake
    S3_PREFIX              folder inside the bucket, default: fx-rates
    AWS_REGION             e.g. eu-west-2
    AWS_ACCESS_KEY_ID      or an AWS profile / IAM role: boto3 finds the
    AWS_SECRET_ACCESS_KEY  credentials through the standard AWS chain

Install:  pip install requests pandas sqlalchemy pyodbc boto3 openpyxl
Run:      python fx_to_sqlserver.py
"""

import io
import numbers
import os
import sys
from datetime import date, datetime, timedelta

import boto3
import numpy as np
import pandas as pd
import requests
from botocore.exceptions import ClientError, NoCredentialsError
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
API_URL = "https://api.frankfurter.dev/v1"
BASE_CURRENCY = "EUR"
CURRENCIES = ["USD", "GBP", "CHF", "JPY", "CAD"]
END_DATE = date.today()
START_DATE = END_DATE - timedelta(days=60)   # the API returns every business day for up to 90 days
TABLE_NAME = "dbo.fx_rate"


# ---------------------------------------------------------------------------
# SQL statements (T-SQL for SQL Server)
# ---------------------------------------------------------------------------
# Create the table only if it does not exist yet. OBJECT_ID returns NULL when
# no user table ('U') has that name, so existing data is never touched.
# The primary key stops duplicates at database level, as a second line of
# defence behind the quality checks.
CREATE_TABLE_SQL = f"""
IF OBJECT_ID(N'{TABLE_NAME}', N'U') IS NULL
BEGIN
    CREATE TABLE {TABLE_NAME} (
        rate_date       DATE        NOT NULL,
        base_currency   VARCHAR(3)  NOT NULL,
        quote_currency  VARCHAR(3)  NOT NULL,
        rate            FLOAT       NOT NULL,
        CONSTRAINT PK_fx_rate PRIMARY KEY (rate_date, base_currency, quote_currency)
    );
END
"""

# Remove the rows of the period being reloaded, for the requested currencies only.
# :start_date, :end_date, :base and :currencies are parameters: values are sent
# separately from the SQL text, which prevents SQL injection.
DELETE_SQL = text(f"""
DELETE FROM {TABLE_NAME}
WHERE rate_date BETWEEN :start_date AND :end_date
  AND base_currency = :base
  AND quote_currency IN :currencies
""").bindparams(bindparam("currencies", expanding=True))   # expands the list to IN (?, ?, ?, ...)

# Insert one row. Executed with a list of rows, it becomes a bulk insert.
INSERT_SQL = text(f"""
INSERT INTO {TABLE_NAME} (rate_date, base_currency, quote_currency, rate)
VALUES (:rate_date, :base_currency, :quote_currency, :rate)
""")


# ---------------------------------------------------------------------------
# 1. EXTRACT
# ---------------------------------------------------------------------------
def extract() -> dict:
    """Call the API and return the JSON response as a Python dictionary."""
    url = f"{API_URL}/{START_DATE}..{END_DATE}"
    params = {"base": BASE_CURRENCY, "symbols": ",".join(CURRENCIES)}
    print(f"[1/5] Extracting {url}")

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()          # stop with an error on 4xx/5xx instead of parsing an error page
    data = response.json()

    # Response shape: {"base": "EUR", "rates": {"2026-10-01": {"USD": 1.09, "GBP": 0.84}, ...}}
    print(f"      {len(data.get('rates', {}))} dates received")
    return data


# ---------------------------------------------------------------------------
# 2. TRANSFORM
# ---------------------------------------------------------------------------
def transform(data: dict) -> pd.DataFrame:
    """Flatten the nested JSON into one row per (date, currency)."""
    print("[2/5] Transforming JSON into a DataFrame")

    rows = []
    for rate_date, quotes in data.get("rates", {}).items():   # one entry per date
        for currency, rate in quotes.items():                  # one entry per currency
            rows.append({
                "rate_date": rate_date,
                "base_currency": data.get("base"),
                "quote_currency": currency,
                "rate": rate,
            })
    # The values are kept exactly as the API sent them. They are converted to
    # their final types only after the quality step has checked those types.
    df = pd.DataFrame(rows, columns=["rate_date", "base_currency", "quote_currency", "rate"])

    print(f"      {len(df)} rows, {df['quote_currency'].nunique()} currencies")
    return df


# ---------------------------------------------------------------------------
# 3. DATA QUALITY
# ---------------------------------------------------------------------------
def is_date_text(value) -> bool:
    """True if value is text in the format YYYY-MM-DD and is a real calendar date."""
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")   # rejects "2026-02-30", "20/10/2026", "2026-10-20T00:00"
        return True
    except ValueError:
        return False


def is_number(value) -> bool:
    """True if value is an integer or a decimal number.

    A JSON true/false is rejected: Python treats bool as a kind of int, but a
    boolean is not an exchange rate. Text such as "1.09" is also rejected:
    the API contract says rates are numbers, so text means the source changed.
    """
    return isinstance(value, numbers.Real) and not isinstance(value, (bool, np.bool_))


def is_text(value) -> bool:
    """True if value is a string."""
    return isinstance(value, str)


# Expected type of every column: (rule applied to each value, description for the report).
EXPECTED_TYPES = {
    "rate_date":      (is_date_text, "text YYYY-MM-DD"),
    "base_currency":  (is_text,      "text"),
    "quote_currency": (is_text,      "text"),
    "rate":           (is_number,    "a number"),
}

# Column types the DataFrame must have after conversion, matching the SQL table:
# (test on the whole column, description for the report).
SQL_COMPATIBLE_DTYPES = {
    "rate_date":      (lambda s: s.map(lambda v: isinstance(v, date)).all(), "date"),   # -> SQL DATE
    "base_currency":  (pd.api.types.is_string_dtype,                         "string"), # -> SQL VARCHAR(3)
    "quote_currency": (pd.api.types.is_string_dtype,                         "string"),
    "rate":           (pd.api.types.is_float_dtype,                          "float"),  # -> SQL FLOAT
}


def report(checks: list[tuple[str, int]]) -> bool:
    """Print one line per check and return True if every check passed (0 failures)."""
    for name, failed in checks:
        status = "PASS" if failed == 0 else f"FAIL ({failed})"
        print(f"      {status:<10} {name}")
    return all(failed == 0 for _, failed in checks)


def convert_types(df: pd.DataFrame) -> pd.DataFrame:
    """Convert the checked raw values to the types of the SQL table."""
    typed = df.copy()
    typed["rate_date"] = pd.to_datetime(typed["rate_date"], format="%Y-%m-%d").dt.date
    typed["base_currency"] = typed["base_currency"].astype(str).str.strip().str.upper()
    typed["quote_currency"] = typed["quote_currency"].astype(str).str.strip().str.upper()
    typed["rate"] = typed["rate"].astype("float64")
    return typed


def check_quality(df: pd.DataFrame) -> pd.DataFrame | None:
    """Run the checks and print a report.

    Returns the DataFrame converted to the SQL types if every check passed,
    or None if the data must not be loaded.
    """
    print("[3/5] Checking data quality")

    # --- A. Structure and data types (on the raw values) -------------------------
    print("      -- Structure and data types")
    checks = [
        ("Dataset is not empty", 0 if len(df) else 1),
        ("All expected columns present", len(set(EXPECTED_TYPES) - set(df.columns))),
        ("No missing values", int(df.isna().any(axis=1).sum())),
    ]
    bad_examples = []
    for column, (rule, description) in EXPECTED_TYPES.items():
        if column in df.columns:
            # Missing values are counted by the check above, so they are skipped here.
            wrong = df[column].notna() & ~df[column].map(rule)
            checks.append((f"{column} is {description}", int(wrong.sum())))
            # Keep up to 3 distinct offending values with their Python type, to help the investigation.
            if wrong.any():
                examples = [f"{v!r} ({type(v).__name__})" for v in df.loc[wrong, column].drop_duplicates().head(3)]
                bad_examples.append(f"bad values in {column}: {', '.join(examples)}")

    # Business rules need correct types (you cannot compare "abc" with 0),
    # so they run only if every structure and type check passed.
    passed = report(checks)
    for line in bad_examples:
        print(f"      {'':<10} {line}")
    if not passed:
        print("      Business rules skipped: fix the structure and data types first.")
        return None

    typed = convert_types(df)

    # After conversion, confirm each column has the type the SQL table expects.
    dtype_checks = [
        (f"{column} converted to {description}", 0 if test(typed[column]) else 1)
        for column, (test, description) in SQL_COMPATIBLE_DTYPES.items()
    ]
    if not report(dtype_checks):
        return None

    # --- B. Business rules (on the typed values) ------------------------------------
    print("      -- Business rules")
    key = ["rate_date", "base_currency", "quote_currency"]
    dates = pd.to_datetime(typed["rate_date"])
    rules = [
        ("Rate is strictly positive",         int((typed["rate"] <= 0).sum())),
        ("Currency code has 3 letters",       int((~typed["quote_currency"].str.fullmatch(r"[A-Z]{3}")).sum())),
        ("Base currency is " + BASE_CURRENCY, int((typed["base_currency"] != BASE_CURRENCY).sum())),
        ("No duplicate date/currency",        int(typed.duplicated(subset=key).sum())),
        ("Date within requested period",      int(((typed["rate_date"] < START_DATE) | (typed["rate_date"] > END_DATE)).sum())),
        ("Date is a weekday",                 int((dates.dt.dayofweek >= 5).sum())),   # Saturday = 5, Sunday = 6
        ("All currencies present",            len(set(CURRENCIES) - set(typed["quote_currency"]))),
    ]
    if not report(rules):
        return None

    # Plausibility (warning only): a daily move above 5% is rare but can be genuine.
    moves = typed.sort_values(key).groupby("quote_currency")["rate"].pct_change().abs()
    if (moves > 0.05).any():
        print(f"      WARNING    {int((moves > 0.05).sum())} daily move(s) above 5%, please review")

    return typed


# ---------------------------------------------------------------------------
# 4. LOAD
# ---------------------------------------------------------------------------
def load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from a .env file into the environment (if the file exists)."""
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                # Empty values (e.g. "AWS_ACCESS_KEY_ID=") are skipped, so the variable
                # stays unset and boto3 falls back to the AWS profile or IAM role.
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip())


def get_engine():
    """Build the SQL Server connection from environment variables."""
    load_dotenv()
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    if not server or not database:
        sys.exit("Set SQL_SERVER and SQL_DATABASE (environment variables or .env file).")

    query = {
        "driver": os.getenv("SQL_DRIVER", "ODBC Driver 18 for SQL Server"),
        "TrustServerCertificate": "yes",   # accept a self-signed certificate (local or dev server)
    }
    user = os.getenv("SQL_USER")
    if not user:
        query["Trusted_Connection"] = "yes"   # Windows authentication: no user name or password

    # URL.create escapes special characters in the password (@, #, / ...) safely.
    url = URL.create(
        "mssql+pyodbc",
        username=user or None,
        password=os.getenv("SQL_PASSWORD") or None,
        host=server,
        database=database,
        query=query,
    )
    # fast_executemany sends the inserts in batches: much faster on SQL Server.
    return create_engine(url, fast_executemany=True)


def load(df: pd.DataFrame, engine) -> None:
    """Create the table if needed, replace the period's rows, insert the new ones."""
    print(f"[4/5] Loading into SQL Server table {TABLE_NAME}")

    # engine.begin() opens ONE transaction, committed at the end of the block.
    # If any statement fails, everything is rolled back: the old data is never lost.
    with engine.begin() as conn:
        # 1. Create the table on the first run (see CREATE_TABLE_SQL at the top).
        conn.execute(text(CREATE_TABLE_SQL))

        # 2. Delete the period being reloaded, so running the script twice
        #    never creates duplicates.
        conn.execute(DELETE_SQL, {
            "start_date": df["rate_date"].min(),
            "end_date": df["rate_date"].max(),
            "base": BASE_CURRENCY,
            "currencies": CURRENCIES,
        })

        # 3. Insert the new rows: a list of dictionaries, one per row.
        conn.execute(INSERT_SQL, df.to_dict(orient="records"))

    print(f"      {len(df)} rows loaded")


# ---------------------------------------------------------------------------
# 5. EXPORT TO AWS S3
# ---------------------------------------------------------------------------
# Each writer turns the DataFrame into the bytes of one file, in memory:
# nothing is written to the local disk.

def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """CSV with a header row, dates as YYYY-MM-DD. utf-8 is the standard for data lakes."""
    export = df.assign(rate_date=pd.to_datetime(df["rate_date"]).dt.strftime("%Y-%m-%d"))
    return export.to_csv(index=False).encode("utf-8")


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    """Excel workbook with one sheet. Dates are kept as real dates, so Excel can sort and filter them."""
    buffer = io.BytesIO()                                   # an in-memory file
    # rate_date holds Python date objects: openpyxl writes them as Excel dates
    # with the yyyy-mm-dd display format.
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="fx_rates", index=False)
    return buffer.getvalue()


def to_json_bytes(df: pd.DataFrame) -> bytes:
    """JSON array of records: [{"rate_date": "2026-10-01", "base_currency": "EUR", ...}, ...]."""
    export = df.assign(rate_date=pd.to_datetime(df["rate_date"]).dt.strftime("%Y-%m-%d"))
    return export.to_json(orient="records", indent=2).encode("utf-8")


# Format -> (writer function, file extension, content type shown by S3 and browsers).
EXPORT_FORMATS = {
    "csv":   (to_csv_bytes,   "csv",  "text/csv"),
    "excel": (to_excel_bytes, "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "json":  (to_json_bytes,  "json", "application/json"),
}


def export_to_s3(df: pd.DataFrame) -> None:
    """Upload the data to S3 once per format.

    Files are organised by format and load date (a common data lake layout,
    which tools such as AWS Athena and Glue can read as partitions):

        s3://<bucket>/fx-rates/format=csv/load_date=2026-10-20/fx_rates_EUR_2026-08-21_2026-10-20.csv
        s3://<bucket>/fx-rates/format=excel/load_date=2026-10-20/fx_rates_EUR_2026-08-21_2026-10-20.xlsx
        s3://<bucket>/fx-rates/format=json/load_date=2026-10-20/fx_rates_EUR_2026-08-21_2026-10-20.json

    Running the script twice on the same day overwrites the same files, so no duplicates.
    """
    bucket = os.getenv("S3_BUCKET")
    prefix = os.getenv("S3_PREFIX", "fx-rates").strip("/")
    print(f"[5/5] Exporting to AWS S3 bucket {bucket}")

    # boto3 finds the credentials itself, in this order: environment variables
    # (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), the ~/.aws/credentials profile,
    # then the IAM role of the machine (EC2, Lambda, ECS). No key in the code.
    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION") or None)

    file_name = f"fx_rates_{BASE_CURRENCY}_{df['rate_date'].min()}_{df['rate_date'].max()}"
    load_date = date.today().isoformat()

    try:
        for fmt, (writer, extension, content_type) in EXPORT_FORMATS.items():
            body = writer(df)
            key = f"{prefix}/format={fmt}/load_date={load_date}/{file_name}.{extension}"
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                ServerSideEncryption="AES256",   # encrypt the file at rest in S3
            )
            print(f"      s3://{bucket}/{key}  ({len(body):,} bytes)")
    except NoCredentialsError:
        sys.exit("No AWS credentials found: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, "
                 "or configure an AWS profile.")
    except ClientError as error:
        # e.g. NoSuchBucket, AccessDenied, InvalidAccessKeyId
        code = error.response["Error"]["Code"]
        sys.exit(f"S3 upload failed ({code}): {error.response['Error'].get('Message', '')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    load_dotenv()                          # read .env before checking which destinations are set
    to_sql = bool(os.getenv("SQL_SERVER"))
    to_s3 = bool(os.getenv("S3_BUCKET"))
    if not (to_sql or to_s3):
        sys.exit("No destination set: define SQL_SERVER and/or S3_BUCKET "
                 "(environment variables or .env file).")

    data = extract()
    df = transform(data)

    typed_df = check_quality(df)           # None if any check failed
    if typed_df is None:
        sys.exit("Data quality checks failed: nothing loaded.")   # exit code 1

    # The same checked data goes to every destination.
    if to_sql:
        load(typed_df, get_engine())
    else:
        print("[4/5] SQL Server skipped (SQL_SERVER not set)")

    if to_s3:
        export_to_s3(typed_df)
    else:
        print("[5/5] AWS S3 skipped (S3_BUCKET not set)")

    print("Done.")


if __name__ == "__main__":
    main()
