#!/usr/bin/env python3
"""Thin re-export shim -- see payroll/parsers/ for the implementation.

Kept so existing scripts/notebooks doing
`from payroll_pdf_parser import parse_register_full, ...` keep working.
"""
from payroll.parsers.company_totals import parse_company_totals
from payroll.parsers.dept_totals import parse_dept_totals_full
from payroll.parsers.register import parse_register_full
from payroll.parsers.stats import parse_stats_full
from payroll.parsers.summary import parse_summary_full
