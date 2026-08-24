#!/usr/bin/env python3
"""
payroll_ingestor.py -- schema v2 ingestor for the {p}_emp_payroll_details /
{p}_emp_pay_rate_details / {p}_emp_deduction_details / {p}_payroll_summary /
{p}_payroll_summary_by_dept redesign.

Two jobs, meant to be re-run for every future batch:

1. PROMOTE: {p}_payroll_raw_landing already holds the full parsed JSON for
   register/summary/stats/journal (however it got landed). This reads that
   JSON and populates the new curated tables -- see payroll.ingest.promote.

2. LAND + PROMOTE the "Company Totals" page (last page of the ADP Payroll
   Register PDF) into {p}_payroll_summary -- see payroll.ingest.land and
   payroll.parsers.company_totals.

Run:
    python payroll_ingestor.py --init-schema
    python payroll_ingestor.py --load-gl-mapping "C:\\...\\ADP Master Mapping Working.xlsx"
    python payroll_ingestor.py --property-id 383 --company-code QVJ \
        --batch-number 6908-030 --pay-date 2026-07-04 \
        --company-totals-pdf "<path to ...Payroll register.pdf>" \
        --promote
"""
import argparse
import sys
from pathlib import Path

from payroll import PROJECT_ROOT
from payroll.db import connect_db, load_config
from payroll.ingest.land import land_and_promote_company_totals, land_batch_from_pdfs, land_journal
from payroll.ingest.promote import (
    promote_company_total, promote_journal, promote_register, promote_stats, promote_summary_by_dept,
)
from payroll.ingest.schema import ensure_schema, load_gl_mapping_from_excel


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dsn')
    ap.add_argument('--config', default=str(PROJECT_ROOT / 'config.ini'))
    ap.add_argument('--property-id', type=int)
    ap.add_argument('--company-code', default='')
    ap.add_argument('--batch-number')
    ap.add_argument('--pay-date')
    ap.add_argument('--init-schema', action='store_true')
    ap.add_argument('--load-gl-mapping', metavar='XLSX_PATH')
    ap.add_argument('--company-totals-pdf', metavar='PDF_PATH')
    ap.add_argument('--register-pdf', metavar='PDF_PATH', help='land register data from a fresh PDF (payroll_pdf_parser)')
    ap.add_argument('--summary-pdf', metavar='PDF_PATH', help='land summary data from a fresh PDF (payroll_pdf_parser)')
    ap.add_argument('--stats-pdf', metavar='PDF_PATH', help='land stats data from a fresh PDF (payroll_pdf_parser)')
    ap.add_argument('--journal-xlsx', metavar='XLSX_PATH', help='land journal data from the NetSuite JE xlsx')
    ap.add_argument('--promote', action='store_true',
                     help='promote register/summary/stats/journal from raw_landing into curated tables')
    ap.add_argument('--no-replace', action='store_true', help='append instead of delete-then-insert')
    args = ap.parse_args()

    cfg = load_config(args.config)
    conn = connect_db(args, cfg)
    prop = str(args.property_id) if args.property_id else None
    replace = not args.no_replace

    if args.init_schema:
        if not prop:
            sys.exit('--init-schema requires --property-id')
        ensure_schema(conn, prop)
        print(f'schema ensured for property {prop}')

    if args.load_gl_mapping:
        n = load_gl_mapping_from_excel(conn, args.load_gl_mapping)
        print(f'adp_gl_dept_mapping: upserted {n} rows')

    if args.register_pdf or args.summary_pdf or args.stats_pdf:
        if not (prop and args.batch_number and args.pay_date):
            sys.exit('--register-pdf/--summary-pdf/--stats-pdf require --property-id, --batch-number, --pay-date')
        meta = {'property_id': args.property_id, 'company_code': args.company_code,
                 'batch_number': args.batch_number, 'pay_date': args.pay_date}
        counts = land_batch_from_pdfs(conn, prop, meta, args.register_pdf, args.summary_pdf, args.stats_pdf, replace=replace)
        print(f'landed from PDFs: {counts} '
              f'(register/stats are verified reliable; summary/dept-level parsing has known bugs -- spot-check it)')

    if args.journal_xlsx:
        if not (prop and args.batch_number and args.pay_date):
            sys.exit('--journal-xlsx requires --property-id, --batch-number, --pay-date')
        meta = {'property_id': args.property_id, 'company_code': args.company_code,
                 'batch_number': args.batch_number, 'pay_date': args.pay_date}
        rows = land_journal(conn, prop, meta, args.journal_xlsx, replace=replace)
        print(f'landed {len(rows)} journal lines from {args.journal_xlsx}')

    if args.company_totals_pdf:
        meta = {'property_id': args.property_id, 'company_code': args.company_code,
                 'batch_number': args.batch_number, 'pay_date': args.pay_date}
        data = land_and_promote_company_totals(conn, prop, meta, args.company_totals_pdf, replace=replace)
        print(f'company totals landed + promoted: gross={data.get("gross_amount")} net_cash={data.get("net_cash")}')

    if args.promote:
        if not (prop and args.batch_number):
            sys.exit('--promote requires --property-id and --batch-number')
        n1 = promote_register(conn, prop, args.batch_number, replace=replace)
        n2 = promote_summary_by_dept(conn, prop, args.batch_number, replace=replace)
        n3 = promote_stats(conn, prop, args.batch_number, replace=replace)
        n4 = promote_journal(conn, prop, args.batch_number, replace=replace)
        n5 = promote_company_total(conn, prop, args.batch_number, replace=replace)
        print(f'promoted: {n1} employees, {n2} dept-summary rows, stats={n3}, '
              f'{n4} journal lines, company_total={n5} (0 means run --company-totals-pdf first)')

    conn.close()


if __name__ == '__main__':
    main()
