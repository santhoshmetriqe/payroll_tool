"""ADP Payroll Register .pdf -> per-employee records, shaped like batch-1 raw_landing
register payloads.

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

from payroll.amounts import adp_amount, band_amount, parse_coded
from payroll.pdf_text import extract_word_rows


def parse_register_full(pdf_path):
    """-> list of employee dicts, shaped like batch-1 raw_landing register payloads."""
    rows = extract_word_rows(pdf_path)
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
