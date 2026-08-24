-- =============================================================================
-- Payroll schema v2 (property-prefixed tables, {p} = property_id, e.g. 383)
-- Supersedes: {p}_payroll_register_line (dropped), {p}_payroll_summary (renamed)
-- Unchanged:  {p}_payroll_raw_landing, {p}_stats_summary, {p}_payroll_journal
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Migration: drop objects being restructured BEFORE creating the new shapes.
-- The old {p}_payroll_summary was dept-level with a different column set
-- (job_code, earnings_summary jsonb, ...) -- it's superseded by
-- {p}_payroll_summary_by_dept, repopulated fresh via promote_summary_by_dept().
-- {p}_payroll_summary is repurposed for the batch-level "Company Totals" page.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS public."{p}_v_payroll_reconciliation";
DROP TABLE IF EXISTS public."{p}_payroll_register_line";
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='public' AND table_name='{p}_payroll_summary')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='{p}_payroll_summary'
                        AND column_name='gross_amount') THEN
        EXECUTE 'DROP TABLE public."{p}_payroll_summary"';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1. Per-employee header (one row per employee per batch)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public."{p}_emp_payroll_details" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    pay_date date, report_period_start_date date, report_period_end_date date, check_date date,
    file_number text DEFAULT '', employee_name text DEFAULT '',
    department_code text DEFAULT '',   -- raw/verbatim from register header; may be combined e.g. '603500/605000' for multi-dept employees. NOT for GL allocation -- use emp_pay_rate_details.department_code for that.
    voucher_number text DEFAULT '',
    hol_hours numeric(10,2) DEFAULT 0, hol_amount numeric(12,2) DEFAULT 0,
    total_work_hrs numeric(10,2) DEFAULT 0,
    gross_amount numeric(12,2) DEFAULT 0, net_pay_amount numeric(12,2) DEFAULT 0,
    fit_amount numeric(12,2) DEFAULT 0, ss_ee_amount numeric(12,2) DEFAULT 0,
    medicare_ee_amount numeric(12,2) DEFAULT 0, state_amount numeric(12,2) DEFAULT 0,
    memo_detail jsonb DEFAULT '[]'::jsonb,  -- informational only (KWAGES/KHOURS/KMATCH/PTO/VJ1 etc). NEVER additive to gross - verified memo totals != gross.
    currency char(3) DEFAULT 'USD', month smallint DEFAULT 0, year smallint DEFAULT 0,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_empdet" UNIQUE (property_id, batch_number, pay_date, file_number));

-- ---------------------------------------------------------------------------
-- 2. Per-employee pay-rate lines (one row per rate segment; carries its OWN
--    department_code, since one employee can work multiple depts in a period)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public."{p}_emp_pay_rate_details" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    pay_date date, report_period_end_date date, check_date date,
    emp_payroll_detail_id bigint REFERENCES public."{p}_emp_payroll_details"(id) ON DELETE CASCADE,
    file_number text DEFAULT '', employee_name text DEFAULT '',
    department_code text DEFAULT '',   -- the authoritative dept for GL allocation of THIS rate line
    rate_seq smallint NOT NULL,
    rate numeric(10,4) DEFAULT 0,
    reg_hours numeric(10,2) DEFAULT 0, reg_earn numeric(12,2) DEFAULT 0,
    ot_code text DEFAULT '', ot_hours numeric(10,2) DEFAULT 0, ot_earn numeric(12,2) DEFAULT 0,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_ratedet" UNIQUE (property_id, batch_number, pay_date, file_number, rate_seq));

-- ---------------------------------------------------------------------------
-- 3. Per-employee deductions (voluntary only -- statutory stays in
--    emp_payroll_details columns and is NOT duplicated here to avoid double
--    counting. cafeteria-125 items are NOT separate rows -- they are a
--    non-additive sub-breakdown of specific voluntary codes (confirmed:
--    DEN/FSA/MED/VIS == DNTPT/MEDFSA/MEDPT/VISPT, same dollar amounts) so
--    they're flagged via is_cafeteria_125 on the matching voluntary row.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public."{p}_emp_deduction_details" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    pay_date date,
    emp_payroll_detail_id bigint REFERENCES public."{p}_emp_payroll_details"(id) ON DELETE CASCADE,
    file_number text DEFAULT '', employee_name text DEFAULT '',
    deduction_type text NOT NULL DEFAULT 'voluntary',  -- 'voluntary' only for now; kept for future flexibility
    deduction_code text NOT NULL, deduction_amount numeric(12,2) DEFAULT 0,
    is_cafeteria_125 boolean DEFAULT false,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_empded" UNIQUE (property_id, batch_number, pay_date, file_number, deduction_code));

-- ---------------------------------------------------------------------------
-- 4. Department-level summary (renamed from old {p}_payroll_summary)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public."{p}_payroll_summary_by_dept" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    pay_date date, report_period_start_date date, report_period_end_date date, check_date date,
    department_code text DEFAULT '', department_title text DEFAULT '', pct_of_company numeric(6,2) DEFAULT 0,
    reg_hours numeric(10,2) DEFAULT 0, reg_earn numeric(12,2) DEFAULT 0,
    ot_hours numeric(10,2) DEFAULT 0, ot_earn numeric(12,2) DEFAULT 0,
    hours34 numeric(10,2) DEFAULT 0, earn34 numeric(12,2) DEFAULT 0, earn5 numeric(12,2) DEFAULT 0,
    gross_amount numeric(12,2) DEFAULT 0, net_cash numeric(12,2) DEFAULT 0,
    fit_amount numeric(12,2) DEFAULT 0, ss_amount numeric(12,2) DEFAULT 0,
    medicare_amount numeric(12,2) DEFAULT 0, state_amount numeric(12,2) DEFAULT 0,
    total_deductions numeric(12,2) DEFAULT 0,
    taxable_analysis jsonb DEFAULT '{}'::jsonb,
    hours34_analysis jsonb DEFAULT '[]'::jsonb,      -- code breakdown of hours34, sums to it
    earnings345_analysis jsonb DEFAULT '[]'::jsonb,  -- code breakdown of earn34+earn5, sums to earn34 (earn5 usually 0)
    memo_detail jsonb DEFAULT '[]'::jsonb,          -- informational only, non-additive
    deduction_detail jsonb DEFAULT '[]'::jsonb,      -- full voluntary list at dept level (reference/reconciliation)
    cafeteria_125_detail jsonb DEFAULT '{}'::jsonb,  -- sub-breakdown only, NOT additive to deduction_detail
    currency char(3) DEFAULT 'USD', month smallint DEFAULT 0, year smallint DEFAULT 0,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_summdept" UNIQUE (property_id, batch_number, pay_date, department_code));

-- ---------------------------------------------------------------------------
-- 5. Batch-level "Company Totals" summary (one row per batch; last page of
--    the ADP Payroll Register). This REPLACES the old {p}_payroll_summary.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public."{p}_payroll_summary" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    pay_date date, period_ending_date date, week_number smallint, service_center text DEFAULT '',
    reg_hours numeric(10,2) DEFAULT 0, ot_hours numeric(10,2) DEFAULT 0,
    hours3 numeric(10,2) DEFAULT 0, hours4 numeric(10,2) DEFAULT 0,
    reg_earn numeric(12,2) DEFAULT 0, ot_earn numeric(12,2) DEFAULT 0,
    earnings3 numeric(12,2) DEFAULT 0, earnings4 numeric(12,2) DEFAULT 0, earnings5 numeric(12,2) DEFAULT 0,
    gross_amount numeric(14,2) DEFAULT 0,
    fit_amount numeric(12,2) DEFAULT 0, ss_amount numeric(12,2) DEFAULT 0,
    medicare_amount numeric(12,2) DEFAULT 0, state_amount numeric(12,2) DEFAULT 0,
    total_voluntary_deductions numeric(14,2) DEFAULT 0,
    net_payroll_checks_amount numeric(12,2) DEFAULT 0, total_deposits numeric(14,2) DEFAULT 0,
    net_voids numeric(12,2) DEFAULT 0, net_cash numeric(14,2) DEFAULT 0,
    checks_count integer DEFAULT 0, flagged_count integer DEFAULT 0,
    vouchers_count integer DEFAULT 0, pays_count integer DEFAULT 0,
    net_cash_pays_over_1000_count integer DEFAULT 0,
    starting_check_number text DEFAULT '', ending_check_number text DEFAULT '',
    evouchers_count integer DEFAULT 0, paper_vouchers_printed integer DEFAULT 0,
    hours_analysis jsonb DEFAULT '[]'::jsonb, earnings_analysis jsonb DEFAULT '[]'::jsonb,
    memo_analysis jsonb DEFAULT '[]'::jsonb, statutory_ded_analysis jsonb DEFAULT '[]'::jsonb,
    voluntary_ded_analysis jsonb DEFAULT '[]'::jsonb, adp_check_numbers jsonb DEFAULT '[]'::jsonb,
    currency char(3) DEFAULT 'USD', month smallint DEFAULT 0, year smallint DEFAULT 0,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_summ" UNIQUE (property_id, batch_number, pay_date));

-- ---------------------------------------------------------------------------
-- 6. Stats summary -- UNCHANGED, standalone, not linked to any other table.
--    (kept exactly as it was; data loaded as-is from the ADP Stats Summary doc)
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- 7. Journal. line_seq added because (gl_account_number, memo_line) is NOT
--    a unique natural key -- confirmed real case (474, batch 7229-030): two
--    separate JE lines both post to account 60920 with memo_line
--    'Rooms Employer Tax' but different amounts (5695.68 and 7.02).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public."{p}_payroll_journal" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    external_id text DEFAULT '', transaction_date date, posting_period text DEFAULT '',
    memo text DEFAULT '', subsidiary text DEFAULT '',
    gl_account_number text DEFAULT '', gl_account_name text DEFAULT '',
    debit numeric(14,2) DEFAULT 0, credit numeric(14,2) DEFAULT 0,
    memo_line text DEFAULT '', outlet text DEFAULT '', currency char(3) DEFAULT 'USD',
    line_seq integer DEFAULT 1,
    month smallint DEFAULT 0, year smallint DEFAULT 0, isactive boolean DEFAULT true,
    source_file text DEFAULT '', created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());

ALTER TABLE public."{p}_payroll_journal" ADD COLUMN IF NOT EXISTS line_seq integer DEFAULT 1;
ALTER TABLE public."{p}_payroll_journal" DROP CONSTRAINT IF EXISTS "uq_{p}_je";
ALTER TABLE public."{p}_payroll_journal" ADD CONSTRAINT "uq_{p}_je"
    UNIQUE (property_id, batch_number, gl_account_number, memo_line, line_seq);

-- ---------------------------------------------------------------------------
-- 8. Raw landing -- UNCHANGED
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public."{p}_payroll_raw_landing" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer, company_code text, batch_number text, pay_date date,
    source_type text, source_file text, row_seq integer, payload jsonb,
    ingested_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_landing" UNIQUE (source_file, source_type, row_seq));

-- ---------------------------------------------------------------------------
-- 9. Global reference: ADP department -> GL account -> NetSuite account.
--    NOT property-prefixed -- shared master mapping across all properties.
--    Sourced from "ADP Master Mapping Working.xlsx".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.adp_gl_dept_mapping (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gl_account integer NOT NULL,
    fnb_outlet text,
    adp_dept integer NOT NULL,
    department_title text,
    division text,
    ns_account text NOT NULL,
    ns_account_name text,
    property_id integer,      -- nullable: only set where a property-specific mapping file confirms this
    company_code text,        -- (dept, ns_account) belongs to that property; most of the 428 generic rows
    hotel_name text,          -- are shared/template rows not yet attributed to a specific property.
    isactive boolean DEFAULT true,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_adp_gl_dept_mapping" UNIQUE (adp_dept, ns_account));

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS "ix_{p}_empdet_period"  ON public."{p}_emp_payroll_details" (property_id, year, month);
CREATE INDEX IF NOT EXISTS "ix_{p}_ratedet_emp"     ON public."{p}_emp_pay_rate_details" (emp_payroll_detail_id);
CREATE INDEX IF NOT EXISTS "ix_{p}_ratedet_dept"    ON public."{p}_emp_pay_rate_details" (property_id, department_code);
CREATE INDEX IF NOT EXISTS "ix_{p}_empded_emp"      ON public."{p}_emp_deduction_details" (emp_payroll_detail_id);
CREATE INDEX IF NOT EXISTS "ix_{p}_summdept_gl"     ON public."{p}_payroll_summary_by_dept" (property_id, department_code);
CREATE INDEX IF NOT EXISTS "ix_{p}_je_acct"         ON public."{p}_payroll_journal" (property_id, gl_account_number);
CREATE INDEX IF NOT EXISTS "ix_adp_gl_dept_dept"    ON public.adp_gl_dept_mapping (adp_dept);

-- ---------------------------------------------------------------------------
-- Reconciliation view (register/summary/journal tie-out; stats reported
-- alongside for reference only, per instruction that stats stays unlinked)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public."{p}_v_payroll_reconciliation" AS
WITH emp AS (
    SELECT property_id, batch_number, pay_date, SUM(gross_amount) reg_gross,
           SUM(fit_amount + ss_ee_amount + medicare_ee_amount + state_amount) reg_ee_tax
    FROM public."{p}_emp_payroll_details" GROUP BY 1,2,3),
sm AS (
    SELECT property_id, batch_number, pay_date, gross_amount sum_gross, net_cash sum_net_cash
    FROM public."{p}_payroll_summary"),
je AS (
    SELECT property_id, batch_number, SUM(debit) je_debit, SUM(credit) je_credit,
           SUM(CASE WHEN gl_account_number ~ '(R|OT|990)$' THEN debit ELSE 0 END) je_wages
    FROM public."{p}_payroll_journal" GROUP BY 1,2),
st AS (
    SELECT property_id, batch_number, pay_date, net_cash stats_net_cash, total_amount_debited
    FROM public."{p}_stats_summary")
SELECT sm.property_id, sm.batch_number, sm.pay_date,
    emp.reg_gross, sm.sum_gross, je.je_wages,
    sm.sum_net_cash, st.stats_net_cash, je.je_debit, je.je_credit, st.total_amount_debited,
    (emp.reg_gross = sm.sum_gross AND sm.sum_gross = je.je_wages) gross_ties,
    (sm.sum_net_cash = st.stats_net_cash) net_ties,
    (abs(je.je_debit - je.je_credit) <= 0.01) je_balances
FROM sm JOIN emp USING (property_id, batch_number, pay_date)
        LEFT JOIN st USING (property_id, batch_number, pay_date)
        JOIN je ON je.property_id = sm.property_id AND je.batch_number = sm.batch_number;
