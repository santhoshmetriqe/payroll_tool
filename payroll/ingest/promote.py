"""PROMOTE from raw_landing -> curated (register / summary / stats / journal)
for the schema-v2 tables.

Two rules learned from inspecting real batch-1 payloads:
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
"""
from datetime import datetime

from psycopg2.extras import Json

from payroll.db import tbl

# ---------------------------------------------------------------------------
# Rule: which voluntary deduction codes are cafeteria-125 components.
# Derived from batch-1 summary payload: cafeteria_125.items codes DEN/FSA/MED/VIS
# had amounts IDENTICAL to voluntary deduction codes DNTPT/MEDFSA/MEDPT/VISPT.
# Extend this if other properties/companies use different code spellings.
# ---------------------------------------------------------------------------
CAFETERIA_125_CODES = {'DNTPT', 'MEDFSA', 'MEDPT', 'VISPT'}


def _fetch_raw(conn, prop, batch_number, source_type):
    cur = conn.cursor()
    cur.execute(
        f'SELECT property_id, company_code, batch_number, pay_date, source_file, row_seq, payload '
        f'FROM public.{tbl(prop, "payroll_raw_landing")} '
        f'WHERE batch_number=%s AND source_type=%s ORDER BY row_seq', (batch_number, source_type))
    return cur.fetchall()


def _mmddyyyy(s):
    if not s:
        return None
    return datetime.strptime(s, '%m/%d/%Y').date()


def promote_register(conn, prop, batch_number, replace=True):
    cur = conn.cursor()
    rows = _fetch_raw(conn, prop, batch_number, 'register')
    if not rows:
        return 0
    if replace:
        cur.execute(f'DELETE FROM public.{tbl(prop,"emp_payroll_details")} WHERE batch_number=%s', (batch_number,))
        # rate/deduction rows cascade via FK ON DELETE CASCADE

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
        # (see payroll.parsers.register._parse_employee_block) but the dept/rate
        # pairing is not reliable, so skip inserting them here -- same reasoning
        # as the deductions skip below.
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
