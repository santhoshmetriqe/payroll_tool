#!/usr/bin/env python3
"""
payroll_ingestor.py -- schema v2 ingestor for the {p}_emp_payroll_details /
{p}_emp_pay_rate_details / {p}_emp_deduction_details / {p}_payroll_summary /
{p}_payroll_summary_by_dept redesign.

Two jobs, meant to be re-run for every future batch:

1. PROMOTE: {p}_payroll_raw_landing already holds the full parsed JSON for
   register/summary/stats/journal (however it got landed). This reads that
   JSON and populates the new curated tables, applying three rules learned
   from inspecting real batch-1 payloads:
     - cafeteria_125 items are NOT inserted as separate deduction rows -- they
       are a non-additive sub-breakdown of specific voluntary codes (verified:
       DEN/FSA/MED/VIS amounts == DNTPT/MEDFSA/MEDPT/VISPT amounts exactly).
       Matching voluntary rows get is_cafeteria_125=true instead.
     - statutory taxes are NOT duplicated into emp_deduction_details -- they
       already live as columns on emp_payroll_details.
     - department_code for GL allocation is taken per rate line (rate_lines[
       i]['dept']), not from the employee header, because employees can work
       multiple departments in one pay period (confirmed real case in batch 1:
       DE LA CRUZ, CIRILA, dept "603500/605000").

2. LAND + PROMOTE the "Company Totals" page (last page of the ADP Payroll
   Register PDF) into {p}_payroll_summary -- this page is NOT currently in
   raw_landing at all, so it's parsed fresh from the PDF here. The parser is
   a fixed-template extractor (not generic) built and verified against the
   real coordinates of that page for batch 1 -- see parse_company_totals().

Run:
    python payroll_ingestor.py --init-schema
    python payroll_ingestor.py --load-gl-mapping "C:\\...\\ADP Master Mapping Working.xlsx"
    python payroll_ingestor.py --property-id 383 --company-code QVJ \
        --batch-number 6908-030 --pay-date 2026-07-04 \
        --company-totals-pdf "<path to ...Payroll register.pdf>" \
        --promote
"""
import argparse
import configparser
import json
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

# ---------------------------------------------------------------------------
# Config / connection (same precedence as payroll_etl.py: --dsn > PG_DSN > config.ini)
# ---------------------------------------------------------------------------
def load_config(path):
    cfg = configparser.ConfigParser()
    if not cfg.read(path, encoding='utf-8'):
        return {}
    out = {}
    if cfg.has_section('database'):
        out['database'] = dict(cfg.items('database'))
    if cfg.has_section('defaults'):
        out['defaults'] = {k: v for k, v in cfg.items('defaults') if v}
    return out


def connect_db(args, cfg):
    import os
    if args.dsn:
        return psycopg2.connect(args.dsn)
    if os.environ.get('PG_DSN'):
        return psycopg2.connect(os.environ['PG_DSN'])
    if cfg.get('database'):
        return psycopg2.connect(**cfg['database'])
    sys.exit('No DB credentials: provide --dsn, PG_DSN, or a [database] section in config.ini')


def tbl(prop, name):
    return f'"{prop}_{name}"'


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------
def ensure_schema(conn, prop):
    ddl = Path(__file__).with_name('payroll_schema_v2.sql').read_text(encoding='utf-8')
    cur = conn.cursor()
    cur.execute(ddl.replace('{p}', prop))
    conn.commit()


# ---------------------------------------------------------------------------
# GL / department master mapping loader (from the ADP Master Mapping Excel)
# ---------------------------------------------------------------------------
def load_gl_mapping_from_excel(conn, xlsx_path):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb['Final']
    cur = conn.cursor()
    n = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        gl_account, fnb_outlet, adp_dept, dept_title, division, ns_account, ns_account_name = row
        if gl_account is None or adp_dept is None or ns_account is None:
            continue
        cur.execute(
            'INSERT INTO public.adp_gl_dept_mapping '
            '(gl_account, fnb_outlet, adp_dept, department_title, division, ns_account, ns_account_name) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s) '
            'ON CONFLICT ON CONSTRAINT uq_adp_gl_dept_mapping DO UPDATE SET '
            'gl_account=EXCLUDED.gl_account, fnb_outlet=EXCLUDED.fnb_outlet, '
            'department_title=EXCLUDED.department_title, division=EXCLUDED.division, '
            'ns_account_name=EXCLUDED.ns_account_name, updated_at=now()',
            (int(gl_account), fnb_outlet, int(adp_dept), dept_title, division,
             str(ns_account), ns_account_name))
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Rule: which voluntary deduction codes are cafeteria-125 components.
# Derived from batch-1 summary payload: cafeteria_125.items codes DEN/FSA/MED/VIS
# had amounts IDENTICAL to voluntary deduction codes DNTPT/MEDFSA/MEDPT/VISPT.
# Extend this if other properties/companies use different code spellings.
# ---------------------------------------------------------------------------
CAFETERIA_125_CODES = {'DNTPT', 'MEDFSA', 'MEDPT', 'VISPT'}


# ---------------------------------------------------------------------------
# PROMOTE from raw_landing -> curated (register / summary / stats / journal)
# ---------------------------------------------------------------------------
def _fetch_raw(conn, prop, batch_number, source_type):
    cur = conn.cursor()
    cur.execute(
        f'SELECT property_id, company_code, batch_number, pay_date, source_file, row_seq, payload '
        f'FROM public.{tbl(prop, "payroll_raw_landing")} '
        f'WHERE batch_number=%s AND source_type=%s ORDER BY row_seq', (batch_number, source_type))
    return cur.fetchall()


def promote_register(conn, prop, batch_number, replace=True):
    cur = conn.cursor()
    rows = _fetch_raw(conn, prop, batch_number, 'register')
    if not rows:
        return 0
    if replace:
        cur.execute(f'DELETE FROM public.{tbl(prop,"emp_payroll_details")} WHERE batch_number=%s', (batch_number,))
        # rate/deduction rows cascade via FK ON DELETE CASCADE

    def _mmddyyyy(s):
        if not s:
            return None
        from datetime import datetime
        return datetime.strptime(s, '%m/%d/%Y').date()

    n = 0
    for (P, cc, bn, pd_, source_file, seq, e) in rows:
        if e.get('record_type') != 'employee':
            # 'dept_total' / 'company_total' rows also live under source_type='register'
            # in the batch-1 hand-built landing -- handled separately (see
            # promote_company_total / promote_summary_by_dept), not per-employee data.
            continue
        personnel = e.get('personnel', {})
        file_number = personnel.get('file', '')
        employee_name = personnel.get('name', '')
        department_code = personnel.get('dept', '')
        statutory = e.get('statutory', {})
        memo = e.get('memo', [])
        period_end = _mmddyyyy(e.get('period_ending_date'))
        check_date = _mmddyyyy(e.get('pay_date_field'))

        cur.execute(
            f'INSERT INTO public.{tbl(prop,"emp_payroll_details")} '
            f'(property_id,company_code,batch_number,pay_date,report_period_end_date,check_date,'
            f'file_number,employee_name,department_code,'
            f'voucher_number,total_work_hrs,gross_amount,net_pay_amount,'
            f'fit_amount,ss_ee_amount,medicare_ee_amount,state_amount,memo_detail,source_file) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
            f'RETURNING id',
            (P, cc, bn, pd_, period_end, check_date, file_number, employee_name, department_code,
             e.get('voucher', ''), e.get('total_work_hrs', 0), e.get('gross', 0), e.get('net_pay', 0),
             statutory.get('FIT', 0), statutory.get('SS', 0), statutory.get('MED', 0), statutory.get('GA', 0),
             Json(memo), source_file))
        emp_id = cur.fetchone()[0]
        n += 1

        # --- rate lines: department sourced per-line, falls back to header dept.
        # needs_manual_review employees keep their parsed rate_lines in raw_landing
        # (see payroll_pdf_parser._parse_employee_block) but the dept/rate pairing
        # is not reliable, so skip inserting them here -- same reasoning as the
        # deductions skip below.
        for i, rl in enumerate(([] if e.get('needs_manual_review') else e.get('rate_lines', [])), 1):
            ot_hours = rl.get('ot_hours', rl.get('qot_entry', 0))
            cur.execute(
                f'INSERT INTO public.{tbl(prop,"emp_pay_rate_details")} '
                f'(property_id,company_code,batch_number,pay_date,report_period_end_date,check_date,'
                f'emp_payroll_detail_id,file_number,'
                f'employee_name,department_code,rate_seq,rate,reg_hours,reg_earn,ot_code,ot_hours,ot_earn,source_file) '
                f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (P, cc, bn, pd_, period_end, check_date, emp_id, file_number, employee_name,
                 rl.get('dept', department_code), i, rl.get('rate', 0),
                 rl.get('reg_hours', 0), rl.get('reg_earn', 0),
                 rl.get('ot_code', ''), ot_hours, rl.get('ot_earn', 0), source_file))

        # --- deductions: voluntary only (statutory stays in header, no duplication).
        # Multi-department employees repeat their deduction listing once per dept
        # segment in the raw register text (same root cause as the rate_lines
        # issue), so their deductions are also unreliable to auto-split -- skip
        # them for needs_manual_review employees rather than insert duplicates/
        # double-counted amounts. Dedupe-by-sum as a defensive safety net for
        # any other duplicate-code parsing artifact (protects the unique index).
        if not e.get('needs_manual_review'):
            by_code = {}
            for d in e.get('voluntary', []):
                code = d.get('code', '')
                by_code[code] = by_code.get(code, 0) + (d.get('amount') or 0)
            for code, amount in by_code.items():
                cur.execute(
                    f'INSERT INTO public.{tbl(prop,"emp_deduction_details")} '
                    f'(property_id,company_code,batch_number,pay_date,emp_payroll_detail_id,file_number,'
                    f'employee_name,deduction_type,deduction_code,deduction_amount,is_cafeteria_125,source_file) '
                    f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (P, cc, bn, pd_, emp_id, file_number, employee_name,
                     'voluntary', code, amount, code in CAFETERIA_125_CODES, source_file))
    conn.commit()
    return n


def promote_summary_by_dept(conn, prop, batch_number, replace=True):
    cur = conn.cursor()
    rows = _fetch_raw(conn, prop, batch_number, 'summary')
    if not rows:
        return 0
    if replace:
        cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_summary_by_dept")} WHERE batch_number=%s', (batch_number,))

    def _mmddyyyy(s):
        if not s:
            return None
        from datetime import datetime
        return datetime.strptime(s, '%m/%d/%Y').date()

    n = 0
    for (P, cc, bn, pd_, source_file, seq, d) in rows:
        taxes = d.get('taxes', {})
        caf = d.get('cafeteria_125', {})
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_summary_by_dept")} '
            f'(property_id,company_code,batch_number,pay_date,report_period_end_date,check_date,'
            f'department_code,department_title,pct_of_company,'
            f'reg_hours,reg_earn,ot_hours,ot_earn,hours34,earn34,earn5,gross_amount,net_cash,'
            f'fit_amount,ss_amount,medicare_amount,state_amount,total_deductions,'
            f'taxable_analysis,hours34_analysis,earnings345_analysis,memo_detail,deduction_detail,'
            f'cafeteria_125_detail,source_file) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, pd_, _mmddyyyy(d.get('period_ending_date')), _mmddyyyy(d.get('pay_date_field')),
             d.get('dept', ''), d.get('dept_name', ''), d.get('pct_of_co', 0),
             d.get('reg_hours', 0), d.get('reg_earn', 0), d.get('ot_hours', 0), d.get('ot_earn', 0),
             d.get('hours34', 0), d.get('earn34', 0), d.get('earn5', 0),
             d.get('gross', 0), d.get('net_cash', 0),
             taxes.get('FIT', 0), taxes.get('SS', 0), taxes.get('MED', 0), taxes.get('STATE', 0),
             d.get('total_deductions', 0),
             Json(d.get('taxable_analysis', {})), Json(d.get('hours34_analysis', [])),
             Json(d.get('earnings345_analysis', [])), Json(d.get('memo', [])),
             Json(d.get('deductions', [])), Json(caf), source_file))
        n += 1
    conn.commit()
    return n


def promote_company_total(conn, prop, batch_number, replace=True):
    """
    Primary path for {p}_payroll_summary: the batch-level 'company_total'
    record already exists in raw_landing under source_type='register' (hand
    landed alongside the employee/dept_total rows for batch 1). Far more
    reliable than re-parsing the Company-Totals PDF page from scratch.
    Falls back to nothing (0 rows) if that record isn't present yet -- in
    that case use --company-totals-pdf to parse+land it first.
    """
    cur = conn.cursor()
    rows = _fetch_raw(conn, prop, batch_number, 'register')
    ct = next((r for r in rows if r[6].get('record_type') == 'company_total'), None)
    if not ct:
        return 0
    P, cc, bn, pd_, source_file, seq, d = ct
    if replace:
        cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_summary")} WHERE batch_number=%s', (bn,))
    cur.execute(
        f'INSERT INTO public.{tbl(prop,"payroll_summary")} '
        f'(property_id,company_code,batch_number,pay_date,reg_hours,ot_hours,hours3,'
        f'reg_earn,ot_earn,earnings3,gross_amount,fit_amount,ss_amount,medicare_amount,state_amount,'
        f'total_voluntary_deductions,net_cash,pays_count,source_file) '
        f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (P, d.get('company_code', cc), bn, pd_, d.get('reg_hours', 0), d.get('ot_hours', 0), d.get('hours3', 0),
         d.get('reg_earn', 0), d.get('ot_earn', 0), d.get('earn3', 0), d.get('gross', 0),
         d.get('FIT', 0), d.get('SS', 0), d.get('MED', 0), d.get('STATE', 0),
         d.get('total_deductions', 0), d.get('net_cash', 0), int(d.get('pays', 0)), source_file))
    conn.commit()
    return 1


def promote_stats(conn, prop, batch_number, replace=True):
    """stats_summary stays as-is per instruction: standalone, data loaded verbatim."""
    cur = conn.cursor()
    rows = _fetch_raw(conn, prop, batch_number, 'stats')
    if not rows:
        return 0
    merged = {}
    P = cc = bn = pd_ = source_file = None
    for (P, cc, bn, pd_, source_file, seq, r) in rows:
        merged.update(r)
    if replace:
        cur.execute(f'DELETE FROM public.{tbl(prop,"stats_summary")} WHERE batch_number=%s', (batch_number,))
    cur.execute(
        f'INSERT INTO public.{tbl(prop,"stats_summary")} '
        f'(property_id,company_code,batch_number,pay_date,net_pay_checks,net_pay_direct_deposit,net_cash,'
        f'fed_income_tax,ss_ee_amount,ss_er_amount,medicare_ee_amount,medicare_er_amount,futa_amount,'
        f'state_income_tax,sui_er_amount,total_taxes_debited,retirement_401k,wage_garnishments,'
        f'total_amount_debited,detail,source_file) '
        f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (P, cc, bn, pd_, merged.get('net_pay_checks', 0), merged.get('adp_direct_deposit', 0),
         merged.get('total_net_pay_liability_net_cash', 0),
         merged.get('federal_income_tax', 0), merged.get('ss_ee', 0), merged.get('ss_er', 0),
         merged.get('medicare_ee', 0), merged.get('medicare_er', 0), merged.get('futa', 0),
         merged.get('state_income_tax', 0), merged.get('state_ga', {}).get('sui_er_rate', 0),
         merged.get('total_taxes_debited', 0), merged.get('retirement_401k', 0),
         merged.get('wage_garnishments', 0), merged.get('total_amount_debited', 0),
         Json(merged), source_file))
    conn.commit()
    return 1


def promote_journal(conn, prop, batch_number, replace=True):
    """payroll_journal stays as-is per instruction: unchanged table."""
    cur = conn.cursor()
    rows = _fetch_raw(conn, prop, batch_number, 'journal')
    if not rows:
        return 0
    if replace:
        cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_journal")} WHERE batch_number=%s', (batch_number,))
    n = 0
    for (P, cc, bn, pd_, source_file, seq, r) in rows:
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_journal")} '
            f'(property_id,company_code,batch_number,external_id,transaction_date,memo,subsidiary,'
            f'gl_account_number,gl_account_name,debit,credit,memo_line,outlet,line_seq,source_file) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, r.get('external_id', ''), r.get('transaction_date'), r.get('memo', ''),
             r.get('subsidiary', ''), r.get('gl_account_number', ''), r.get('gl_account_name', ''),
             r.get('debit', 0), r.get('credit', 0), r.get('memo_line', ''), r.get('outlet', ''), seq, source_file))
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Company-Totals page (last page of ADP Payroll Register) -- fixed-template
# parser, verified against the real word coordinates of batch-1 page 5.
# ---------------------------------------------------------------------------
_NUMTOK = re.compile(r'^-?\d+(\.\d+)?-?$')


def _flush_pair(digits, amt, neg, label_words):
    if amt is None:
        amt = (int(''.join(digits)) / 100.0) if digits else 0.0
    if neg:
        amt = -amt
    label = ' '.join(label_words).strip()
    return label, amt


def _parse_grid_row(words):
    """Sequential amount/label scanner for one row of the 4-row totals grid."""
    pairs = []
    digits, amt, neg, label_words = [], None, False, []
    i = 0
    while i < len(words):
        w = words[i]
        if _NUMTOK.match(w):
            if label_words:
                if digits or amt is not None:
                    pairs.append(_flush_pair(digits, amt, neg, label_words))
                # else: leading label with no preceding amount (e.g. the 'QVJ'
                # company-code token before the first number) -- discard it.
                digits, amt, neg, label_words = [], None, False, []
            if w.endswith('-'):
                neg = True
                w = w[:-1]
            if '.' in w:
                amt = float(w)  # already a complete decimal value (e.g. "529.80"), not a digit-group
            else:
                digits.append(w)
            i += 1
            continue
        label_words.append(w)
        i += 1
        # absorb a lone digit suffix immediately after HOURS/EARNINGS (column index label, e.g. "HOURS 3")
        if w in ('HOURS', 'EARNINGS') and i < len(words) and re.fullmatch(r'\d', words[i]):
            label_words.append(words[i])
            i += 1
    if digits or amt is not None or label_words:
        pairs.append(_flush_pair(digits, amt, neg, label_words))
    return pairs


def _parse_code_amount_list(words):
    """Generic 'amount CODE DESC amount CODE DESC ...' list parser (analysis
    sections) -- reuses the grid row scanner so multi-word codes like
    'CK1 ck1' or 'TOTAL DEDUCTIONS' are captured in full, not truncated to
    their first word."""
    return [{'amount': amt, 'code': label} for label, amt in _parse_grid_row(words) if label]


def _adp_money(s):
    s = s.strip()
    if not s:
        return 0.0
    if '.' in s:
        return float(s)
    digits = re.sub(r'\D', '', s)
    return int(digits) / 100.0 if digits else 0.0


def parse_company_totals(pdf_path):
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        target = None
        for page in pdf.pages:
            text = page.extract_text() or ''
            if 'Company Totals' in text:
                target = page
                full_text = text
                break
        if target is None:
            raise ValueError(f'No "Company Totals" page found in {pdf_path}')

        words = target.extract_words()
        rows = {}
        for w in words:
            key = round(w['top'])
            rows.setdefault(key, []).append(w)
        # the 4-row grid immediately below "COMPANY CODE" -- skip that header
        # row itself (it has no digit tokens) and any other non-numeric rows.
        row_tops = [t for t in sorted(rows) if any(_NUMTOK.match(w['text']) for w in rows[t])][:4]
        grid_rows = [[w['text'] for w in sorted(rows[t], key=lambda x: x['x0'])] for t in row_tops]

    field_map = {
        (0, 0): 'hours_reg', (0, 1): 'earnings_reg', (0, 2): 'earnings_ot',
        (0, 3): 'fit_amount', (0, 4): 'total_voluntary_deductions', (0, 5): 'pays_count',
        (1, 0): 'hours_ot', (1, 1): 'earnings3', (1, 2): 'earnings4',
        (1, 3): 'ss_amount', (1, 4): 'net_payroll_checks_amount',
        (2, 0): 'hours3', (2, 1): 'earnings5', (2, 2): 'gross_amount', (2, 3): 'medicare_amount',
        (3, 0): 'hours4', (3, 1): 'state_amount',
    }
    out = {}
    for ridx, row in enumerate(grid_rows):
        for pidx, (label, amt) in enumerate(_parse_grid_row(row)):
            key = field_map.get((ridx, pidx))
            if key:
                out[key] = amt
    out['reg_hours'] = out.pop('hours_reg', 0)
    out['ot_hours'] = out.pop('hours_ot', 0)
    out['reg_earn'] = out.pop('earnings_reg', 0)
    out['ot_earn'] = out.pop('earnings_ot', 0)
    # 'Pays' is a plain count, not a money amount -- undo the /100 cents division
    out['pays_count'] = int(round(out.get('pays_count', 0) * 100))

    def _section(header, next_headers):
        m = re.search(re.escape(header) + r':?\s*(.*?)(?=' +
                       '|'.join(re.escape(h) for h in next_headers) + '|$)', full_text, re.S)
        return m.group(1).split() if m else []

    out['hours_analysis'] = _parse_code_amount_list(
        _section('HOURS ANALYSIS', ['EARNINGS ANALYSIS']))
    out['earnings_analysis'] = _parse_code_amount_list(
        _section('EARNINGS ANALYSIS', ['MEMO ANALYSIS']))
    out['memo_analysis'] = _parse_code_amount_list(
        _section('MEMO ANALYSIS', ['STATUTORY DED ANALYSIS', 'VOLUNTARY DED ANALYSIS']))
    out['statutory_ded_analysis'] = _parse_code_amount_list(
        _section('STATUTORY DED ANALYSIS', ['VOLUNTARY DED ANALYSIS']))
    out['voluntary_ded_analysis'] = _parse_code_amount_list(
        _section('VOLUNTARY DED ANALYSIS', ['NET PAYROLL']))

    def _num(pattern, text=full_text, cast=_adp_money):
        m = re.search(pattern, text)
        return cast(m.group(1)) if m else None

    out['net_payroll_checks_amount'] = _num(r'NET PAYROLL:\s*([\d\s.]+?)\s*CHECKS') or out.get('net_payroll_checks_amount', 0)
    out['checks_count'] = int(_num(r'CHECKS:\s*(\d+)', cast=float) or 0)
    out['flagged_count'] = int(_num(r'FLAGGED:\s*(\d+)', cast=float) or 0)
    out['total_deposits'] = _num(r'TOTAL DEPOSITS:\s*([\d\s.]+?)\s*VOUCHERS') or 0
    out['vouchers_count'] = int(_num(r'VOUCHERS:\s*(\d+)', cast=float) or 0)
    out['net_cash_pays_over_1000_count'] = int(_num(r'NET CASH PAYS[\s\S]*?(\d+)\s*OR MORE', cast=float) or 0)
    out['net_voids'] = _num(r'NET VOIDS:\s*([\d\s.]+?)\s*ADJUSTMENTS') or 0
    out['evouchers_count'] = int(_num(r'eVOUCHERS:\s*(\d+)', cast=float) or 0)
    out['net_cash'] = _num(r'NET CASH:\s*([\d\s.]+?)\s*PAPER VOUCHERS') or 0
    out['paper_vouchers_printed'] = int(_num(r'PAPER VOUCHERS PRINTED:\s*(\d+)', cast=float) or 0)
    check_nums = re.findall(r'CHECK NUMBER:\s*(\S+)', full_text)
    out['starting_check_number'] = check_nums[-2] if len(check_nums) >= 2 else ''
    out['ending_check_number'] = check_nums[-1] if check_nums else ''
    m = re.search(r'ADP CHECK NUMBERS:\s*(.*?)\s*STARTING CHECK NUMBER', full_text)
    out['adp_check_numbers'] = [{'label': m.group(1).strip()}] if m else []

    m = re.search(r'Batch:\s*(\S+)\s+PeriodEnding\s*:\s*(\S+)\s+Week\s+(\d+)', full_text)
    if m:
        out['_batch_number'] = m.group(1)
        out['_period_ending_date'] = m.group(2)
        out['week_number'] = int(m.group(3))
    m = re.search(r'Service Center\s*:\s*(\S+)\s+PayDate:\s*(\S+)', full_text)
    if m:
        out['service_center'] = m.group(1)
        out['_pay_date'] = m.group(2)
    return out


def land_and_promote_company_totals(conn, prop, meta, pdf_path, replace=True):
    from datetime import datetime
    data = parse_company_totals(pdf_path)
    P, cc, bn, pd_ = meta['property_id'], meta['company_code'], meta['batch_number'], meta['pay_date']
    cur = conn.cursor()

    if replace:
        cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_summary")} WHERE batch_number=%s', (bn,))
        cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_raw_landing")} '
                    f'WHERE batch_number=%s AND source_type=%s', (bn, 'company_totals'))

    cur.execute(
        f'INSERT INTO public.{tbl(prop,"payroll_raw_landing")} '
        f'(property_id,company_code,batch_number,pay_date,source_type,source_file,row_seq,payload) '
        f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
        (P, cc, bn, pd_, 'company_totals', str(pdf_path), 1, Json({k: v for k, v in data.items() if not k.startswith('_')})))

    period_ending = None
    if data.get('_period_ending_date'):
        try:
            period_ending = datetime.strptime(data['_period_ending_date'], '%m/%d/%Y').date()
        except ValueError:
            period_ending = None

    cur.execute(
        f'INSERT INTO public.{tbl(prop,"payroll_summary")} '
        f'(property_id,company_code,batch_number,pay_date,period_ending_date,week_number,service_center,'
        f'reg_hours,ot_hours,hours3,hours4,reg_earn,ot_earn,earnings3,earnings4,earnings5,gross_amount,'
        f'fit_amount,ss_amount,medicare_amount,state_amount,total_voluntary_deductions,'
        f'net_payroll_checks_amount,total_deposits,net_voids,net_cash,checks_count,flagged_count,'
        f'vouchers_count,pays_count,net_cash_pays_over_1000_count,starting_check_number,ending_check_number,'
        f'evouchers_count,paper_vouchers_printed,hours_analysis,earnings_analysis,memo_analysis,'
        f'statutory_ded_analysis,voluntary_ded_analysis,adp_check_numbers,source_file) '
        f'VALUES ({",".join(["%s"] * 42)})',
        (P, cc, bn, pd_, period_ending, data.get('week_number'), data.get('service_center', ''),
         data.get('reg_hours', 0), data.get('ot_hours', 0), data.get('hours3', 0), data.get('hours4', 0),
         data.get('reg_earn', 0), data.get('ot_earn', 0), data.get('earnings3', 0), data.get('earnings4', 0),
         data.get('earnings5', 0), data.get('gross_amount', 0),
         data.get('fit_amount', 0), data.get('ss_amount', 0), data.get('medicare_amount', 0), data.get('state_amount', 0),
         data.get('total_voluntary_deductions', 0),
         data.get('net_payroll_checks_amount', 0), data.get('total_deposits', 0), data.get('net_voids', 0),
         data.get('net_cash', 0), data.get('checks_count', 0), data.get('flagged_count', 0),
         data.get('vouchers_count', 0), int(data.get('pays_count', 0)), data.get('net_cash_pays_over_1000_count', 0),
         data.get('starting_check_number', ''), data.get('ending_check_number', ''),
         data.get('evouchers_count', 0), data.get('paper_vouchers_printed', 0),
         Json(data.get('hours_analysis', [])), Json(data.get('earnings_analysis', [])),
         Json(data.get('memo_analysis', [])), Json(data.get('statutory_ded_analysis', [])),
         Json(data.get('voluntary_ded_analysis', [])), Json(data.get('adp_check_numbers', [])),
         str(pdf_path)))
    conn.commit()
    return data


# ---------------------------------------------------------------------------
# Land a full batch straight from PDFs, using payroll_pdf_parser.py.
# Register + stats are high-confidence (verified against batch-1 ground
# truth). The summary/dept-level parser still has known bugs (deduction/
# cafeteria totals can be wrong, especially for the last department on the
# last page) -- landed anyway so raw data isn't lost, but promote_summary_by_dept()
# output should be spot-checked before trusting it, unlike the other tables.
# ---------------------------------------------------------------------------
def land_batch_from_pdfs(conn, prop, meta, register_pdf=None, summary_pdf=None, stats_pdf=None, replace=True):
    from payroll_pdf_parser import parse_register_full, parse_summary_full, parse_stats_full, parse_dept_totals_full
    P, cc, bn, pd_ = meta['property_id'], meta['company_code'], meta['batch_number'], meta['pay_date']
    cur = conn.cursor()
    counts = {}

    def _land(source_type, path, records):
        if replace:
            cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_raw_landing")} '
                        f'WHERE batch_number=%s AND source_type=%s', (bn, source_type))
        for i, rec in enumerate(records, 1):
            cur.execute(
                f'INSERT INTO public.{tbl(prop,"payroll_raw_landing")} '
                f'(property_id,company_code,batch_number,pay_date,source_type,source_file,row_seq,payload) '
                f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (P, cc, bn, pd_, source_type, str(path), i, Json(rec)))
        conn.commit()
        counts[source_type] = len(records)

    if register_pdf:
        # dept_total records (record_type='dept_total') land alongside the
        # employee records under the same source_type -- promote_register()
        # already skips non-'employee' record_types here, exactly as it does
        # for the hand-landed batch-1 dept_total/company_total rows.
        _land('register', register_pdf, parse_register_full(register_pdf) + parse_dept_totals_full(register_pdf))
    if summary_pdf:
        _land('summary', summary_pdf, parse_summary_full(summary_pdf))
    if stats_pdf:
        _land('stats', stats_pdf, [parse_stats_full(stats_pdf)])
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dsn')
    ap.add_argument('--config', default=str(Path(__file__).with_name('config.ini')))
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
        from payroll_etl import parse_journal
        cur = conn.cursor()
        if replace:
            cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_raw_landing")} '
                        f'WHERE batch_number=%s AND source_type=%s', (args.batch_number, 'journal'))
        rows = parse_journal(args.journal_xlsx)
        for i, r in enumerate(rows, 1):
            cur.execute(
                f'INSERT INTO public.{tbl(prop,"payroll_raw_landing")} '
                f'(property_id,company_code,batch_number,pay_date,source_type,source_file,row_seq,payload) '
                f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (args.property_id, args.company_code, args.batch_number, args.pay_date, 'journal',
                 args.journal_xlsx, i, Json({**r, 'transaction_date': str(r['transaction_date'])})))
        conn.commit()
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
