"""DEPT TOTAL blocks -- the authoritative per-department grand-total line that
prints at the end of each department's employee block in the register PDF,
plus its HOURS/EARNINGS/MEMO/STATUTORY DED/VOLUNTARY DED ANALYSIS
breakdowns immediately below it. parse_register_full() treats 'DEPT TOTAL'
as a STOP marker and discards everything from there to the next employee --
this function captures that discarded content instead, landed separately
(record_type='dept_total') so nothing from the source PDF is lost. See
payroll-schema-v2 memory: this is the number to trust for department-level
GL reconciliation, not a bottom-up sum of emp_pay_rate_details.

Grid fields are kept as raw (label, amount) pairs in print order rather
than mapped to fixed field names: the ADP grid reuses the same label text
(e.g. 'REG', 'O/T') for both an hours value and an earnings value on
different rows, and the last grid row is state-specific (GA prints a 'GA'
state-tax line here; WA properties print 'FLI'/'MLI' instead) -- capturing
raw pairs is honest about what's actually printed instead of guessing a
fixed schema across states/properties.
"""
import re

from payroll.pdf_text import DEPT6_RE, extract_word_rows

# ---------------------------------------------------------------------------
# Grid/analysis-list parsing helpers, "lenient" variant (strips commas before
# matching a numeric token, so "1,046.68"-style grouped amounts are
# recognised). This is intentionally a SEPARATE implementation from the
# "strict" trio in payroll.parsers.company_totals -- their _NUMTOK regexes
# and comma-handling differ, so they parse comma-formatted amounts
# differently. Do not merge them; each was verified against its own PDF
# page's real tokens.
# ---------------------------------------------------------------------------
_NUMTOK = re.compile(r'^-?(\d+\.\d+|\.\d+|\d+)-?$')


def _flush_pair(digits, amt, neg, label_words):
    if amt is None:
        amt = (int(''.join(digits)) / 100.0) if digits else 0.0
    if neg:
        amt = -amt
    label = ' '.join(label_words).strip()
    return label, amt


def _parse_grid_row(words):
    """Sequential amount/label scanner: 'amount LABEL amount LABEL ...' ->
    [(label, amount), ...], each label describing the amount immediately
    preceding it."""
    pairs = []
    digits, amt, neg, label_words = [], None, False, []
    i = 0
    while i < len(words):
        w = words[i]
        if _NUMTOK.match(w.replace(',', '')):
            if label_words:
                if digits or amt is not None:
                    pairs.append(_flush_pair(digits, amt, neg, label_words))
                digits, amt, neg, label_words = [], None, False, []
            if w.endswith('-'):
                neg = True
                w = w[:-1]
            if '.' in w:
                amt = float(w.replace(',', ''))
            else:
                digits.append(w)
            i += 1
            continue
        label_words.append(w)
        i += 1
        if w in ('HOURS', 'EARNINGS') and i < len(words) and re.fullmatch(r'\d', words[i]):
            label_words.append(words[i])
            i += 1
    if digits or amt is not None or label_words:
        pairs.append(_flush_pair(digits, amt, neg, label_words))
    return pairs


def _parse_code_amount_list(words):
    return [{'amount': amt, 'code': label} for label, amt in _parse_grid_row(words) if label]


_PAGE_NOISE_RE = re.compile(
    r'^Reg O/T Hours3&4|^Payroll Register$|^Copyright|^Company Code:|Batch:.*PeriodEnding'
)
_ANALYSIS_HEADERS = ['HOURS ANALYSIS', 'EARNINGS ANALYSIS', 'MEMO ANALYSIS',
                      'STATUTORY DED ANALYSIS', 'VOLUNTARY DED ANALYSIS']
_DEPT_TOTAL_FIELD_MAP = {
    (0, 0): 'reg_hours', (0, 1): 'reg_earn', (0, 2): 'ot_earn',
    (0, 3): 'fit_amount', (0, 4): 'total_deductions', (0, 5): 'pays_count',
    (1, 0): 'ot_hours', (1, 1): 'earnings3', (1, 2): 'earnings4', (1, 3): 'ss_amount',
    (2, 0): 'hours3', (2, 1): 'earnings5', (2, 2): 'gross_amount', (2, 3): 'medicare_amount',
    (3, 0): 'hours4',
}


def _is_dept_total_start(r):
    ws = r['words']
    return len(ws) >= 2 and ws[0]['text'] == 'DEPT' and ws[1]['text'] == 'TOTAL' and ws[0]['x0'] < 20


def _is_company_total_start(r):
    ws = r['words']
    return len(ws) >= 2 and ws[0]['text'] == 'COMPANY' and ws[1]['text'] in ('TOTAL', 'CODE') and ws[0]['x0'] < 25


def _is_emp_name_start(r):
    if not r['words'] or r['words'][0]['x0'] >= 20:
        return False
    for w in r['words'][:6]:
        if w['text'] in ('File:', 'Dept:', 'Rate:'):
            return False
        if ',' in w['text']:
            return re.match(r"^[A-Z][A-Z' .\-]*,?$", w['text'].split(',')[0] + ',') is not None
        if not re.match(r"^[A-Z][A-Z' .\-]*$", w['text']):
            return False
    return False


def parse_dept_totals_full(pdf_path):
    """-> list of dept_total dicts: one per DEPT TOTAL block in the register."""
    rows = extract_word_rows(pdf_path)
    name_starts = [i for i, r in enumerate(rows) if _is_emp_name_start(r)]
    dept_total_starts = [i for i, r in enumerate(rows) if _is_dept_total_start(r)]
    company_total_starts = [i for i, r in enumerate(rows) if _is_company_total_start(r)]
    boundaries = sorted(set(name_starts) | set(dept_total_starts) | set(company_total_starts) | {len(rows)})

    # page-footer noise indices: the 5-line block [property name, 'Batch: ...
    # PeriodEnding ...', 'Payroll Register', 'Company Code: ...', 'Copyright
    # ...'] repeats at the bottom of every page. The property-name line has no
    # stable text to match on its own (varies per property), so it's dropped
    # by position -- the row immediately before a 'Batch:...PeriodEnding' row.
    noise_idx = set()
    for i, r in enumerate(rows):
        if _PAGE_NOISE_RE.search(r['text']):
            noise_idx.add(i)
            if 'PeriodEnding' in r['text'] and i > 0:
                noise_idx.add(i - 1)

    out = []
    for s in dept_total_starts:
        end = next(b for b in boundaries if b > s)
        block = [r for i, r in enumerate(rows[s:end], s) if i not in noise_idx]
        if not block:
            continue

        dept = ''
        grid_rows = []  # list of per-row (label, amount) pair lists -- row index is meaningful, see _DEPT_TOTAL_FIELD_MAP
        analysis_start = len(block)
        for bi, r in enumerate(block):
            if any(h + ':' in r['text'] for h in _ANALYSIS_HEADERS):
                analysis_start = bi
                break
            words = [w['text'] for w in r['words']]
            if bi == 0 and words[:2] == ['DEPT', 'TOTAL']:
                words = words[2:]
            elif bi == 1 and words and DEPT6_RE.match(words[0]):
                dept = words[0]
                words = words[1:]
            grid_rows.append(_parse_grid_row(words))

        # 'Pays' is a plain count, not a money amount -- undo the /100 cents
        # division applied to every other bare digit-group in the grid.
        def _fix(lbl, amt):
            return round(amt * 100) if lbl == 'Pays' else amt

        grid = [{'label': lbl, 'amount': _fix(lbl, amt)} for pairs in grid_rows for lbl, amt in pairs if lbl]

        # positional field map (same column layout as the Company Totals page
        # grid, see payroll.parsers.company_totals.parse_company_totals) for
        # the fields that matter for reconciliation. Anything beyond row 3
        # col 0 is state-tax and its label varies by state (GA prints one
        # 'GA' line, WA prints 'FLI'+'MLI') -- kept as a generic
        # {label: amount} dict instead of forcing a fixed column name across
        # states.
        fields = {}
        state_taxes = {}
        for ridx, pairs in enumerate(grid_rows):
            pairs = [(lbl, _fix(lbl, amt)) for lbl, amt in pairs if lbl]
            for pidx, (lbl, amt) in enumerate(pairs):
                key = _DEPT_TOTAL_FIELD_MAP.get((ridx, pidx))
                if key:
                    fields[key] = amt
                elif ridx >= 3:
                    state_taxes[lbl] = amt

        section_text = ' '.join(r['text'] for r in block[analysis_start:])
        sections = {}
        for i, header in enumerate(_ANALYSIS_HEADERS):
            next_headers = _ANALYSIS_HEADERS[i + 1:]
            lookahead = ('|'.join(re.escape(h) for h in next_headers) + '|$') if next_headers else '$'
            m = re.search(re.escape(header) + r':?\s*(.*?)(?=' + lookahead + ')', section_text, re.S)
            words = m.group(1).split() if m else []
            key = header.lower().replace(' ', '_')
            sections[key] = _parse_code_amount_list(words)

        out.append({'record_type': 'dept_total', 'dept': dept, 'grid': grid,
                     'state_taxes': state_taxes, **fields, **sections})
    return out
