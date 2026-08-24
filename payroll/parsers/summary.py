"""ADP Payroll Summary .pdf -> list of department dicts, shaped like batch-1
raw_landing summary payloads."""
import re

from payroll.amounts import adp_amount, band_amount, parse_coded
from payroll.pdf_text import DEPT6_RE, extract_word_rows


def parse_summary_full(pdf_path):
    """-> list of department dicts, shaped like batch-1 raw_landing summary payloads."""
    rows = extract_word_rows(pdf_path)
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
