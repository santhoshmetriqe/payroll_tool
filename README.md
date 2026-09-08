# payrol_register

ETL pipeline that turns an **ADP payroll batch** (Payroll Register / Payroll
Summary / Statistical Summary PDFs + the NetSuite journal-entry `.xlsx`) into
per-property tables in the `integration_dev` Postgres database, with a
reconciliation view that checks gross pay, net cash and the JE all tie out.

Every table is prefixed with the property id (`{p}`, e.g. `383_emp_payroll_details`).
Batches processed so far: **383** (Fairfield Inn & Suites Gainesville / `QVJ`),
**421** (Sheraton Chicago Northbrook / `UZM`), **474** (The Sound Hotel Seattle / `PG8`),
two batches each (pay dates 2026-07-04 and 2026-07-18).

## Why the PDFs are parsed by coordinate

ADP's PDF text layer is scrambled, so `payroll/pdf_text.py` rebuilds each visual
row from word x/y positions instead of using `extract_text()`. Money is printed
as space-separated groups whose last group is cents (`1 046 68` → `1046.68`);
`payroll/amounts.py` normalises that, `1,046.68`, and trailing-minus negatives.

## Repository layout

```
payroll/
  amounts.py            ADP money-token + coded-column parsing
  pdf_text.py            word/line reconstruction from PDF coordinates
  db.py                  config.ini / PG_DSN / --dsn connection helpers
  parsers/               schema-v2 PDF parsers
    register.py           Payroll Register  -> per-employee records
    dept_totals.py        "DEPT TOTAL" blocks (authoritative dept-level totals)
    summary.py            Payroll Summary   -> per-department records
    stats.py              Statistical Summary -> batch recap
    company_totals.py     last "Company Totals" page of the Register
  ingest/                schema-v2 load pipeline
    schema.py             CREATE TABLE + GL/department master-mapping loader
    land.py               parse PDFs/xlsx -> {p}_payroll_raw_landing (verbatim)
    promote.py            raw_landing -> curated {p}_* tables
  legacy/                schema-v1 pipeline (superseded; helpers still reused)
  cli/
    ingestor.py           schema-v2 CLI
    etl_legacy.py          schema-v1 CLI

payroll_ingestor.py       thin entry point -> payroll.cli.ingestor
payroll_etl.py            thin entry point -> payroll.cli.etl_legacy   (legacy)
payroll_pdf_parser.py     re-export shim -> payroll.parsers.*          (legacy imports)
```

Not tracked in git (see `.gitignore`), expected locally:

| path | contents |
|------|----------|
| `config.ini` | DB credentials + optional CLI defaults (has a password) |
| `migration/payroll_schema_v2.sql` | schema-v2 DDL (parametrised on `{p}`) |
| `migration/payroll_schema.sql` | schema-v1 DDL |
| `docs/` | ERD notes, ADP↔GL mapping spreadsheets, sample data |
| `Payroll data Request/` | the raw ADP batch folders |

## Requirements

- Python 3.13
- `pdfplumber`, `openpyxl`, `psycopg2-binary`

```
pip install pdfplumber openpyxl psycopg2-binary
```

## Configuration

The DB connection is resolved in this order: `--dsn` → `PG_DSN` env var →
`[database]` section of `config.ini`.

```ini
# config.ini  (gitignored)
[database]
host = <rds-host>
port = 5432
user = postgres
password = <password>
dbname = integration_dev

[defaults]
company_code =
table_prefix =
```

## Running a batch (schema v2)

> `payroll/ingest/schema.py` reads the DDL from `payroll_schema_v2.sql` at the
> repo root. The file currently lives in `migration/` — copy it up before the
> first `--init-schema` run: `cp migration/payroll_schema_v2.sql .`

```bash
# 1. one-time per property: create the {p}_* tables + reconciliation view
python payroll_ingestor.py --property-id 383 --init-schema

# 2. one-time / on change: load the ADP department -> GL -> NetSuite mapping
python payroll_ingestor.py --load-gl-mapping "docs/ADP Master Mapping Working.xlsx"

# 3. per batch: land the source files verbatim into {p}_payroll_raw_landing
python payroll_ingestor.py --property-id 383 --company-code QVJ \
    --batch-number 6908-030 --pay-date 2026-07-04 \
    --register-pdf ".../Payroll register.pdf" \
    --summary-pdf  ".../Payroll Summary.pdf" \
    --stats-pdf    ".../Stat Summary.pdf" \
    --journal-xlsx ".../Payroll.xlsx" \
    --company-totals-pdf ".../Payroll register.pdf"

# 4. per batch: promote raw_landing into the curated tables
python payroll_ingestor.py --property-id 383 --company-code QVJ \
    --batch-number 6908-030 --pay-date 2026-07-04 --promote
```

`--promote` always wipes and rebuilds `{p}_payroll_summary` from the
`company_total` record; if it reports `company_total=0`, re-run with
`--company-totals-pdf` to repopulate it. Pass `--no-replace` to append instead
of delete-then-insert.

After every load, check `{p}_v_payroll_reconciliation`
(`gross_ties` / `net_ties` / `je_balances`).

### Tables produced

| table | grain |
|-------|-------|
| `{p}_payroll_raw_landing` | verbatim parsed JSON, one row per source record |
| `{p}_emp_payroll_details` | one row per employee per batch (header: gross, net, statutory tax) |
| `{p}_emp_pay_rate_details` | one row per rate segment, own `department_code` (for GL allocation) |
| `{p}_emp_deduction_details` | one row per voluntary deduction code per employee |
| `{p}_payroll_summary` | batch-level "Company Totals" |
| `{p}_payroll_summary_by_dept` | per-department totals from the Payroll Summary |
| `{p}_stats_summary` | batch tax/liability recap (standalone) |
| `{p}_payroll_journal` | NetSuite JE lines |
| `adp_gl_dept_mapping` | global: ADP dept → GL account → NetSuite account |

Employee names in the curated `emp_*` tables are masked to `First.Last.`
initials; full names remain only in `raw_landing`.

### Known parser limits

- **Register:** employees who worked multiple departments in one pay period are
  flagged `needs_manual_review` with no rate/deduction rows — the label-to-value
  pairing is not reliably derivable from row order. Their header
  `department_code` is a compound string (`603500/605000`); filter with
  `LIKE '%603500%'`, not `=`.
- **Payroll Summary / `{p}_payroll_summary_by_dept`:** deduction and cafeteria-125
  sub-totals can be wrong — spot-check before trusting them. The register's own
  `DEPT TOTAL` line (captured as `record_type='dept_total'` in `raw_landing`) is
  the number to trust for department-level GL reconciliation.
- Register/stats parsing is verified against batch-1 ground truth.

## Legacy pipeline (schema v1)

`payroll_etl.py` is the original single-folder ETL (`{p}_payroll_register_line` +
`{p}_payroll_summary` at department grain). Superseded by the v2 ingestor, but
kept because its `parse_journal` / `parse_stats` helpers are still used.

```bash
python payroll_etl.py --folder "<batch folder>" --property-id 383 \
    --company-code QVJ --dry-run
```
