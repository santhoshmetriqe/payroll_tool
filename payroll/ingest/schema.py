"""Schema v2 init + GL/department master mapping loader."""
from payroll import PROJECT_ROOT


def ensure_schema(conn, prop):
    ddl = (PROJECT_ROOT / 'payroll_schema_v2.sql').read_text(encoding='utf-8')
    cur = conn.cursor()
    cur.execute(ddl.replace('{p}', prop))
    conn.commit()


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
