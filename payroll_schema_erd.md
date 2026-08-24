# Payroll ETL — Entity Relationship Diagram

Tables are parameterised by property id (`{p}`, e.g. `383`). There are no enforced
foreign keys between them — they are logically linked by the shared business key
`(property_id, batch_number, pay_date)`, as used in the `v_payroll_reconciliation` view.

```mermaid
erDiagram
    PAYROLL_RAW_LANDING {
        bigint id PK
        int property_id
        text company_code
        text batch_number
        date pay_date
        text source_type
        text source_file
        int row_seq
        jsonb payload
        timestamptz ingested_at
    }

    PAYROLL_REGISTER_LINE {
        bigint id PK
        int property_id
        text company_code
        text batch_number
        date report_period_start_date
        date report_period_end_date
        date check_date
        date pay_date
        text file_number
        text employee_name
        text department_code
        text gl_account
        numeric reg_hours
        numeric reg_amount
        numeric ot_hours
        numeric ot_amount
        numeric hol_hours
        numeric hol_amount
        numeric gross_amount
        numeric fit_amount
        numeric ss_ee_amount
        numeric medicare_ee_amount
        numeric state_amount
        jsonb earnings_detail
        jsonb tax_detail
        jsonb deduction_detail
        boolean deductions_reconciled
        text voucher_number
        numeric net_pay_amount
        char currency
        smallint month
        smallint year
        boolean isactive
        text source_file
    }

    PAYROLL_SUMMARY {
        bigint id PK
        int property_id
        text company_code
        text batch_number
        date report_period_start_date
        date report_period_end_date
        date check_date
        date pay_date
        text job_code
        text job_code_description
        text department_code
        text gl_account
        int total_employees
        int female_employees
        int male_employees
        jsonb earnings_summary
        jsonb tax_summary
        jsonb deduction_summary
        jsonb employer_cost_summary
        jsonb payment_summary
        jsonb cafeteria_125
        numeric total_earnings_hours
        numeric total_earnings_amount
        numeric total_tax_deductions_amount
        numeric total_other_deductions_amount
        numeric total_employer_cost_amount
        numeric total_net_pay_amount
        char currency
        smallint month
        smallint year
        boolean isactive
        text source_file
    }

    STATS_SUMMARY {
        bigint id PK
        int property_id
        text company_code
        text batch_number
        date report_period_end_date
        date pay_date
        smallint quarter_number
        numeric net_pay_checks
        numeric net_pay_direct_deposit
        numeric net_cash
        numeric fed_income_tax
        numeric ss_ee_amount
        numeric ss_er_amount
        numeric medicare_ee_amount
        numeric medicare_er_amount
        numeric futa_amount
        numeric state_income_tax
        numeric sui_er_amount
        numeric total_taxes_debited
        numeric retirement_401k
        numeric wage_garnishments
        numeric total_amount_debited
        jsonb detail
        char currency
        smallint month
        smallint year
        boolean isactive
        text source_file
    }

    PAYROLL_JOURNAL {
        bigint id PK
        int property_id
        text company_code
        text batch_number
        text external_id
        date transaction_date
        text posting_period
        text memo
        text subsidiary
        text gl_account_number
        text gl_account_name
        numeric debit
        numeric credit
        text memo_line
        text outlet
        char currency
        smallint month
        smallint year
        boolean isactive
        text source_file
    }

    PAYROLL_RAW_LANDING     }o--|| STATS_SUMMARY : "property_id + batch_number + pay_date"
    PAYROLL_REGISTER_LINE   }o--|| STATS_SUMMARY : "property_id + batch_number + pay_date"
    PAYROLL_SUMMARY         }o--|| STATS_SUMMARY : "property_id + batch_number + pay_date"
    PAYROLL_JOURNAL         }o--|| STATS_SUMMARY : "property_id + batch_number"
```

## Notes
- **Unique keys per table** (enforce one row per logical grain):
  - `payroll_raw_landing`: (source_file, source_type, row_seq)
  - `payroll_register_line`: (property_id, batch_number, pay_date, file_number, department_code)
  - `payroll_summary`: (property_id, batch_number, pay_date, job_code, gl_account)
  - `stats_summary`: (property_id, batch_number, pay_date) — one row per batch
  - `payroll_journal`: (property_id, batch_number, gl_account_number, memo_line)
- **Reconciliation view** `{p}_v_payroll_reconciliation` joins `register_line`, `summary`, `stats_summary`, and `journal` on `(property_id, batch_number, pay_date)` to verify gross pay, net cash, and JE balance all tie out.
- Source: `D:\payrol_register\payroll_etl.py` lines 427–529.
