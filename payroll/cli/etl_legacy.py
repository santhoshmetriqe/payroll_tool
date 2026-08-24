#!/usr/bin/env python3
"""
payroll_etl.py  --  Payroll (P/L) extraction & load pipeline (schema v1, legacy)
==================================================================================

Extracts an ADP payroll batch (register / summary / stats + NetSuite JE) and
loads it into the property data-lake:

    RAW      : "<prop>_payroll_raw_landing"      (verbatim, per record_type)
    CURATED  : "<prop>_payroll_register_line"    (employee x dept)
               "<prop>_payroll_summary"          (department)
               "<prop>_stats_summary"            (pay-run recap)
               "<prop>_payroll_journal"          (GL line)
    RECONCILE: "<prop>_v_payroll_reconciliation" (view)

A batch folder is expected to contain (names matched case-insensitively):
    *register*.pdf          ADP Payroll Register
    *summary*.pdf (Payroll) ADP Payroll Summary
    *stats*.pdf             ADP Statistical Summary
    *.xlsx                  NetSuite payroll journal entry

Usage
-----
    # dry run - parse & validate only, no DB writes
    python payroll_etl.py --folder "<batch folder>" --property-id 383 \
        --company-code QVJ --dry-run

    # load (DB DSN from env PG_DSN or --dsn)
    export PG_DSN="postgresql://postgres:***@host:5432/integration_dev"
    python payroll_etl.py --folder "<batch folder>" --property-id 383 \
        --company-code QVJ

Design notes
------------
* ADP prints money as space-separated groups whose last group is cents
  ("1 046 68" -> 1046.68); totals sometimes use "1,046.68".  adp_amount()
  normalises both, plus trailing-minus ("75 00-" -> -75.00).
* The register text layer is scrambled; we rebuild rows from word x/y
  coordinates (reconstruct_lines) instead of extract_text().
* Every landing row keeps the raw reconstructed lines, so nothing is lost
  even if a structured field fails to parse.
* Load is validated: employee gross must equal the company total, the JE must
  balance (<=0.01), and register==summary==journal gross must agree, or the
  batch is rejected (unless --force).
"""
from __future__ import annotations
import argparse
import os
import sys

from payroll import PROJECT_ROOT
from payroll.legacy.parsers import connect_db, load_config, parse_journal, parse_register, parse_stats, parse_summary
from payroll.legacy.schema import ensure_schema
from payroll.legacy.load import load, validate
from payroll.pdf_text import find_file


def main(argv=None):
    ap = argparse.ArgumentParser(description='Payroll batch ETL')
    ap.add_argument('--folder', required=True, help='batch folder with the ADP files')
    ap.add_argument('--property-id', type=int, required=True)
    ap.add_argument('--company-code', default='')
    ap.add_argument('--batch-number', default='')
    ap.add_argument('--pay-date', default='', help='YYYY-MM-DD')
    ap.add_argument('--table-prefix', default=None, help='table name prefix (default = property id)')
    ap.add_argument('--config', default=str(PROJECT_ROOT / 'config.ini'),
                    help='path to config.ini (DB credentials + defaults)')
    ap.add_argument('--dsn', default=os.environ.get('PG_DSN'),
                    help='override DB connection string (else taken from config.ini)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='load even if validation fails')
    ap.add_argument('--init-schema', action='store_true',
                    help='CREATE TABLE IF NOT EXISTS (+ indexes + recon view) before loading')
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    dflt = cfg.get('defaults', {})
    if not a.company_code and dflt.get('company_code'):
        a.company_code = dflt['company_code']
    if not a.table_prefix and dflt.get('table_prefix'):
        a.table_prefix = dflt['table_prefix']

    folder = a.folder
    files = {
        'register': find_file(folder, 'register', ext='.pdf'),
        'summary':  find_file(folder, 'summary', ext='.pdf') or find_file(folder, 'payroll summary', ext='.pdf'),
        'stats':    find_file(folder, 'stats', ext='.pdf'),
        'journal':  find_file(folder, ext='.xlsx'),
    }
    for k, v in files.items():
        print(f'  {k:9}: {os.path.basename(v) if v else "** NOT FOUND **"}')
    missing = [k for k, v in files.items() if not v]
    if missing:
        sys.exit(f'Missing required files: {missing}')

    print('\nParsing ...')
    journal = parse_journal(files['journal'])
    stats = parse_stats(files['stats'])
    summary = parse_summary(files['summary'])
    register = parse_register(files['register'])
    print(f'  journal lines : {len(journal)}')
    print(f'  summary depts : {len(summary["departments"])}')
    print(f'  register emps : {len(register["employees"])}')

    v = validate(journal, summary, register)
    print('\nValidation:')
    for k in ('je_debit', 'je_credit', 'summary_gross', 'register_gross', 'je_wages'):
        print(f'  {k:15}: {v[k]}')
    if v['issues']:
        print('  ISSUES:')
        for i in v['issues']:
            print('   -', i)
    else:
        print('  all reconciliation checks passed')

    meta = {
        'property_id': a.property_id, 'company_code': a.company_code,
        'batch_number': a.batch_number, 'pay_date': a.pay_date or None,
        'files': {k: os.path.basename(vv) for k, vv in files.items()},
    }
    if a.pay_date:
        y, m, _ = a.pay_date.split('-')
        meta['year'], meta['month'] = int(y), int(m)
    else:
        meta['year'], meta['month'] = 0, 0

    if a.dry_run:
        print('\n[dry-run] no DB writes.')
        return
    if v['issues'] and not a.force:
        sys.exit('\nValidation failed; refusing to load (use --force to override).')
    from payroll.legacy.parsers import psycopg2
    if psycopg2 is None:
        sys.exit('psycopg2 not installed.')
    prop = a.table_prefix or str(a.property_id)
    conn = connect_db(a, cfg)
    if a.init_schema:
        ensure_schema(conn, prop)
        print(f'\nSchema ensured for prefix "{prop}".')
    load(conn, prop, meta, journal, stats, summary, register)
    print('Loaded to DB.')


if __name__ == '__main__':
    main()
