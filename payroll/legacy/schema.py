"""Schema v1 DDL (superseded by payroll_schema_v2.sql, see payroll.ingest.schema),
kept for the legacy payroll_etl.py CLI (--init-schema)."""

SCHEMA_DDL = r'''
CREATE TABLE IF NOT EXISTS public."{p}_payroll_raw_landing" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer, company_code text, batch_number text, pay_date date,
    source_type text, source_file text, row_seq integer, payload jsonb,
    ingested_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_landing" UNIQUE (source_file, source_type, row_seq));

CREATE TABLE IF NOT EXISTS public."{p}_payroll_register_line" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    report_period_start_date date, report_period_end_date date, check_date date, pay_date date,
    file_number text DEFAULT '', employee_name text DEFAULT '', department_code text DEFAULT '',
    gl_account text DEFAULT '',
    reg_hours numeric(10,2) DEFAULT 0, reg_amount numeric(12,2) DEFAULT 0,
    ot_hours numeric(10,2) DEFAULT 0, ot_amount numeric(12,2) DEFAULT 0,
    hol_hours numeric(10,2) DEFAULT 0, hol_amount numeric(12,2) DEFAULT 0,
    gross_amount numeric(12,2) DEFAULT 0,
    fit_amount numeric(12,2) DEFAULT 0, ss_ee_amount numeric(12,2) DEFAULT 0,
    medicare_ee_amount numeric(12,2) DEFAULT 0, state_amount numeric(12,2) DEFAULT 0,
    earnings_detail jsonb DEFAULT '[]'::jsonb, tax_detail jsonb DEFAULT '[]'::jsonb,
    deduction_detail jsonb DEFAULT '[]'::jsonb,
    deductions_reconciled boolean DEFAULT false,
    voucher_number text DEFAULT '', net_pay_amount numeric(12,2),
    currency char(3) DEFAULT 'USD', month smallint DEFAULT 0, year smallint DEFAULT 0,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_reg" UNIQUE (property_id, batch_number, pay_date, file_number, department_code));

CREATE TABLE IF NOT EXISTS public."{p}_payroll_summary" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    report_period_start_date date, report_period_end_date date, check_date date, pay_date date,
    job_code text DEFAULT '', job_code_description text DEFAULT '',
    department_code text DEFAULT '', gl_account text DEFAULT '',
    total_employees integer DEFAULT 0, female_employees integer DEFAULT 0, male_employees integer DEFAULT 0,
    earnings_summary jsonb DEFAULT '[]'::jsonb, tax_summary jsonb DEFAULT '[]'::jsonb,
    deduction_summary jsonb DEFAULT '[]'::jsonb, employer_cost_summary jsonb DEFAULT '{}'::jsonb,
    payment_summary jsonb DEFAULT '{}'::jsonb, cafeteria_125 jsonb DEFAULT '{}'::jsonb,
    total_earnings_hours numeric(12,2) DEFAULT 0, total_earnings_amount numeric(14,2) DEFAULT 0,
    total_tax_deductions_amount numeric(14,2) DEFAULT 0, total_other_deductions_amount numeric(14,2) DEFAULT 0,
    total_employer_cost_amount numeric(14,2) DEFAULT 0, total_net_pay_amount numeric(14,2) DEFAULT 0,
    currency char(3) DEFAULT 'USD', month smallint DEFAULT 0, year smallint DEFAULT 0,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_sum" UNIQUE (property_id, batch_number, pay_date, job_code, gl_account));

CREATE TABLE IF NOT EXISTS public."{p}_stats_summary" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    report_period_end_date date, pay_date date, quarter_number smallint DEFAULT 0,
    net_pay_checks numeric(14,2) DEFAULT 0, net_pay_direct_deposit numeric(14,2) DEFAULT 0,
    net_cash numeric(14,2) DEFAULT 0,
    fed_income_tax numeric(14,2) DEFAULT 0, ss_ee_amount numeric(14,2) DEFAULT 0,
    ss_er_amount numeric(14,2) DEFAULT 0, medicare_ee_amount numeric(14,2) DEFAULT 0,
    medicare_er_amount numeric(14,2) DEFAULT 0, futa_amount numeric(14,2) DEFAULT 0,
    state_income_tax numeric(14,2) DEFAULT 0, sui_er_amount numeric(14,2) DEFAULT 0,
    total_taxes_debited numeric(14,2) DEFAULT 0, retirement_401k numeric(14,2) DEFAULT 0,
    wage_garnishments numeric(14,2) DEFAULT 0, total_amount_debited numeric(14,2) DEFAULT 0,
    detail jsonb DEFAULT '{}'::jsonb, currency char(3) DEFAULT 'USD',
    month smallint DEFAULT 0, year smallint DEFAULT 0, isactive boolean DEFAULT true,
    source_file text DEFAULT '', created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_stats" UNIQUE (property_id, batch_number, pay_date));

CREATE TABLE IF NOT EXISTS public."{p}_payroll_journal" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    external_id text DEFAULT '', transaction_date date, posting_period text DEFAULT '',
    memo text DEFAULT '', subsidiary text DEFAULT '',
    gl_account_number text DEFAULT '', gl_account_name text DEFAULT '',
    debit numeric(14,2) DEFAULT 0, credit numeric(14,2) DEFAULT 0,
    memo_line text DEFAULT '', outlet text DEFAULT '', currency char(3) DEFAULT 'USD',
    month smallint DEFAULT 0, year smallint DEFAULT 0, isactive boolean DEFAULT true,
    source_file text DEFAULT '', created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_je" UNIQUE (property_id, batch_number, gl_account_number, memo_line));

CREATE INDEX IF NOT EXISTS "ix_{p}_reg_period" ON public."{p}_payroll_register_line" (property_id, year, month);
CREATE INDEX IF NOT EXISTS "ix_{p}_sum_gl"     ON public."{p}_payroll_summary" (property_id, gl_account);
CREATE INDEX IF NOT EXISTS "ix_{p}_je_acct"    ON public."{p}_payroll_journal" (property_id, gl_account_number);

CREATE OR REPLACE VIEW public."{p}_v_payroll_reconciliation" AS
WITH reg AS (SELECT property_id,batch_number,pay_date,
        SUM(gross_amount) reg_gross,
        SUM(fit_amount+ss_ee_amount+medicare_ee_amount+state_amount) reg_ee_tax
     FROM public."{p}_payroll_register_line" GROUP BY 1,2,3),
sm AS (SELECT property_id,batch_number,pay_date,
        SUM(total_earnings_amount) sum_gross, SUM(total_net_pay_amount) sum_net_cash
     FROM public."{p}_payroll_summary" GROUP BY 1,2,3),
je AS (SELECT property_id,batch_number,SUM(debit) je_debit,SUM(credit) je_credit,
        SUM(CASE WHEN gl_account_number ~ '(R|OT|990)$' THEN debit ELSE 0 END) je_wages
     FROM public."{p}_payroll_journal" GROUP BY 1,2),
st AS (SELECT property_id,batch_number,pay_date,net_cash stats_net_cash,total_amount_debited
     FROM public."{p}_stats_summary")
SELECT sm.property_id,sm.batch_number,sm.pay_date,
    reg.reg_gross,sm.sum_gross,je.je_wages,
    sm.sum_net_cash,st.stats_net_cash,je.je_debit,je.je_credit,st.total_amount_debited,
    (reg.reg_gross=sm.sum_gross AND sm.sum_gross=je.je_wages) gross_ties,
    (sm.sum_net_cash=st.stats_net_cash) net_ties,
    (abs(je.je_debit-je.je_credit)<=0.01) je_balances
FROM sm JOIN reg USING(property_id,batch_number,pay_date)
        JOIN st USING(property_id,batch_number,pay_date)
        JOIN je ON je.property_id=sm.property_id AND je.batch_number=sm.batch_number;
'''


def ensure_schema(conn, prop):
    cur = conn.cursor()
    cur.execute(SCHEMA_DDL.replace('{p}', prop))
    conn.commit()
