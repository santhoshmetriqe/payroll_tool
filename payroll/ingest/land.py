"""Land raw batch data (PDFs/xlsx) into {p}_payroll_raw_landing, and the
Company-Totals page straight into {p}_payroll_summary.

Register + stats are high-confidence (verified against batch-1 ground
truth). The summary/dept-level parser still has known bugs (deduction/
cafeteria totals can be wrong, especially for the last department on the
last page) -- landed anyway so raw data isn't lost, but promote_summary_by_dept()
output should be spot-checked before trusting it, unlike the other tables.
"""
from datetime import datetime

from psycopg2.extras import Json

from payroll.db import tbl
from payroll.parsers.company_totals import parse_company_totals
from payroll.parsers.dept_totals import parse_dept_totals_full
from payroll.parsers.register import parse_register_full
from payroll.parsers.stats import parse_stats_full
from payroll.parsers.summary import parse_summary_full


def land_and_promote_company_totals(conn, prop, meta, pdf_path, replace=True):
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


def land_batch_from_pdfs(conn, prop, meta, register_pdf=None, summary_pdf=None, stats_pdf=None, replace=True):
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


def land_journal(conn, prop, meta, xlsx_path, replace=True):
    from payroll.legacy.parsers import parse_journal
    P, cc, bn, pd_ = meta['property_id'], meta['company_code'], meta['batch_number'], meta['pay_date']
    cur = conn.cursor()
    if replace:
        cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_raw_landing")} '
                    f'WHERE batch_number=%s AND source_type=%s', (bn, 'journal'))
    rows = parse_journal(xlsx_path)
    for i, r in enumerate(rows, 1):
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_raw_landing")} '
            f'(property_id,company_code,batch_number,pay_date,source_type,source_file,row_seq,payload) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, pd_, 'journal', xlsx_path, i, Json({**r, 'transaction_date': str(r['transaction_date'])})))
    conn.commit()
    return rows
