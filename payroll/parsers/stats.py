"""Stats (thin re-shape of the already-working payroll.legacy.parsers.parse_stats)."""
from payroll.legacy.parsers import parse_stats as _parse_stats_old


def parse_stats_full(pdf_path):
    d = _parse_stats_old(pdf_path)
    total_taxes = round(sum(x or 0 for x in [
        d.get('federal_income_tax'), d.get('ss_ee'), d.get('ss_er'),
        d.get('medicare_ee'), d.get('medicare_er'), d.get('state_income_tax')]), 2)
    return {
        'record_type': 'stats_recap',
        'net_pay_checks': d.get('adp_check') or 0,
        'adp_direct_deposit': d.get('adp_direct_deposit') or 0,
        'total_net_pay_liability_net_cash': d.get('net_cash') or 0,
        'federal_income_tax': d.get('federal_income_tax') or 0,
        'ss_ee': d.get('ss_ee') or 0, 'ss_er': d.get('ss_er') or 0,
        'medicare_ee': d.get('medicare_ee') or 0, 'medicare_er': d.get('medicare_er') or 0,
        'futa': 0,
        'state_income_tax': d.get('state_income_tax') or 0,
        'state_ga': {},
        'total_taxes_debited': total_taxes,
        'retirement_401k': d.get('retirement_401k') or 0,
        'wage_garnishments': d.get('wage_garnishments') or 0,
        'total_amount_debited': d.get('total_amount_debited') or 0,
    }
