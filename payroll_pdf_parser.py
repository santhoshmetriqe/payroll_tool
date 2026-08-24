#!/usr/bin/env python3
"""
payroll_pdf_parser.py -- parses ADP Payroll Register / Payroll Summary /
Stats Summary PDFs into the same JSON shape already used in
{p}_payroll_raw_landing for batch 1 (personnel/rate_lines/voluntary/memo for
register; dept/memo/gross/taxes/deductions/cafeteria_125 for summary).

Register scope (deliberately limited): dept/rate label-to-value pairing in
the ADP register is NOT reliably derivable from row order for employees who
worked multiple departments in one pay period -- verified with a real
counterexample (DE LA CRUZ, batch 1: a 'Rate:' label immediately following
one dept's values actually belongs to an EARLIER dept's values). Guessing
would silently corrupt department-level P&L, so multi-dept employees are
flagged 'needs_manual_review' with no rate_lines instead of a guessed split.
Single-department employees (the common case) are parsed with full rate/dept
fidelity, verified byte-for-byte against real batch-1 data.
"""
import re
import pdfplumber

from payroll_etl import (
    adp_amount, band_amount, parse_coded, _word_lines, _register_bands, parse_stats as _parse_stats_old,
)

NAME_START_RE = re.compile(r"^[A-Z][A-Z' .\-]*,[A-Z' .\-]+$")
DEPT6_RE = re.compile(r'^\d{6}$')

# ---------------------------------------------------------------------------
# Shared grid/analysis-list parsing helpers (same technique used by
# payroll_ingestor.parse_company_totals for the Company Totals page -- the
# DEPT TOTAL blocks embedded per-department in the register use an identical
# column grid, just without a couple of company-only fields).
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


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
def _all_word_rows(pdf_path):
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, 1):
            for y, ws in _word_lines(page):
                out.append({'page': pageno, 'y': y, 'words': ws, 'text': ' '.join(w['text'] for w in ws)})
    return out


def parse_register_full(pdf_path):
    """-> list of employee dicts, shaped like batch-1 raw_landing register payloads."""
    rows = _all_word_rows(pdf_path)
    bands = None
    for r in rows:
        xs = {w['text']: w for w in r['words']}
        if 'Federal' in xs and 'State/Local' in xs:
            fed = xs['Federal']['x0']; st = xs['State/Local']['x0']
            bands = {'gross': (369, fed - 1), 'stat': (fed - 1, st - 1), 'state': (st - 1, st + 70)}

    # Page-footer 'Batch: ... PeriodEnding : MM/DD/YYYY Week N' / 'PayDate:
    # MM/DD/YYYY' repeat on every page, identical for the whole batch -- same
    # fields as parse_summary_full, attached to every employee record here.
    full_text = ' '.join(r['text'] for r in rows)
    m = re.search(r'PeriodEnding\s*:\s*(\d{2}/\d{2}/\d{4})', full_text)
    period_ending_date = m.group(1) if m else None
    m = re.search(r'PayDate\s*:\s*(\d{2}/\d{2}/\d{4})', full_text)
    pay_date_field = m.group(1) if m else None

    STOP = ('DEPT TOTAL', 'COMPANY TOTAL', 'GRAND TOTAL', 'HOURS ANALYSIS',
            'ANALYSIS DEPT', 'MEMO ANALYSIS', 'VOLUNTARY DED', 'COMPANY CODE')

    # segment rows into employee blocks: a block starts at a name row (comma,
    # x0 ~14-40) and ends at the next name row or a STOP-marker row.
    # Multi-word surnames (e.g. "DE LA CRUZ,CIRILA") split the comma across
    # several leading words, so scan forward from x0<20 until a comma token
    # or a non-name token (digit / label) appears.
    def _is_name_start(r):
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

    starts = [i for i, r in enumerate(rows) if _is_name_start(r)]

    employees = []
    for k, s in enumerate(starts):
        # the surname may split its comma across several leading word-tokens
        # on the start row (e.g. "DE" "LA" "CRUZ,CIRILA") -- consume tokens
        # until the one containing the comma, the rest of that row are value
        # tokens (hours/earnings), not name.
        start_words = rows[s]['words']
        comma_idx = next((i for i, w in enumerate(start_words) if ',' in w['text']), 0)
        end_idx = comma_idx + 1
        # absorb a trailing middle-initial/name token right after the comma word
        while end_idx < len(start_words) and re.match(r"^[A-Z']{1,15}$", start_words[end_idx]['text']):
            end_idx += 1
        name_parts = [w['text'] for w in start_words[:end_idx]]
        rest_of_start = start_words[end_idx:]
        name_parts[comma_idx] = name_parts[comma_idx].rstrip(',')
        j = s + 1
        while j < len(rows) and rows[j]['words'] and rows[j]['words'][0]['x0'] < 20 and \
                re.match(r"^[A-Z' \-]+$", rows[j]['words'][0]['text']) and \
                not any(t in rows[j]['text'] for t in ('File:', 'Dept:', 'Rate:')):
            name_parts.append(rows[j]['words'][0]['text'])
            j += 1
        end = starts[k + 1] if k + 1 < len(starts) else len(rows)
        block = rows[s:end]
        trimmed = []
        for b in block:
            if any(m in b['text'] for m in STOP):
                break
            trimmed.append(b)
        block = trimmed

        # ADP sometimes prints a second 'File:' section under the same name
        # header with no repeated name row (seen: supplemental/FLSA-retro pay
        # lines) -- without this split, the first File:'s employee vanishes,
        # silently merged into the second and undercounting total gross.
        file_idxs = [i for i, b in enumerate(block) if re.search(r'File:\s*\d', b['text'])]
        full_name = ' '.join(name_parts)
        if len(file_idxs) > 1:
            bounds = file_idxs[1:] + [len(block)]
            sub_start = 0
            for bidx, fi in enumerate(bounds):
                sub_block = block[sub_start:fi]
                extra = rest_of_start if sub_start == 0 else []
                employees.append(_parse_employee_block(full_name, extra, sub_block, bands))
                sub_start = fi
        else:
            employees.append(_parse_employee_block(full_name, rest_of_start, block, bands))
    for e in employees:
        e['period_ending_date'] = period_ending_date
        e['pay_date_field'] = pay_date_field
    return employees


def _parse_employee_block(name, first_row_extra_words, block, bands):
    emp = {'record_type': 'employee', 'personnel': {'name': name, 'file': '', 'dept': ''},
           'gross': 0.0, 'net_pay': 0.0, 'voucher': '', 'total_work_hrs': 0.0,
           'statutory': {}, 'voluntary': [], 'memo': [], 'rate_lines': []}

    dept_seen = []
    current_dept = None
    pending = []  # value-rows awaiting a Rate: label

    def emit(rate):
        for e in pending:
            e['dept'] = e['dept'] or current_dept
            e['rate'] = rate
            emp['rate_lines'].append(e)
        pending.clear()

    for idx, b in enumerate(block):
        t = b['text']
        words = b['words'] if idx else (b['words'][:1] + first_row_extra_words if first_row_extra_words else b['words'])

        fm = re.search(r'File:\s*(\d+)', t)
        if fm:
            emp['personnel']['file'] = fm.group(1)

        dm = re.search(r'Dept:\s*(\d{6})', t)
        if dm:
            current_dept = dm.group(1)
            if current_dept not in dept_seen:
                dept_seen.append(current_dept)

        rm = re.search(r'Rate:\s*(\d+)\s+(\d+)', t)
        if rm:
            rate = int(rm.group(1) + rm.group(2)) / 10000.0
            emit(rate)

        reg_earn = band_amount(words, 210, 252)
        if reg_earn is not None:
            entry = {'reg_earn': reg_earn, 'dept': dm.group(1) if dm else None}
            rh = band_amount(words, 95, 128)
            if rh is not None:
                entry['reg_hours'] = rh
            oh = band_amount([w for w in words if not (182 <= w['x0'] < 220 and not w['text'].replace(',','').isdigit())], 128, 182)
            if oh is not None:
                entry['ot_hours'] = oh
            oc = next((w['text'] for w in words if 182 <= w['x0'] < 210 and w['text'].isalpha()), None)
            if oc:
                entry['ot_code'] = oc
            oe = band_amount(words, 252, 369)
            if oe is not None:
                entry['ot_earn'] = oe
            oc2 = next((w['text'] for w in words if 316 <= w['x0'] < 340 and w['text'].isalpha()), None)
            if oc2 and not oc:
                entry['ot_code'] = oc2
            pending.append(entry)

        if 'Total Work Hrs:' in t:
            g = band_amount(words, *bands['gross']) if bands else None
            if g is not None:
                emp['gross'] = g
            twh = [w['text'] for w in words if 150 < w['x0'] < 210 and w['text'].replace(',', '').isdigit()]
            if twh:
                emp['total_work_hrs'] = adp_amount(' '.join(twh)) or 0.0

        for kw in ('FIT', 'SS', 'MED'):
            if re.search(r'\b' + kw + r'\b', t) and bands:
                v = band_amount(words, *bands['stat'])
                if v is not None:
                    emp['statutory'][kw] = v
        if re.search(r'\bGA\b', t) and bands:
            v = band_amount(words, *bands['state'])
            if v is not None and v < 100000:
                emp['statutory']['GA'] = v

        if 'Voucher#' in t or 'Check#' in t:
            if idx + 1 < len(block):
                nxt_words = block[idx + 1]['words']
                num = next((w['text'] for w in nxt_words if w['x0'] > 700 and re.match(r'^\d+$', w['text'])), None)
                if num:
                    emp['voucher'] = num

        voltoks = [w['text'] for w in words if 550 <= w['x0'] < 720]
        d, m = parse_coded(voltoks)
        for dd in d:
            emp['voluntary'].append({'code': dd['code'], 'amount': dd['amount']})
        for mm in m:
            emp['memo'].append(mm)

    # fallback: some employees (seen: salaried/exempt) have no 'Total Work Hrs:'
    # line at all, so gross/total_work_hrs never get set from the band lookup --
    # derive them from the rate-line earnings instead of silently leaving 0.
    if not emp['gross'] and emp['rate_lines']:
        emp['gross'] = round(sum((rl.get('reg_earn') or 0) + (rl.get('ot_earn') or 0) for rl in emp['rate_lines']), 2)
    if not emp['total_work_hrs'] and emp['rate_lines']:
        emp['total_work_hrs'] = round(sum((rl.get('reg_hours') or 0) + (rl.get('ot_hours') or 0) for rl in emp['rate_lines']), 2)

    emp['personnel']['dept'] = '/'.join(dept_seen) if dept_seen else current_dept or ''
    if len(dept_seen) > 1:
        emp['needs_manual_review'] = True
        emp['review_reason'] = ('multi-department employee: rate/dept line pairing is not '
                                 'reliably derivable from the register layout -- rate_lines below are '
                                 'parsed as-is but their dept/rate pairing should not be trusted; '
                                 'promote_register() skips inserting them into curated tables for this reason')
    g = emp['gross'] or 0
    emp['voluntary'] = [d for d in emp['voluntary'] if d['amount'] is not None and abs(d['amount']) <= g + 0.01]
    return emp


# ---------------------------------------------------------------------------
# DEPT TOTAL blocks -- the authoritative per-department grand-total line that
# prints at the end of each department's employee block in the register PDF,
# plus its HOURS/EARNINGS/MEMO/STATUTORY DED/VOLUNTARY DED ANALYSIS
# breakdowns immediately below it. parse_register_full() treats 'DEPT TOTAL'
# as a STOP marker and discards everything from there to the next employee --
# this function captures that discarded content instead, landed separately
# (record_type='dept_total') so nothing from the source PDF is lost. See
# payroll-schema-v2 memory: this is the number to trust for department-level
# GL reconciliation, not a bottom-up sum of emp_pay_rate_details.
#
# Grid fields are kept as raw (label, amount) pairs in print order rather
# than mapped to fixed field names: the ADP grid reuses the same label text
# (e.g. 'REG', 'O/T') for both an hours value and an earnings value on
# different rows, and the last grid row is state-specific (GA prints a 'GA'
# state-tax line here; WA properties print 'FLI'/'MLI' instead) -- capturing
# raw pairs is honest about what's actually printed instead of guessing a
# fixed schema across states/properties.
# ---------------------------------------------------------------------------
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
    rows = _all_word_rows(pdf_path)
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
        # grid, see payroll_ingestor.parse_company_totals) for the fields that
        # matter for reconciliation. Anything beyond row 3 col 0 is state-tax
        # and its label varies by state (GA prints one 'GA' line, WA prints
        # 'FLI'+'MLI') -- kept as a generic {label: amount} dict instead of
        # forcing a fixed column name across states.
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


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def parse_summary_full(pdf_path):
    """-> list of department dicts, shaped like batch-1 raw_landing summary payloads."""
    rows = _all_word_rows(pdf_path)
    dept_starts = [i for i, r in enumerate(rows) if r['words'] and DEPT6_RE.match(r['words'][0]['text'])
                   and r['words'][0]['x0'] < 20]
    # The LAST department's block would otherwise run to end-of-document,
    # swallowing the company-level '* * GRAND TOTAL * *' section (its own
    # Hours3,4/Earnings3,4,5/Memo/Deduction Analysis headers included) as if
    # it were that department's data -- bound it there too.
    grand_total_starts = [i for i, r in enumerate(rows) if 'GRAND TOTAL' in r['text']]

    # Page-footer 'Batch: ... PeriodEnding : MM/DD/YYYY Week N' and
    # 'Company Code: ... PayDate: MM/DD/YYYY' repeat on every page, identical
    # for the whole batch -- pull them once. No period START date is printed
    # anywhere in this report (only the ending date), so that field is left
    # for the caller to leave NULL rather than guess a pay-frequency.
    full_text = ' '.join(r['text'] for r in rows)
    m = re.search(r'PeriodEnding\s*:\s*(\d{2}/\d{2}/\d{4})', full_text)
    period_ending_date = m.group(1) if m else None
    m = re.search(r'PayDate\s*:\s*(\d{2}/\d{2}/\d{4})', full_text)
    pay_date_field = m.group(1) if m else None

    depts = []
    for k, s in enumerate(dept_starts):
        end = dept_starts[k + 1] if k + 1 < len(dept_starts) else len(rows)
        gt = next((i for i in grand_total_starts if s < i < end), None)
        if gt is not None:
            end = gt
        block = rows[s:end]
        d = _parse_dept_block(block)
        d['period_ending_date'] = period_ending_date
        d['pay_date_field'] = pay_date_field
        depts.append(d)
    return depts


def _parse_dept_block(block):
    hdr = block[0]
    words = hdr['words']
    dept = words[0]['text']
    d = {'record_type': 'dept_summary', 'dept': dept, 'dept_name': '', 'memo': [],
         'gross': 0.0, 'taxes': {}, 'net_cash': 0.0, 'reg_hours': 0.0, 'ot_hours': 0.0,
         'reg_earn': 0.0, 'ot_earn': 0.0, 'hours34': 0.0, 'earn34': 0.0, 'earn5': 0.0,
         'hours34_analysis': [], 'earnings345_analysis': [],
         'pct_of_co': 0.0, 'deductions': [],
         'cafeteria_125': {'items': [], 'total': 0.0}, 'taxable_analysis': {}, 'total_deductions': 0.0}

    # Hours/Earnings grid: 'Reg /O/T Hours3&4  Reg /O/T Earn3&4 Earn5' prints
    # as one header row per page, then each dept's REG values sit on the dept
    # code's own row and any O/T values (when nonzero) sit on the very next
    # row -- same stacked-row pattern as the register's per-employee rate
    # lines. Column x0-bands (verified against property 474 batch 1, depts
    # 600000/600100/600200/600400): hours col ~120-195, Hours3&4 ~195-245,
    # earnings col ~245-315, Earn3&4 ~315-385, Earn5 ~385-420 (narrower than
    # the header's own x-span to avoid colliding with the '%  of CO' pct
    # column at x~420-460 on the following row).
    # Stop at the first 'ANALYSIS DEPT:' marker row -- everything after it is
    # the Hours3,4/Earnings3,4,5/Memo/Deduction breakdown sections, whose own
    # code-amount columns can fall inside the same x-bands as the main grid
    # (e.g. a lone breakdown entry landing at x~122-156 looks identical to a
    # second 'O/T hours' grid row) and would otherwise be misread as O/T.
    grid_end = next((i for i, r in enumerate(block) if 'ANALYSIS' in r['text']), len(block))
    hrs_vals, earn_vals, hours34_vals, earn34_vals, earn5_vals = [], [], [], [], []
    for r in block[:grid_end]:
        ws = r['words']
        h = band_amount(ws, 120, 195)
        if h is not None:
            hrs_vals.append(h)
        h34 = band_amount(ws, 195, 245)
        if h34 is not None:
            hours34_vals.append(h34)
        e = band_amount(ws, 245, 315)
        if e is not None:
            earn_vals.append(e)
        e34 = band_amount(ws, 315, 385)
        if e34 is not None:
            earn34_vals.append(e34)
        e5 = band_amount(ws, 385, 420)
        if e5 is not None:
            earn5_vals.append(e5)

    if hrs_vals:
        d['reg_hours'] = hrs_vals[0]
    if len(hrs_vals) > 1:
        d['ot_hours'] = hrs_vals[1]
    if earn_vals:
        d['reg_earn'] = earn_vals[0]
    if len(earn_vals) > 1:
        d['ot_earn'] = earn_vals[1]
    if hours34_vals:
        d['hours34'] = hours34_vals[0]
    if earn34_vals:
        d['earn34'] = earn34_vals[0]
    if earn5_vals:
        d['earn5'] = earn5_vals[0]

    # Hours3,4 Analysis / Earnings3,4,5 Analysis: the code-by-code breakdown
    # printed below 'ANALYSIS DEPT:'. ADP wraps the code list into two print
    # sub-columns per section to save vertical space (e.g. 'CAM CA MP'/'HOW'
    # on the left, 'SIC SICK' on the right) -- and depts with more codes get
    # more rows, so this walks left-to-right per row within the section's
    # combined x-range (both sub-columns together) rather than assuming a
    # fixed row count. Each section is self-terminating at its OWN 'Total'
    # token (by x-position: hours' Total sits inside its x-range, earnings'
    # inside its own) -- this is what keeps it from either (a) reading past
    # a short section into the unrelated State/FLI/MLI analysis further down
    # the page, which reuses similar x-coordinates, or (b) getting truncated
    # early by a SHORTER neighboring column's (Memo/Deduction) own Total line
    # if that row-boundary were shared, which an earlier fixed-row-count
    # version of this code got wrong for taller multi-row breakdowns.
    # Verified property 474 batch 1 dept 600000: entries sum to 16.62
    # (hours34) and 563.86 (earn34) exactly, matching the main grid above.
    def _section_pairs(rows, x_lo, x_hi):
        out = []
        for r in rows:
            toks = sorted((w for w in r['words'] if x_lo <= w['x0'] < x_hi), key=lambda w: w['x0'])
            i, n = 0, len(toks)
            stop = False
            while i < n:
                code_words = []
                while i < n and not toks[i]['text'].replace(',', '').replace('.', '').isdigit():
                    code_words.append(toks[i]['text'])
                    i += 1
                if not code_words:
                    i += 1
                    continue
                amt_words = []
                while i < n and toks[i]['text'].replace(',', '').replace('.', '').isdigit():
                    amt_words.append(toks[i]['text'])
                    i += 1
                code = ' '.join(code_words)
                if code == 'Total':
                    stop = True
                    break
                if amt_words:
                    out.append({'code': code, 'amount': adp_amount(' '.join(amt_words))})
            if stop:
                break
        return out

    # Hours3,4 Analysis and Earnings3,4,5 Analysis are independent headers --
    # ADP omits the Hours header entirely for a dept whose hours34 is $0.00
    # (e.g. a flat non-worked earning like tips/gratuity), so each section
    # must be located and sliced on its own rather than assuming they always
    # co-occur on the same header row.
    h_idx = next((i for i, r in enumerate(block) if 'Hours3,4Analysis' in r['text'].replace(' ', '')), None)
    e_idx = next((i for i, r in enumerate(block) if 'Earnings3,4,5Analysis' in r['text'].replace(' ', '')), None)
    if h_idx is not None:
        d['hours34_analysis'] = _section_pairs(block[h_idx + 1:], 15, 269)
    if e_idx is not None:
        d['earnings345_analysis'] = _section_pairs(block[e_idx + 1:], 269, 515)

    gross_tok = next((w['text'] for w in words if w['text'].replace(',', '').replace('.', '').isdigit() and '.' in w['text']), None)
    if gross_tok:
        d['gross'] = adp_amount(gross_tok) or 0.0
    fit = band_amount(words, 510, 546)
    if fit is not None:
        d['taxes']['FIT'] = fit
    tail = band_amount(words, 670, 720)
    if tail is not None:
        d['total_deductions'] = tail

    dept_name_words = []
    for r in block[1:4]:
        w0 = r['words'][0] if r['words'] else None
        if w0 and w0['x0'] < 20 and re.match(r"^[A-Za-z][A-Za-z]*$", w0['text']):
            dept_name_words.append(w0['text'])
        for w in r['words']:
            if 420 <= w['x0'] < 460 and '%' in w['text']:
                pct = re.match(r'(\d+)', w['text'])
                if pct:
                    prev = next((x['text'] for x in r['words'] if x['x1'] <= w['x0'] and 415 <= x['x0'] < 425), '')
                    d['pct_of_co'] = adp_amount((prev + pct.group(1)).strip()) or 0.0
        ss = band_amount(r['words'], 510, 546)
        if ss is not None and any(w['text'] == 'SS' for w in r['words']):
            d['taxes']['SS'] = ss
        med = band_amount(r['words'], 510, 546)
        if med is not None and any(w['text'] == 'MED' for w in r['words']):
            d['taxes']['MED'] = med
    d['dept_name'] = ' '.join(dept_name_words)
    d['taxes'].setdefault('STATE', 0.0)

    for r in block:
        if any(w['text'] == 'NET' for w in r['words']) and any(w['text'] == 'CASH:' for w in r['words']):
            v = band_amount(r['words'], 555, 620)
            if v is not None:
                d['net_cash'] = v

    # memo analysis column (x ~ 518-620) -- entries prefixed 'N-'/'M-' like the register
    memo_toks = []
    for r in block:
        memo_toks.extend([w['text'] for w in r['words'] if 515 <= w['x0'] < 645])
    _, memos = parse_coded(memo_toks)
    d['memo'] = memos

    # deduction analysis column (x ~ 648-770), stops once "Cafeteria" header seen
    ded_rows, caf_rows, in_caf = [], [], False
    for r in block:
        if any(w['text'] == 'Cafeteria' for w in r['words']):
            in_caf = True
            continue
        toks = [(w['x0'], w['text']) for w in r['words'] if 648 <= w['x0'] < 775]
        if not toks:
            continue
        (caf_rows if in_caf else ded_rows).append(toks)

    def _pairs(rowlist):
        out = []
        for toks in rowlist:
            toks = sorted(toks)
            code = next((t for x, t in toks if x < 665 and not t.replace(',', '').replace('.', '').isdigit()), None)
            label = next((t for x, t in toks if 665 <= x < 700), '')
            amt_toks = [t for x, t in toks if x >= 700 and t.replace(',', '').replace('.', '').isdigit()]
            if code and amt_toks:
                out.append({'code': code, 'label': label.lstrip('-'), 'amount': adp_amount(' '.join(amt_toks))})
        return out

    ded_pairs = _pairs(ded_rows)
    d['deductions'] = [p for p in ded_pairs if p['code'] not in ('Total',)]
    caf_pairs = _pairs(caf_rows)
    caf_items = [{'code': p['code'], 'label': p['label'], 'amount': p['amount']} for p in caf_pairs if p['code'] != 'Total']
    d['cafeteria_125'] = {'items': caf_items, 'total': round(sum(x['amount'] or 0 for x in caf_items), 2)}

    # federal taxable analysis (x ~ 15-300, rows containing 'Federal'/'Social Security'/'Medicare')
    for r in block:
        txt = r['text']
        if 'Federal' in txt and 'Taxable' not in txt and 'FUTA' not in txt:
            v = band_amount(r['words'], 160, 260)
            if v is not None:
                d['taxable_analysis']['federal_taxable'] = v
        if 'Social Security-EE' in txt:
            v = band_amount(r['words'], 160, 260)
            if v is not None:
                d['taxable_analysis']['ss_taxable'] = v
        if 'Medicare-EE' in txt:
            v = band_amount(r['words'], 160, 260)
            if v is not None:
                d['taxable_analysis']['medicare_taxable'] = v
        if re.match(r'^GA\b', txt):
            v = band_amount(r['words'], 130, 210)
            if v is not None:
                d['taxable_analysis']['ga_taxable'] = v

    return d


# ---------------------------------------------------------------------------
# Stats (thin re-shape of the already-working payroll_etl.parse_stats)
# ---------------------------------------------------------------------------
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
