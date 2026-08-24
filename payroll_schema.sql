-- =====================================================================
-- Payroll summary table  (per property / pay-run / job-code grain)
-- Source: ADP Payroll Register + Summary + Stats Summary + NetSuite JE
-- Reconciles to GL (HEQ General Ledger) and USALI Income Statement.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.payroll_summary
(
    id                              bigint       GENERATED ALWAYS AS IDENTITY,

    -- ---- Entity / pay-run identity ----------------------------------
    property_id                     integer      NOT NULL,               -- 383 (NS ID)
    company_code                    text         NOT NULL DEFAULT '',    -- QVJ
    batch_number                    text         NOT NULL DEFAULT '',    -- 6908-030
    report_period_start_date        date,                                -- 2026-06-21
    report_period_end_date          date,                                -- 2026-07-04
    check_date                      date,                                -- pay/check date
    pay_date                        date,                                -- 2026-07-10

    -- ---- Job / GL mapping -------------------------------------------
    job_code                        text         NOT NULL DEFAULT '',    -- 600500
    job_code_description            text         NOT NULL DEFAULT '',    -- Guest Service Reps
    department_code                 text         DEFAULT '',             -- 600500
    gl_account                      text         DEFAULT '',             -- 60050R

    -- ---- Headcount ---------------------------------------------------
    total_employees                 integer      DEFAULT 0,
    female_employees                integer      DEFAULT 0,              -- from HR source, not ADP
    male_employees                  integer      DEFAULT 0,

    -- ---- Detail (typed JSONB documents) -----------------------------
    earnings_summary                jsonb        DEFAULT '[]'::jsonb,
    tax_summary                     jsonb        DEFAULT '[]'::jsonb,
    deduction_summary               jsonb        DEFAULT '[]'::jsonb,
    employer_cost_summary           jsonb        DEFAULT '{}'::jsonb,    -- employer burden
    payment_summary                 jsonb        DEFAULT '{}'::jsonb,

    -- ---- Current-period totals --------------------------------------
    total_earnings_hours            numeric(12,2) DEFAULT 0.00,
    total_earnings_amount           numeric(14,2) DEFAULT 0.00,   -- gross
    total_tax_deductions_amount     numeric(14,2) DEFAULT 0.00,   -- employee taxes
    total_other_deductions_amount   numeric(14,2) DEFAULT 0.00,   -- voluntary deductions
    total_employer_cost_amount      numeric(14,2) DEFAULT 0.00,   -- er taxes+benefits+match
    total_net_pay_amount            numeric(14,2) DEFAULT 0.00,   -- net cash

    -- ---- YTD totals --------------------------------------------------
    total_ytd_hours                 numeric(12,2) DEFAULT 0.00,
    total_ytd_amount                numeric(14,2) DEFAULT 0.00,
    total_tax_deductions_ytd_amount numeric(14,2) DEFAULT 0.00,
    total_other_deductions_ytd_amount numeric(14,2) DEFAULT 0.00,

    -- ---- Period keys / housekeeping ---------------------------------
    currency                        char(3)      DEFAULT 'USD',
    month                           smallint     DEFAULT 0,
    year                            smallint     DEFAULT 0,
    isactive                        boolean      DEFAULT true,

    -- ---- Lineage / audit --------------------------------------------
    source_file                     text         DEFAULT '',
    created_at                      timestamptz  DEFAULT now(),
    updated_at                      timestamptz  DEFAULT now(),

    CONSTRAINT payroll_summary_pkey PRIMARY KEY (id),
    CONSTRAINT chk_month CHECK (month BETWEEN 0 AND 12),
    CONSTRAINT chk_year  CHECK (year BETWEEN 0 AND 9999)
);

-- Prevent duplicate loads of the same job code within one pay run
CREATE UNIQUE INDEX IF NOT EXISTS uq_payroll_natural
    ON public.payroll_summary (property_id, batch_number, pay_date, job_code, gl_account);

CREATE INDEX IF NOT EXISTS ix_payroll_period ON public.payroll_summary (property_id, year, month);
CREATE INDEX IF NOT EXISTS ix_payroll_dates  ON public.payroll_summary (report_period_start_date, report_period_end_date);
CREATE INDEX IF NOT EXISTS ix_payroll_gl     ON public.payroll_summary (property_id, gl_account);

-- =====================================================================
-- Sample INSERT  -  Fairfield Inn & Suites Gainesville (383 / QVJ)
-- Batch 6908-030, Period 06/21/2026-07/04/2026, Pay date 07/10/2026
-- =====================================================================

INSERT INTO public.payroll_summary (
    property_id, company_code, batch_number,
    report_period_start_date, report_period_end_date, check_date, pay_date,
    job_code, job_code_description, department_code, gl_account,
    total_employees, female_employees, male_employees,
    earnings_summary, tax_summary, deduction_summary, employer_cost_summary, payment_summary,
    total_earnings_hours, total_earnings_amount,
    total_tax_deductions_amount, total_other_deductions_amount,
    total_employer_cost_amount, total_net_pay_amount,
    currency, month, year, source_file
) VALUES

-- 600500  Guest Service Representatives  -> GL 60050R
(383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
 '600500','Guest Service Representatives','600500','60050R',
 2,0,0,
 '[{"code":"REG","hours":103.44,"amount":1571.10},{"code":"HOW","hours":8.18,"amount":188.22}]'::jsonb,
 '[{"code":"FIT","ee_amount":58.07},{"code":"SS","ee_amount":100.38},{"code":"MED","ee_amount":23.48},{"code":"GA_SIT","ee_amount":0.00}]'::jsonb,
 '[{"code":"401k","amount":20.94},{"code":"roth","amount":20.94},{"code":"CK1","label":"Direct Deposit","amount":961.96},{"code":"MEDPT","amount":58.22},{"code":"SV2","amount":200.00},{"code":"401K$","amount":50.00}]'::jsonb,
 '{"ss_er":100.38,"medicare_er":23.48,"er_med":266.18,"match_401k":65.15}'::jsonb,
 '{"gross":1759.32,"net_cash":1204.66,"total_deductions":1577.39}'::jsonb,
 111.62,1759.32, 181.93,1395.46, 455.19,1204.66,
 'USD',7,2026,'07.04.2026 Payroll Summary.pdf'),

-- 603500  Housekeeping  -> GL 60350R / 60350OT
(383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
 '603500','Housekeeping','603500','60350R',
 2,0,0,
 '[{"code":"REG","hours":97.30,"amount":1489.93},{"code":"HOW","hours":4.33,"amount":100.87},{"code":"OT","hours":5.55,"amount":127.90}]'::jsonb,
 '[{"code":"FIT","ee_amount":41.93},{"code":"SS","ee_amount":102.95},{"code":"MED","ee_amount":24.08},{"code":"GA_SIT","ee_amount":14.78}]'::jsonb,
 '[{"code":"CK1","label":"Direct Deposit","amount":1476.74},{"code":"MEDPT","amount":58.22}]'::jsonb,
 '{"ss_er":102.95,"medicare_er":24.08,"er_med":266.18}'::jsonb,
 '{"gross":1718.70,"net_cash":1476.74,"total_deductions":1534.96}'::jsonb,
 107.18,1718.70, 183.74,1351.22, 393.21,1476.74,
 'USD',7,2026,'07.04.2026 Payroll Summary.pdf'),

-- 604000  Breakfast  -> GL 60400R / 60400OT
(383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
 '604000','Breakfast','604000','60400R',
 1,0,0,
 '[{"code":"REG","hours":72.25,"amount":1054.80},{"code":"OT","hours":1.07,"amount":24.08}]'::jsonb,
 '[{"code":"FIT","ee_amount":50.13},{"code":"SS","ee_amount":63.09},{"code":"MED","ee_amount":14.76},{"code":"GA_SIT","ee_amount":27.15}]'::jsonb,
 '[{"code":"401k","amount":97.09},{"code":"GARNSH","amount":230.94},{"code":"VLIFA","amount":4.64},{"code":"MEDPT","amount":58.22},{"code":"VISPT","amount":3.06}]'::jsonb,
 '{"ss_er":63.09,"medicare_er":14.76,"er_med":266.18,"match_401k":43.15}'::jsonb,
 '{"gross":1078.88,"net_cash":529.80,"total_deductions":393.95}'::jsonb,
 73.32,1078.88, 155.13,393.95, 387.18,529.80,
 'USD',7,2026,'07.04.2026 Payroll Summary.pdf'),

-- 605000  Laundry  -> GL 60500R
(383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
 '605000','Laundry','605000','60500R',
 1,0,0,
 '[{"code":"REG","hours":36.92,"amount":570.82}]'::jsonb,
 '[{"code":"FIT","ee_amount":28.51},{"code":"SS","ee_amount":35.39},{"code":"MED","ee_amount":8.28},{"code":"GA_SIT","ee_amount":10.20}]'::jsonb,
 '[{"code":"CK1","label":"Direct Deposit","amount":488.44}]'::jsonb,
 '{"ss_er":35.39,"medicare_er":8.28}'::jsonb,
 '{"gross":570.82,"net_cash":488.44,"total_deductions":488.44}'::jsonb,
 36.92,570.82, 82.38,406.06, 43.67,488.44,
 'USD',7,2026,'07.04.2026 Payroll Summary.pdf'),

-- 730000  General Management  -> GL 73000R (+73990 Supplemental)
(383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
 '730000','General Management','730000','73000R',
 1,0,0,
 '[{"code":"REG","hours":72.00,"amount":2506.61},{"code":"HOL","hours":8.00,"amount":278.51}]'::jsonb,
 '[{"code":"FIT","ee_amount":273.12},{"code":"SS","ee_amount":172.78},{"code":"MED","ee_amount":40.41},{"code":"GA_SIT","ee_amount":110.19}]'::jsonb,
 '[{"code":"CEL","label":"Reimbursement","amount":-75.00},{"code":"CK1","amount":350.00},{"code":"SV1","amount":1313.62},{"code":"SV2","amount":100.00},{"code":"SV3","amount":500.00}]'::jsonb,
 '{"ss_er":172.78,"medicare_er":40.41,"er_lif":8.04,"er_ltd":7.24,"er_std":9.82,"gtl":1.63}'::jsonb,
 '{"gross":2785.12,"net_cash":2263.62,"total_deductions":2188.62}'::jsonb,
 80.00,2785.12, 596.50,1592.12, 239.92,2263.62,
 'USD',7,2026,'07.04.2026 Payroll Summary.pdf'),

-- 731000  Asst General Manager (A&G)  -> GL 73100R
(383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
 '731000','Asst General Manager (A&G)','731000','73100R',
 1,0,0,
 '[{"code":"REG","hours":72.00,"amount":2025.54},{"code":"HOL","hours":8.00,"amount":232.00}]'::jsonb,
 '[{"code":"FIT","ee_amount":0.00},{"code":"SS","ee_amount":135.35},{"code":"MED","ee_amount":31.65},{"code":"GA_SIT","ee_amount":0.00}]'::jsonb,
 '[{"code":"CK1","amount":1477.40},{"code":"CK2","amount":219.61},{"code":"DNTPT","amount":12.88},{"code":"VLIFA","amount":5.45},{"code":"MEDPT","amount":58.22},{"code":"SV1","amount":299.48},{"code":"VISPT","amount":5.02}]'::jsonb,
 '{"ss_er":135.35,"medicare_er":31.65,"er_med":266.18,"er_den":14.69,"er_lif":6.72,"er_ltd":6.03,"er_std":8.18,"er_vis":3.06,"gtl":1.70}'::jsonb,
 '{"gross":2257.54,"net_cash":1996.49,"total_deductions":2090.54}'::jsonb,
 80.00,2257.54, 167.00,1923.54, 473.56,1996.49,
 'USD',7,2026,'07.04.2026 Payroll Summary.pdf');

-- =====================================================================
-- Reconciliation check (should return the batch grand totals):
--   gross 10,170.38 | employee tax 1,366.68 | net cash 7,959.75
-- =====================================================================
-- SELECT sum(total_earnings_amount)        AS gross,
--        sum(total_tax_deductions_amount)  AS employee_tax,
--        sum(total_net_pay_amount)         AS net_cash,
--        sum(total_employer_cost_amount)   AS employer_burden
-- FROM public.payroll_summary
-- WHERE property_id = 383 AND batch_number = '6908-030';
