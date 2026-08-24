"""Company-Totals page (last page of ADP Payroll Register) -- fixed-template
parser, verified against the real word coordinates of batch-1 page 5."""
import re

# ---------------------------------------------------------------------------
# Grid/analysis-list parsing helpers, "strict" variant (no comma-stripping
# before matching a numeric token). This is intentionally a SEPARATE
# implementation from the "lenient" trio in payroll.parsers.dept_totals --
# their _NUMTOK regexes and comma-handling differ, so they parse
# comma-formatted amounts differently. Do not merge them; each was verified
# against its own PDF page's real tokens.
# ---------------------------------------------------------------------------
_NUMTOK = re.compile(r'^-?\d+(\.\d+)?-?$')


def _flush_pair(digits, amt, neg, label_words):
    if amt is None:
        amt = (int(''.join(digits)) / 100.0) if digits else 0.0
    if neg:
        amt = -amt
    label = ' '.join(label_words).strip()
    return label, amt


def _parse_grid_row(words):
    """Sequential amount/label scanner for one row of the 4-row totals grid."""
    pairs = []
    digits, amt, neg, label_words = [], None, False, []
    i = 0
    while i < len(words):
        w = words[i]
        if _NUMTOK.match(w):
            if label_words:
                if digits or amt is not None:
                    pairs.append(_flush_pair(digits, amt, neg, label_words))
                # else: leading label with no preceding amount (e.g. the 'QVJ'
                # company-code token before the first number) -- discard it.
                digits, amt, neg, label_words = [], None, False, []
            if w.endswith('-'):
                neg = True
                w = w[:-1]
            if '.' in w:
                amt = float(w)  # already a complete decimal value (e.g. "529.80"), not a digit-group
            else:
                digits.append(w)
            i += 1
            continue
        label_words.append(w)
        i += 1
        # absorb a lone digit suffix immediately after HOURS/EARNINGS (column index label, e.g. "HOURS 3")
        if w in ('HOURS', 'EARNINGS') and i < len(words) and re.fullmatch(r'\d', words[i]):
            label_words.append(words[i])
            i += 1
    if digits or amt is not None or label_words:
        pairs.append(_flush_pair(digits, amt, neg, label_words))
    return pairs


def _parse_code_amount_list(words):
    """Generic 'amount CODE DESC amount CODE DESC ...' list parser (analysis
    sections) -- reuses the grid row scanner so multi-word codes like
    'CK1 ck1' or 'TOTAL DEDUCTIONS' are captured in full, not truncated to
    their first word."""
    return [{'amount': amt, 'code': label} for label, amt in _parse_grid_row(words) if label]


def _adp_money(s):
    s = s.strip()
    if not s:
        return 0.0
    if '.' in s:
        return float(s)
    digits = re.sub(r'\D', '', s)
    return int(digits) / 100.0 if digits else 0.0


def parse_company_totals(pdf_path):
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        target = None
        for page in pdf.pages:
            text = page.extract_text() or ''
            if 'Company Totals' in text:
                target = page
                full_text = text
                break
        if target is None:
            raise ValueError(f'No "Company Totals" page found in {pdf_path}')

        words = target.extract_words()
        rows = {}
        for w in words:
            key = round(w['top'])
            rows.setdefault(key, []).append(w)
        # the 4-row grid immediately below "COMPANY CODE" -- skip that header
        # row itself (it has no digit tokens) and any other non-numeric rows.
        row_tops = [t for t in sorted(rows) if any(_NUMTOK.match(w['text']) for w in rows[t])][:4]
        grid_rows = [[w['text'] for w in sorted(rows[t], key=lambda x: x['x0'])] for t in row_tops]

    field_map = {
        (0, 0): 'hours_reg', (0, 1): 'earnings_reg', (0, 2): 'earnings_ot',
        (0, 3): 'fit_amount', (0, 4): 'total_voluntary_deductions', (0, 5): 'pays_count',
        (1, 0): 'hours_ot', (1, 1): 'earnings3', (1, 2): 'earnings4',
        (1, 3): 'ss_amount', (1, 4): 'net_payroll_checks_amount',
        (2, 0): 'hours3', (2, 1): 'earnings5', (2, 2): 'gross_amount', (2, 3): 'medicare_amount',
        (3, 0): 'hours4', (3, 1): 'state_amount',
    }
    out = {}
    for ridx, row in enumerate(grid_rows):
        for pidx, (label, amt) in enumerate(_parse_grid_row(row)):
            key = field_map.get((ridx, pidx))
            if key:
                out[key] = amt
    out['reg_hours'] = out.pop('hours_reg', 0)
    out['ot_hours'] = out.pop('hours_ot', 0)
    out['reg_earn'] = out.pop('earnings_reg', 0)
    out['ot_earn'] = out.pop('earnings_ot', 0)
    # 'Pays' is a plain count, not a money amount -- undo the /100 cents division
    out['pays_count'] = int(round(out.get('pays_count', 0) * 100))

    def _section(header, next_headers):
        m = re.search(re.escape(header) + r':?\s*(.*?)(?=' +
                       '|'.join(re.escape(h) for h in next_headers) + '|$)', full_text, re.S)
        return m.group(1).split() if m else []

    out['hours_analysis'] = _parse_code_amount_list(
        _section('HOURS ANALYSIS', ['EARNINGS ANALYSIS']))
    out['earnings_analysis'] = _parse_code_amount_list(
        _section('EARNINGS ANALYSIS', ['MEMO ANALYSIS']))
    out['memo_analysis'] = _parse_code_amount_list(
        _section('MEMO ANALYSIS', ['STATUTORY DED ANALYSIS', 'VOLUNTARY DED ANALYSIS']))
    out['statutory_ded_analysis'] = _parse_code_amount_list(
        _section('STATUTORY DED ANALYSIS', ['VOLUNTARY DED ANALYSIS']))
    out['voluntary_ded_analysis'] = _parse_code_amount_list(
        _section('VOLUNTARY DED ANALYSIS', ['NET PAYROLL']))

    def _num(pattern, text=full_text, cast=_adp_money):
        m = re.search(pattern, text)
        return cast(m.group(1)) if m else None

    out['net_payroll_checks_amount'] = _num(r'NET PAYROLL:\s*([\d\s.]+?)\s*CHECKS') or out.get('net_payroll_checks_amount', 0)
    out['checks_count'] = int(_num(r'CHECKS:\s*(\d+)', cast=float) or 0)
    out['flagged_count'] = int(_num(r'FLAGGED:\s*(\d+)', cast=float) or 0)
    out['total_deposits'] = _num(r'TOTAL DEPOSITS:\s*([\d\s.]+?)\s*VOUCHERS') or 0
    out['vouchers_count'] = int(_num(r'VOUCHERS:\s*(\d+)', cast=float) or 0)
    out['net_cash_pays_over_1000_count'] = int(_num(r'NET CASH PAYS[\s\S]*?(\d+)\s*OR MORE', cast=float) or 0)
    out['net_voids'] = _num(r'NET VOIDS:\s*([\d\s.]+?)\s*ADJUSTMENTS') or 0
    out['evouchers_count'] = int(_num(r'eVOUCHERS:\s*(\d+)', cast=float) or 0)
    out['net_cash'] = _num(r'NET CASH:\s*([\d\s.]+?)\s*PAPER VOUCHERS') or 0
    out['paper_vouchers_printed'] = int(_num(r'PAPER VOUCHERS PRINTED:\s*(\d+)', cast=float) or 0)
    check_nums = re.findall(r'CHECK NUMBER:\s*(\S+)', full_text)
    out['starting_check_number'] = check_nums[-2] if len(check_nums) >= 2 else ''
    out['ending_check_number'] = check_nums[-1] if check_nums else ''
    m = re.search(r'ADP CHECK NUMBERS:\s*(.*?)\s*STARTING CHECK NUMBER', full_text)
    out['adp_check_numbers'] = [{'label': m.group(1).strip()}] if m else []

    m = re.search(r'Batch:\s*(\S+)\s+PeriodEnding\s*:\s*(\S+)\s+Week\s+(\d+)', full_text)
    if m:
        out['_batch_number'] = m.group(1)
        out['_period_ending_date'] = m.group(2)
        out['week_number'] = int(m.group(3))
    m = re.search(r'Service Center\s*:\s*(\S+)\s+PayDate:\s*(\S+)', full_text)
    if m:
        out['service_center'] = m.group(1)
        out['_pay_date'] = m.group(2)
    return out
