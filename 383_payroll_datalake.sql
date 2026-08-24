-- =====================================================================
--  Fairfield Inn & Suites Gainesville  (NS ID 383 / Company Code QVJ)
--  Payroll data-lake : RAW landing -> curated tables -> reconciliation
--  Target DB : integration_dev  (PostgreSQL 17)
--
--  Pattern:
--    1. RAW      : "383_payroll_raw_landing"     (all sources, verbatim JSON)
--    2. CURATED  : "383_payroll_register_line"   (employee x dept grain)
--                  "383_payroll_summary"         (department grain)
--                  "383_stats_summary"           (pay-run / bank recap grain)
--                  "383_payroll_journal"         (GL account line grain)
--    3. RECONCILE: "383_v_payroll_reconciliation"(view tying the grains)
--
--  Sample data = Batch 6908-030, Period 06/21/2026-07/04/2026,
--                Pay date 07/10/2026 (Week 28).
--  Batch grand totals: gross 10,170.38 | EE tax 1,366.68 | net cash 7,959.75
-- =====================================================================

BEGIN;

-- =====================================================================
-- 1. RAW LANDING (single consolidated table, all four sources)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public."383_payroll_raw_landing"
(
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id   integer,
    company_code  text,
    batch_number  text,
    pay_date      date,
    source_type   text,            -- 'register' | 'summary' | 'stats' | 'journal'
    source_file   text,
    row_seq       integer,
    payload       jsonb,           -- raw row / section, as parsed from the file
    ingested_at   timestamptz DEFAULT now(),
    CONSTRAINT uq_383_landing UNIQUE (source_file, source_type, row_seq)
);

-- =====================================================================
-- 2a. CURATED : REGISTER LINE  (grain = employee x department x pay-run)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public."383_payroll_register_line"
(
    id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id               integer      NOT NULL,
    company_code              text         NOT NULL DEFAULT '',
    batch_number              text         NOT NULL DEFAULT '',
    report_period_start_date  date,
    report_period_end_date    date,
    check_date                date,
    pay_date                  date,

    file_number               text         NOT NULL DEFAULT '',
    employee_name             text         NOT NULL DEFAULT '',
    department_code           text         NOT NULL DEFAULT '',
    gl_account                text         DEFAULT '',

    reg_hours                 numeric(10,2) DEFAULT 0,
    reg_amount                numeric(12,2) DEFAULT 0,
    ot_hours                  numeric(10,2) DEFAULT 0,
    ot_amount                 numeric(12,2) DEFAULT 0,
    hol_hours                 numeric(10,2) DEFAULT 0,
    hol_amount                numeric(12,2) DEFAULT 0,
    gross_amount              numeric(12,2) DEFAULT 0,

    fit_amount                numeric(12,2) DEFAULT 0,   -- federal income tax
    ss_ee_amount              numeric(12,2) DEFAULT 0,   -- social security EE
    medicare_ee_amount        numeric(12,2) DEFAULT 0,   -- medicare EE
    state_amount              numeric(12,2) DEFAULT 0,   -- GA state income tax

    earnings_detail           jsonb        DEFAULT '[]'::jsonb,
    tax_detail                jsonb        DEFAULT '[]'::jsonb,
    deduction_detail          jsonb        DEFAULT '[]'::jsonb,

    voucher_number            text         DEFAULT '',
    net_pay_amount            numeric(12,2),             -- null = direct deposit voucher

    currency                  char(3)      DEFAULT 'USD',
    month                     smallint     DEFAULT 0,
    year                      smallint     DEFAULT 0,
    isactive                  boolean      DEFAULT true,
    source_file               text         DEFAULT '',
    created_at                timestamptz  DEFAULT now(),
    updated_at                timestamptz  DEFAULT now(),
    CONSTRAINT uq_383_reg UNIQUE (property_id, batch_number, pay_date, file_number, department_code)
);

-- =====================================================================
-- 2b. CURATED : PAYROLL SUMMARY  (grain = department x pay-run)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public."383_payroll_summary"
(
    id                              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id                     integer      NOT NULL,
    company_code                    text         NOT NULL DEFAULT '',
    batch_number                    text         NOT NULL DEFAULT '',
    report_period_start_date        date,
    report_period_end_date          date,
    check_date                      date,
    pay_date                        date,

    job_code                        text         NOT NULL DEFAULT '',
    job_code_description            text         NOT NULL DEFAULT '',
    department_code                 text         DEFAULT '',
    gl_account                      text         DEFAULT '',

    total_employees                 integer      DEFAULT 0,
    female_employees                integer      DEFAULT 0,   -- from HR source (not ADP)
    male_employees                  integer      DEFAULT 0,

    earnings_summary                jsonb        DEFAULT '[]'::jsonb,
    tax_summary                     jsonb        DEFAULT '[]'::jsonb,
    deduction_summary               jsonb        DEFAULT '[]'::jsonb,
    employer_cost_summary           jsonb        DEFAULT '{}'::jsonb,
    payment_summary                 jsonb        DEFAULT '{}'::jsonb,

    total_earnings_hours            numeric(12,2) DEFAULT 0,
    total_earnings_amount           numeric(14,2) DEFAULT 0,  -- gross
    total_tax_deductions_amount     numeric(14,2) DEFAULT 0,  -- employee taxes
    total_other_deductions_amount   numeric(14,2) DEFAULT 0,  -- voluntary deductions
    total_employer_cost_amount      numeric(14,2) DEFAULT 0,  -- er taxes+benefits+match
    total_net_pay_amount            numeric(14,2) DEFAULT 0,  -- net cash

    total_ytd_hours                 numeric(12,2) DEFAULT 0,
    total_ytd_amount                numeric(14,2) DEFAULT 0,
    total_tax_deductions_ytd_amount numeric(14,2) DEFAULT 0,
    total_other_deductions_ytd_amount numeric(14,2) DEFAULT 0,

    currency                        char(3)      DEFAULT 'USD',
    month                           smallint     DEFAULT 0,
    year                            smallint     DEFAULT 0,
    isactive                        boolean      DEFAULT true,
    source_file                     text         DEFAULT '',
    created_at                      timestamptz  DEFAULT now(),
    updated_at                      timestamptz  DEFAULT now(),
    CONSTRAINT chk_383_sum_month CHECK (month BETWEEN 0 AND 12),
    CONSTRAINT uq_383_sum UNIQUE (property_id, batch_number, pay_date, job_code, gl_account)
);

-- =====================================================================
-- 2c. CURATED : STATS SUMMARY  (grain = one pay-run / bank recap)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public."383_stats_summary"
(
    id                          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id                 integer      NOT NULL,
    company_code                text         NOT NULL DEFAULT '',
    batch_number                text         NOT NULL DEFAULT '',
    report_period_end_date      date,
    pay_date                    date,
    quarter_number              smallint     DEFAULT 0,

    net_pay_checks              numeric(14,2) DEFAULT 0,   -- 529.80
    net_pay_direct_deposit      numeric(14,2) DEFAULT 0,   -- 7,429.95
    net_cash                    numeric(14,2) DEFAULT 0,   -- 7,959.75

    fed_income_tax              numeric(14,2) DEFAULT 0,   -- 451.76
    ss_ee_amount                numeric(14,2) DEFAULT 0,   -- 609.94
    ss_er_amount                numeric(14,2) DEFAULT 0,   -- 609.94
    medicare_ee_amount          numeric(14,2) DEFAULT 0,   -- 142.66
    medicare_er_amount          numeric(14,2) DEFAULT 0,   -- 142.65
    futa_amount                 numeric(14,2) DEFAULT 0,   -- 0.00
    state_income_tax            numeric(14,2) DEFAULT 0,   -- 162.32
    sui_er_amount               numeric(14,2) DEFAULT 0,   -- 0.00
    total_taxes_debited         numeric(14,2) DEFAULT 0,   -- 2,119.27

    retirement_401k             numeric(14,2) DEFAULT 0,   -- 348.79
    wage_garnishments           numeric(14,2) DEFAULT 0,   -- 230.94
    total_amount_debited        numeric(14,2) DEFAULT 0,   -- 10,658.75

    detail                      jsonb        DEFAULT '{}'::jsonb,
    currency                    char(3)      DEFAULT 'USD',
    month                       smallint     DEFAULT 0,
    year                        smallint     DEFAULT 0,
    isactive                    boolean      DEFAULT true,
    source_file                 text         DEFAULT '',
    created_at                  timestamptz  DEFAULT now(),
    updated_at                  timestamptz  DEFAULT now(),
    CONSTRAINT uq_383_stats UNIQUE (property_id, batch_number, pay_date)
);

-- =====================================================================
-- 2d. CURATED : PAYROLL JOURNAL  (grain = GL account line, NetSuite JE)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public."383_payroll_journal"
(
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id       integer      NOT NULL,
    company_code      text         DEFAULT '',
    batch_number      text         DEFAULT '',
    external_id       text         DEFAULT '',        -- HEG-00015
    transaction_date  date,
    posting_period    text         DEFAULT '',
    memo              text         DEFAULT '',
    subsidiary        text         DEFAULT '',         -- 383
    gl_account_number text         NOT NULL DEFAULT '',
    gl_account_name   text         DEFAULT '',
    debit             numeric(14,2) DEFAULT 0,
    credit            numeric(14,2) DEFAULT 0,
    memo_line         text         DEFAULT '',
    outlet            text         DEFAULT '',
    currency          char(3)      DEFAULT 'USD',
    month             smallint     DEFAULT 0,
    year              smallint     DEFAULT 0,
    isactive          boolean      DEFAULT true,
    source_file       text         DEFAULT '',
    created_at        timestamptz  DEFAULT now(),
    updated_at        timestamptz  DEFAULT now(),
    CONSTRAINT uq_383_je UNIQUE (property_id, batch_number, gl_account_number, memo_line)
);

-- =====================================================================
-- INDEXES
-- =====================================================================
CREATE INDEX IF NOT EXISTS ix_383_reg_period   ON public."383_payroll_register_line" (property_id, year, month);
CREATE INDEX IF NOT EXISTS ix_383_reg_emp      ON public."383_payroll_register_line" (property_id, file_number);
CREATE INDEX IF NOT EXISTS ix_383_sum_period   ON public."383_payroll_summary" (property_id, year, month);
CREATE INDEX IF NOT EXISTS ix_383_sum_gl       ON public."383_payroll_summary" (property_id, gl_account);
CREATE INDEX IF NOT EXISTS ix_383_je_acct      ON public."383_payroll_journal" (property_id, gl_account_number);
CREATE INDEX IF NOT EXISTS ix_383_landing_src  ON public."383_payroll_raw_landing" (property_id, source_type, batch_number);


-- #####################################################################
--  SAMPLE DATA  -  Batch 6908-030  (07/04/2026)
-- #####################################################################

-- ---------------------------------------------------------------------
-- RAW LANDING  (one row per source file; ETL normally loads row-by-row)
-- ---------------------------------------------------------------------
INSERT INTO public."383_payroll_raw_landing"
    (property_id, company_code, batch_number, pay_date, source_type, source_file, row_seq, payload)
VALUES
 (383,'QVJ','6908-030','2026-07-10','register','07.04.2026 Payroll register.pdf',1,
  '{"grand_total":{"gross":10170.38,"reg":9218.80,"ot":151.98,"hol":510.51,"net_cash":7959.75,"pays":7}}'::jsonb),
 (383,'QVJ','6908-030','2026-07-10','summary','07.04.2026 Payroll Summary.pdf',1,
  '{"grand_total":{"gross":10170.38,"fit":451.76,"ss":609.94,"med":142.66,"state":162.32,"total_ded":8273.90,"net_cash":7959.75}}'::jsonb),
 (383,'QVJ','6908-030','2026-07-10','stats','07.04.2026 Stats Summary.pdf',1,
  '{"total_amount_debited":10658.75,"total_taxes_debited":2119.27,"retirement_401k":348.79,"direct_deposit":7429.95,"check":529.80,"garnishment":230.94}'::jsonb),
 (383,'QVJ','6908-030','2026-07-10','journal','07.04.2026 Payroll.xlsx',1,
  '{"external_id":"HEG-00015","memo":"06.21.2026 to 07.04.2026","lines":22,"total_debit":12234.78,"total_credit":12234.77}'::jsonb);

-- ---------------------------------------------------------------------
-- REGISTER LINE  (8 rows = employee x department)
-- ---------------------------------------------------------------------
INSERT INTO public."383_payroll_register_line"
 (property_id,company_code,batch_number,report_period_start_date,report_period_end_date,check_date,pay_date,
  file_number,employee_name,department_code,gl_account,
  reg_hours,reg_amount,ot_hours,ot_amount,hol_hours,hol_amount,gross_amount,
  fit_amount,ss_ee_amount,medicare_ee_amount,state_amount,
  voucher_number,net_pay_amount,month,year,source_file)
VALUES
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '005753','MERCK, KENDRA L','600500','60050R',
  57.89,858.46,8.18,188.22,0,0,1046.68, 38.07,63.35,14.81,0.00,'280001',NULL,7,2026,'07.04.2026 Payroll register.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '005675','SINGLETON, TRINA YVETTE','600500','60050R',
  45.55,712.64,0,0,0,0,712.64, 20.00,37.03,8.67,0.00,'280002',NULL,7,2026,'07.04.2026 Payroll register.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '000058','DE LA CRUZ, CIRILA','603500','60350R',
  43.08,659.47,5.55,127.90,0,0,787.37, 28.51,84.21,19.70,10.20,'280003',NULL,7,2026,'07.04.2026 Payroll register.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '000058','DE LA CRUZ, CIRILA','605000','60500R',
  36.92,570.82,0,0,0,0,570.82, 0.00,0.00,0.00,0.00,'280003',NULL,7,2026,'07.04.2026 Payroll register.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '000060','WEATHERS, CANDIDA M','603500','60350R',
  54.22,830.46,4.33,100.87,0,0,931.33, 41.93,54.13,12.66,14.78,'280004',NULL,7,2026,'07.04.2026 Payroll register.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '005688','SINGLETON, JAMES BENNETT','604000','60400R',
  72.25,1054.80,1.07,24.08,0,0,1078.88, 50.13,63.09,14.76,27.15,'40431847',529.80,7,2026,'07.04.2026 Payroll register.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '000124','PATEL, DILAN AMIT','730000','73000R',
  72.00,2506.61,0,0,8.00,278.51,2785.12, 273.12,172.78,40.41,110.19,'280005',NULL,7,2026,'07.04.2026 Payroll register.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '005755','NTORE, SULEIMAN','731000','73100R',
  72.00,2025.54,0,0,8.00,232.00,2257.54, 0.00,135.35,31.65,0.00,'280006',NULL,7,2026,'07.04.2026 Payroll register.pdf');

-- ---------------------------------------------------------------------
-- PAYROLL SUMMARY  (6 rows = department)
-- ---------------------------------------------------------------------
INSERT INTO public."383_payroll_summary"
 (property_id,company_code,batch_number,report_period_start_date,report_period_end_date,check_date,pay_date,
  job_code,job_code_description,department_code,gl_account,total_employees,
  earnings_summary,tax_summary,deduction_summary,employer_cost_summary,payment_summary,
  total_earnings_hours,total_earnings_amount,total_tax_deductions_amount,total_other_deductions_amount,
  total_employer_cost_amount,total_net_pay_amount,month,year,source_file)
VALUES
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '600500','Guest Service Representatives','600500','60050R',2,
  '[{"code":"REG","hours":103.44,"amount":1571.10},{"code":"HOW","hours":8.18,"amount":188.22}]'::jsonb,
  '[{"code":"FIT","ee_amount":58.07},{"code":"SS","ee_amount":100.38},{"code":"MED","ee_amount":23.48},{"code":"GA_SIT","ee_amount":0.00}]'::jsonb,
  '[{"code":"401k","amount":20.94},{"code":"roth","amount":20.94},{"code":"CK1","label":"Direct Deposit","amount":961.96},{"code":"MEDPT","amount":58.22},{"code":"SV2","amount":200.00},{"code":"401K$","amount":50.00}]'::jsonb,
  '{"ss_er":100.38,"medicare_er":23.48,"er_med":266.18,"match_401k":65.15}'::jsonb,
  '{"gross":1759.32,"net_cash":1204.66,"total_deductions":1577.39}'::jsonb,
  111.62,1759.32,181.93,1395.46,455.19,1204.66,7,2026,'07.04.2026 Payroll Summary.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '603500','Housekeeping','603500','60350R',2,
  '[{"code":"REG","hours":97.30,"amount":1489.93},{"code":"HOW","hours":4.33,"amount":100.87},{"code":"OT","hours":5.55,"amount":127.90}]'::jsonb,
  '[{"code":"FIT","ee_amount":41.93},{"code":"SS","ee_amount":102.95},{"code":"MED","ee_amount":24.08},{"code":"GA_SIT","ee_amount":14.78}]'::jsonb,
  '[{"code":"CK1","label":"Direct Deposit","amount":1476.74},{"code":"MEDPT","amount":58.22}]'::jsonb,
  '{"ss_er":102.95,"medicare_er":24.08,"er_med":266.18}'::jsonb,
  '{"gross":1718.70,"net_cash":1476.74,"total_deductions":1534.96}'::jsonb,
  107.18,1718.70,183.74,1351.22,393.21,1476.74,7,2026,'07.04.2026 Payroll Summary.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '604000','Breakfast','604000','60400R',1,
  '[{"code":"REG","hours":72.25,"amount":1054.80},{"code":"OT","hours":1.07,"amount":24.08}]'::jsonb,
  '[{"code":"FIT","ee_amount":50.13},{"code":"SS","ee_amount":63.09},{"code":"MED","ee_amount":14.76},{"code":"GA_SIT","ee_amount":27.15}]'::jsonb,
  '[{"code":"401k","amount":97.09},{"code":"GARNSH","amount":230.94},{"code":"VLIFA","amount":4.64},{"code":"MEDPT","amount":58.22},{"code":"VISPT","amount":3.06}]'::jsonb,
  '{"ss_er":63.09,"medicare_er":14.76,"er_med":266.18,"match_401k":43.15}'::jsonb,
  '{"gross":1078.88,"net_cash":529.80,"total_deductions":393.95}'::jsonb,
  73.32,1078.88,155.13,393.95,387.18,529.80,7,2026,'07.04.2026 Payroll Summary.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '605000','Laundry','605000','60500R',1,
  '[{"code":"REG","hours":36.92,"amount":570.82}]'::jsonb,
  '[{"code":"FIT","ee_amount":28.51},{"code":"SS","ee_amount":35.39},{"code":"MED","ee_amount":8.28},{"code":"GA_SIT","ee_amount":10.20}]'::jsonb,
  '[{"code":"CK1","label":"Direct Deposit","amount":488.44}]'::jsonb,
  '{"ss_er":35.39,"medicare_er":8.28}'::jsonb,
  '{"gross":570.82,"net_cash":488.44,"total_deductions":488.44}'::jsonb,
  36.92,570.82,82.38,406.06,43.67,488.44,7,2026,'07.04.2026 Payroll Summary.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '730000','General Management','730000','73000R',1,
  '[{"code":"REG","hours":72.00,"amount":2506.61},{"code":"HOL","hours":8.00,"amount":278.51}]'::jsonb,
  '[{"code":"FIT","ee_amount":273.12},{"code":"SS","ee_amount":172.78},{"code":"MED","ee_amount":40.41},{"code":"GA_SIT","ee_amount":110.19}]'::jsonb,
  '[{"code":"CEL","label":"Reimbursement","amount":-75.00},{"code":"CK1","amount":350.00},{"code":"SV1","amount":1313.62},{"code":"SV2","amount":100.00},{"code":"SV3","amount":500.00}]'::jsonb,
  '{"ss_er":172.78,"medicare_er":40.41,"er_lif":8.04,"er_ltd":7.24,"er_std":9.82,"gtl":1.63}'::jsonb,
  '{"gross":2785.12,"net_cash":2263.62,"total_deductions":2188.62}'::jsonb,
  80.00,2785.12,596.50,1592.12,239.92,2263.62,7,2026,'07.04.2026 Payroll Summary.pdf'),
 (383,'QVJ','6908-030','2026-06-21','2026-07-04','2026-07-10','2026-07-10',
  '731000','Asst General Manager (A&G)','731000','73100R',1,
  '[{"code":"REG","hours":72.00,"amount":2025.54},{"code":"HOL","hours":8.00,"amount":232.00}]'::jsonb,
  '[{"code":"FIT","ee_amount":0.00},{"code":"SS","ee_amount":135.35},{"code":"MED","ee_amount":31.65},{"code":"GA_SIT","ee_amount":0.00}]'::jsonb,
  '[{"code":"CK1","amount":1477.40},{"code":"CK2","amount":219.61},{"code":"DNTPT","amount":12.88},{"code":"VLIFA","amount":5.45},{"code":"MEDPT","amount":58.22},{"code":"SV1","amount":299.48},{"code":"VISPT","amount":5.02}]'::jsonb,
  '{"ss_er":135.35,"medicare_er":31.65,"er_med":266.18,"er_den":14.69,"er_lif":6.72,"er_ltd":6.03,"er_std":8.18,"er_vis":3.06,"gtl":1.70}'::jsonb,
  '{"gross":2257.54,"net_cash":1996.49,"total_deductions":2090.54}'::jsonb,
  80.00,2257.54,167.00,1923.54,473.56,1996.49,7,2026,'07.04.2026 Payroll Summary.pdf');

-- ---------------------------------------------------------------------
-- STATS SUMMARY  (1 row = pay-run bank recap)
-- ---------------------------------------------------------------------
INSERT INTO public."383_stats_summary"
 (property_id,company_code,batch_number,report_period_end_date,pay_date,quarter_number,
  net_pay_checks,net_pay_direct_deposit,net_cash,
  fed_income_tax,ss_ee_amount,ss_er_amount,medicare_ee_amount,medicare_er_amount,futa_amount,
  state_income_tax,sui_er_amount,total_taxes_debited,
  retirement_401k,wage_garnishments,total_amount_debited,
  detail,month,year,source_file)
VALUES
 (383,'QVJ','6908-030','2026-07-04','2026-07-10',3,
  529.80,7429.95,7959.75,
  451.76,609.94,609.94,142.66,142.65,0.00,
  162.32,0.00,2119.27,
  348.79,230.94,10658.75,
  '{"retirement_ee":240.49,"retirement_er":108.30,"loan_repayment":51.52,"employee_txns":13,"bank_acct":"XXXXXXX5789"}'::jsonb,
  7,2026,'07.04.2026 Stats Summary.pdf');

-- ---------------------------------------------------------------------
-- PAYROLL JOURNAL  (23 rows = NetSuite JE / GL posting)
-- ---------------------------------------------------------------------
INSERT INTO public."383_payroll_journal"
 (property_id,company_code,batch_number,external_id,transaction_date,memo,subsidiary,
  gl_account_number,debit,credit,memo_line,month,year,source_file)
VALUES
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','21500',0,230.94,'ADP Garnishment Liability',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','21500',0,7959.75,'ADP Wagepay',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','21500',0,447.52,'Employee Benefits Contributions',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','21500',0,1366.68,'Employee Tax Liability',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','21500',0,1128.50,'Employer Benefits Cost',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','21500',0,752.59,'Employer Tax Liability',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','22060',0,348.79,'ADP Retirement Liability',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','51920',75.00,0,'Reimbursements',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60920',372.40,0,'Rooms Employer Tax',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60940',798.54,0,'Employer Benefits Cost',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60940',108.30,0,'Employer Match Retirement',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','73920',380.20,0,'A&G Employer Tax',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','73940',329.96,0,'Employer Benefits Cost',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','73990',510.51,0,'Supplemental Pay',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60050R',1759.32,0,'Guest Service Wages',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60350OT',127.90,0,'Room Attendant OT',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60350R',1590.80,0,'Room Attendant Wages',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60400OT',24.08,0,'Breakfast OT',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60400R',1054.80,0,'Breakfast Host Wages',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','60500R',570.82,0,'Hskping Laundry Wages',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','73000R',2506.61,0,'GM Wages',7,2026,'07.04.2026 Payroll.xlsx'),
 (383,'QVJ','6908-030','HEG-00015','2026-07-04','06.21.2026 to 07.04.2026','383','73100R',2025.54,0,'AGM Wages',7,2026,'07.04.2026 Payroll.xlsx');

-- =====================================================================
-- 3. RECONCILIATION VIEW  (ties the four grains together per pay-run)
-- =====================================================================
CREATE OR REPLACE VIEW public."383_v_payroll_reconciliation" AS
WITH reg AS (
    SELECT property_id, batch_number, pay_date,
           SUM(gross_amount) AS reg_gross,
           SUM(fit_amount+ss_ee_amount+medicare_ee_amount+state_amount) AS reg_ee_tax
    FROM public."383_payroll_register_line" GROUP BY 1,2,3),
sm AS (
    SELECT property_id, batch_number, pay_date,
           SUM(total_earnings_amount) AS sum_gross,
           SUM(total_tax_deductions_amount) AS sum_ee_tax,
           SUM(total_net_pay_amount) AS sum_net_cash,
           SUM(total_employer_cost_amount) AS sum_er_cost
    FROM public."383_payroll_summary" GROUP BY 1,2,3),
je AS (
    SELECT property_id, batch_number,
           SUM(debit) AS je_debit, SUM(credit) AS je_credit,
           -- wage-type accounts end in R / OT / 990 (supplemental); tax=920, benefit=940 excluded
           SUM(CASE WHEN gl_account_number ~ '(R|OT|990)$' THEN debit ELSE 0 END) AS je_wages
    FROM public."383_payroll_journal" GROUP BY 1,2),
st AS (
    SELECT property_id, batch_number, pay_date,
           net_cash AS stats_net_cash, total_amount_debited, total_taxes_debited
    FROM public."383_stats_summary")
SELECT
    sm.property_id, sm.batch_number, sm.pay_date,
    reg.reg_gross, sm.sum_gross, je.je_wages,
    reg.reg_ee_tax, sm.sum_ee_tax,
    sm.sum_net_cash, st.stats_net_cash,
    sm.sum_er_cost,
    je.je_debit, je.je_credit,
    st.total_amount_debited,
    (reg.reg_gross = sm.sum_gross AND sm.sum_gross = je.je_wages) AS gross_ties,
    (sm.sum_net_cash = st.stats_net_cash)                        AS net_ties,
    (abs(je.je_debit - je.je_credit) <= 0.01)                   AS je_balances
FROM sm
JOIN reg USING (property_id,batch_number,pay_date)
JOIN st  USING (property_id,batch_number,pay_date)
JOIN je  ON je.property_id=sm.property_id AND je.batch_number=sm.batch_number;

COMMIT;

-- =====================================================================
-- VERIFY (run after commit):
--   SELECT * FROM public."383_v_payroll_reconciliation";
--   Expect: reg_gross=sum_gross=je_wages=10170.38, gross_ties=t,
--           sum_net_cash=stats_net_cash=7959.75, net_ties=t, je_balances=t
-- =====================================================================
