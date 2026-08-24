"""Schema-v1 DB loader + reconciliation validation."""
import re

try:
    from psycopg2.extras import Json
except ImportError:
    Json = None

from payroll.legacy.parsers import tbl


def load(conn, prop, meta, journal, stats, summary, register, replace=True):
    cur = conn.cursor()
    P = meta['property_id']
    cc = meta['company_code']
    bn = meta['batch_number']
    pd_ = meta['pay_date']

    if replace:
        for t, st in [('payroll_raw_landing', None)]:
            cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_raw_landing")} '
                        f'WHERE batch_number=%s', (bn,))
        for t in ('payroll_journal', 'payroll_summary', 'stats_summary', 'payroll_register_line'):
            cur.execute(f'DELETE FROM public.{tbl(prop,t)} WHERE batch_number=%s', (bn,))

    def land(stype, sfile, seq, payload):
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_raw_landing")} '
            f'(property_id,company_code,batch_number,pay_date,source_type,source_file,row_seq,payload) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, pd_, stype, sfile, seq, Json(payload)))

    # --- journal (landing + curated) ---
    for i, r in enumerate(journal, 1):
        land('journal', meta['files']['journal'], i, {**r, 'record_type': 'je_line',
             'transaction_date': str(r['transaction_date'])})
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_journal")} '
            f'(property_id,company_code,batch_number,external_id,transaction_date,memo,subsidiary,'
            f'gl_account_number,gl_account_name,debit,credit,memo_line,outlet,month,year,source_file) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, r['external_id'], r['transaction_date'], r['memo'], r['subsidiary'],
             r['gl_account_number'], r['gl_account_name'], r['debit'], r['credit'],
             r['memo_line'], r['outlet'], meta['month'], meta['year'], meta['files']['journal']))

    # --- stats (landing) ---
    land('stats', meta['files']['stats'], 1, {'record_type': 'stats_recap', **{k: v for k, v in stats.items() if k != 'raw_lines'}})
    cur.execute(
        f'INSERT INTO public.{tbl(prop,"stats_summary")} '
        f'(property_id,company_code,batch_number,pay_date,net_cash,fed_income_tax,ss_ee_amount,ss_er_amount,'
        f'medicare_ee_amount,medicare_er_amount,state_income_tax,retirement_401k,wage_garnishments,'
        f'total_amount_debited,detail,month,year,source_file) '
        f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (P, cc, bn, pd_, stats.get('net_cash'), stats.get('federal_income_tax'), stats.get('ss_ee'),
         stats.get('ss_er'), stats.get('medicare_ee'), stats.get('medicare_er'), stats.get('state_income_tax'),
         stats.get('retirement_401k'), stats.get('wage_garnishments'), stats.get('total_amount_debited'),
         Json(stats), meta['month'], meta['year'], meta['files']['stats']))

    # --- summary (landing + curated) ---
    for i, d in enumerate(summary['departments'], 1):
        land('summary', meta['files']['summary'], i, {'record_type': 'dept_summary', **d})
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_summary")} '
            f'(property_id,company_code,batch_number,pay_date,job_code,department_code,'
            f'earnings_summary,tax_summary,payment_summary,total_earnings_amount,total_net_pay_amount,'
            f'month,year,source_file) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, pd_, d['dept'], d['dept'], Json([]), Json(d.get('taxes', {})),
             Json({'gross': d.get('gross'), 'net_cash': d.get('net_cash')}),
             d.get('gross'), d.get('net_cash'), meta['month'], meta['year'], meta['files']['summary']))

    # --- register (landing + curated) ---
    for i, e in enumerate(register['employees'], 1):
        land('register', meta['files']['register'], i, {'record_type': 'employee', **e})
        earn = e.get('earnings', [])
        reg_amt = round(sum(x.get('reg_earn', 0) for x in earn), 2)
        ot_amt = round(sum(x.get('ot_earn', 0) for x in earn), 2)
        reg_hrs = round(sum(x.get('reg_hours', 0) for x in earn), 2)
        ot_hrs = round(sum(x.get('ot_hours', 0) for x in earn), 2)
        st = e.get('statutory', {})
        tax_detail = [{'code': k, 'amount': v} for k, v in st.items()]
        ded_detail = e.get('deductions', []) if e.get('deductions_reconciled') else []
        depts = e['depts'] or ['']
        for dep in depts:
            primary = dep == depts[0]
            cur.execute(
                f'INSERT INTO public.{tbl(prop,"payroll_register_line")} '
                f'(property_id,company_code,batch_number,pay_date,file_number,employee_name,department_code,'
                f'reg_hours,reg_amount,ot_hours,ot_amount,gross_amount,'
                f'fit_amount,ss_ee_amount,medicare_ee_amount,state_amount,'
                f'earnings_detail,tax_detail,deduction_detail,deductions_reconciled,'
                f'month,year,source_file) '
                f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (P, cc, bn, pd_, e.get('file', ''), e['name'], dep,
                 reg_hrs if primary else 0, reg_amt if primary else 0,
                 ot_hrs if primary else 0, ot_amt if primary else 0,
                 e.get('gross') if primary else 0,
                 st.get('FIT') if primary else 0, st.get('SS') if primary else 0,
                 st.get('MED') if primary else 0, st.get('GA') if primary else 0,
                 Json(earn if primary else []), Json(tax_detail if primary else []),
                 Json(ded_detail if primary else []), bool(e.get('deductions_reconciled')),
                 meta['month'], meta['year'], meta['files']['register']))
    conn.commit()


def validate(journal, summary, register):
    issues = []
    jd = round(sum(r['debit'] for r in journal), 2)
    jc = round(sum(r['credit'] for r in journal), 2)
    if round(abs(jd - jc), 2) > 0.01:
        issues.append(f'JE not balanced: debit {jd} vs credit {jc}')
    sg = round(sum(d.get('gross') or 0 for d in summary['departments']), 2)
    rg = round(sum(e.get('gross') or 0 for e in register['employees']), 2)
    je_wages = round(sum(r['debit'] for r in journal
                         if re.search(r'(R|OT|990)$', r['gl_account_number'])), 2)
    if abs(sg - je_wages) > 0.01:
        issues.append(f'summary gross {sg} != JE wages {je_wages}')
    if abs(rg - je_wages) > 0.01:
        issues.append(f'register gross {rg} != JE wages {je_wages}')
    return {'je_debit': jd, 'je_credit': jc, 'summary_gross': sg,
            'register_gross': rg, 'je_wages': je_wages, 'issues': issues}
