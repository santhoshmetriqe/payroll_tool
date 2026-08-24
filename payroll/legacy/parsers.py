"""Schema-v1 parsers (superseded by payroll.parsers, see [[payroll-schema-v2]] memory), kept
because their helpers are still reused: parse_journal by the v2 ingestor, parse_stats by
payroll.parsers.stats.

Register scope note (still true for this v1 parser too): dept/rate label-to-value pairing for
employees who worked multiple departments in one pay period is NOT reliably derivable from row
order.
"""
import re
import sys

import openpyxl

try:
    import psycopg2
except ImportError:
    psycopg2 = None

from payroll.amounts import adp_amount, band_amount, parse_coded
from payroll.pdf_text import all_lines, extract_word_rows

DEPT_HDR_RE = re.compile(r'^(\d{6})\s')
NAME_FRAG_RE = re.compile(r"^[A-Z][A-Z' .\-]*(?:,[A-Z' .\-]+)?$")


def trailing_amount(line, keyword):
    """Return the ADP amount that appears immediately before KEYWORD on a line."""
    m = re.search(r'((?:\d[\d,]*\s)*\d{2}|\.\d{2}|[\d,]+\.\d{2})\s*' + re.escape(keyword), line)
    return adp_amount(m.group(1)) if m else None


def parse_journal(xlsx_path):
    """NetSuite payroll JE .xlsx  ->  list[dict] (fully reliable)."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip() if h else '' for h in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    def col(r, name):
        return r[idx[name]] if name in idx and idx[name] < len(r) else None
    out = []
    for r in rows[1:]:
        if not any(r):
            continue
        acct = col(r, 'GL Account Number')
        if acct is None:
            continue
        out.append({
            'external_id': col(r, 'External ID') or '',
            'transaction_date': col(r, 'Transaction Date'),
            'posting_period': col(r, 'Posting Period') or '',
            'memo': col(r, 'Memo') or '',
            'subsidiary': str(col(r, 'Subsidiary') or ''),
            'gl_account_number': str(acct),
            'gl_account_name': col(r, 'GL Account Name') or '',
            'debit': float(col(r, 'Debit') or 0),
            'credit': float(col(r, 'Credit') or 0),
            'memo_line': col(r, 'Memo (Line)') or '',
            'outlet': col(r, 'Outlet') or '',
        })
    return out


def parse_stats(pdf_path):
    """ADP Statistical Summary .pdf -> dict of recap values."""
    lines = [l for _, _, l in all_lines(pdf_path)]
    text = '\n'.join(lines)
    def val(label):
        for l in lines:
            if l.startswith(label) or (label in l and l.strip().startswith(label.split()[0])):
                after = l.split(label, 1)[1]
                m = re.search(r'((?:\d[\d,]*\s)*\d{2}|[\d,]+\.\d{2})', after)
                if m:
                    return adp_amount(m.group(1))
        return None
    d = {
        'federal_income_tax': val('Federal Income Tax'),
        'ss_ee': val('Social Security - EE'),
        'ss_er': val('Social Security - ER'),
        'medicare_ee': val('Medicare - EE'),
        'medicare_er': val('Medicare - ER'),
        'state_income_tax': val('State Income Tax'),
        'total_amount_debited': val('Total Amount Debited From Your Accounts'),
        'net_cash': None,
    }
    # net cash / subtotal net pay
    for l in lines:
        if 'Subtotal Net Pay' in l or 'Total Net Pay Liability' in l:
            m = re.search(r'([\d,]+\.\d{2}|(?:\d[\d,]*\s)*\d{2})\s*$', l)
            if m:
                d['net_cash'] = adp_amount(m.group(1))
    for key, kw in [('adp_direct_deposit', 'ADP Direct Deposit'),
                    ('adp_check', 'ADP Check'),
                    ('wage_garnishments', 'Wage Garnishments'),
                    ('retirement_401k', '401K/Retirement')]:
        d[key] = val(kw)
    d['raw_lines'] = lines
    return d


def _first_decimal(line):
    m = re.search(r'([\d,]+\.\d{2})', line)
    return adp_amount(m.group(1)) if m else None


def trailing_after(line, label):
    after = line.split(label, 1)[1] if label in line else ''
    return adp_amount(after.strip()) if after.strip() else None


def parse_summary(pdf_path):
    """ADP Payroll Summary .pdf -> {'departments':[...], 'grand_total':{...}}.
    Anchored on the 6-digit department header line and its ANALYSIS block."""
    lines = [l for _, _, l in all_lines(pdf_path)]
    depts = {}
    grand = {}
    cur = None
    for l in lines:
        m = DEPT_HDR_RE.match(l)
        if m:
            cur = m.group(1)
            depts.setdefault(cur, {'dept': cur, 'raw_lines': []})
            # header: DEPT reg ot ... gross ... FIT ... total_ded
            depts[cur]['gross'] = _first_decimal(l)
            depts[cur]['header'] = l
        if '* * GRAND TOTAL * *' in l:
            cur = 'GRAND'
            grand['raw_lines'] = []
        if cur and cur != 'GRAND':
            depts[cur]['raw_lines'].append(l)
            if 'NET CASH:' in l:
                depts[cur]['net_cash'] = trailing_after(l, 'NET CASH:')
            for kw, key in [(' FIT', 'FIT'), (' SS', 'SS'), (' MED', 'MED')]:
                v = trailing_amount(l, kw.strip())
                if v is not None:
                    depts[cur].setdefault('taxes', {})[key] = v
        if cur == 'GRAND':
            grand['raw_lines'].append(l)
    return {'departments': list(depts.values()), 'grand_total': grand}


def _register_bands(lines):
    """Derive GROSS / statutory / state x-bands from the column header row."""
    bands = {'gross': (369, 428), 'stat': (428, 483), 'state': (483, 556)}
    for l in lines:
        xs = {w['text']: w for w in l['words']}
        if 'Federal' in xs and 'State/Local' in xs:
            fed = xs['Federal']['x0']; st = xs['State/Local']['x0']
            e5 = next((w['x1'] for w in l['words'] if w['text'] == '5'), fed - 60)
            bands = {'gross': (e5 + 1, fed - 1), 'stat': (fed - 1, st - 1),
                     'state': (st - 1, st + 70)}
            break
    return bands


def parse_register(pdf_path):
    """ADP Payroll Register .pdf -> per-employee records via column x-bands.
    Employees are segmented on 'File:' lines; gross/FIT/SS/MED are read from
    their specific columns.  Each employee keeps raw_lines (loss-free landing)."""
    lines = extract_word_rows(pdf_path)
    bands = _register_bands(lines)
    is_name = lambda t: bool(NAME_FRAG_RE.match(t)) and ',' in t.split('  ')[0]

    # locate employee starts: the name line just above each "File:" line
    file_idx = [i for i, l in enumerate(lines) if re.search(r'File:\s*\d', l['text'])]
    starts = []
    for fi in file_idx:
        # walk up from the File: line: the surname (comma) line is the base,
        # any name-continuation lines below it (no comma) are appended.
        base, cont, j = None, [], fi - 1
        while j >= 0 and (fi - j) <= 3:
            frag = re.match(r"^[A-Z][A-Z' .\-]*(?:,[A-Z' .\-]*)?", lines[j]['text'])
            if not frag:
                break
            txt = frag.group(0).strip()
            if ',' in txt:
                base = txt.rstrip(',')
                break
            elif txt.isupper() and len(txt) <= 15:
                cont.insert(0, txt)
            else:
                break
            j -= 1
        name = ' '.join(([base] if base else []) + cont).strip()
        starts.append((j if base else fi, name, fi))

    employees = []
    for k, (s_idx, name, fi) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        block = lines[s_idx:end]
        # the employee's own record ends where the dept/company roll-ups begin
        STOP = ('DEPT TOTAL', 'COMPANY TOTAL', 'GRAND TOTAL', 'HOURS ANALYSIS',
                'ANALYSIS DEPT', 'MEMO ANALYSIS', 'VOLUNTARY DED')
        own = []
        for b in block:
            if any(s in b['text'] for s in STOP):
                break
            own.append(b)
        block = own
        emp = {'name': name, 'file': None, 'depts': [], 'statutory': {},
               'gross': None, 'total_work_hrs': None,
               'earnings': [], 'deductions': [], 'memo': [],
               'raw_lines': [b['text'] for b in block]}
        for b in block:
            t = b['text']
            # earnings rate line (has a value in the Reg-earnings band)
            re_amt = band_amount(b['words'], 210, 252)
            if re_amt is not None:
                ent = {'reg_earn': re_amt}
                for key, lo, hi in (('reg_hours', 95, 128), ('ot_hours', 128, 210),
                                    ('ot_earn', 252, 369)):
                    v = band_amount(b['words'], lo, hi)
                    if v is not None:
                        ent[key] = v
                emp['earnings'].append(ent)
            # voluntary deductions + memo: bounded band excludes the Voucher#/
            # check-number column (x~726) on the right and taxes on the left
            voltoks = [w['text'] for w in b['words'] if 550 <= w['x0'] < 720]
            d, m = parse_coded(voltoks)
            emp['deductions'].extend(d)
            emp['memo'].extend(m)
            fm = re.search(r'File:\s*(\d+)', t)
            if fm:
                emp['file'] = fm.group(1)
            for dm in re.finditer(r'Dept:\s*(\d{6})', t):
                if dm.group(1) not in emp['depts']:
                    emp['depts'].append(dm.group(1))
            # statutory amounts sit in the Federal band, left of their label
            for kw in ('FIT', 'SS', 'MED'):
                if re.search(r'\b' + kw + r'\b', t):
                    v = band_amount(b['words'], *bands['stat'])
                    if v is not None:
                        emp['statutory'][kw] = v
            if re.search(r'\bGA\b', t):
                v = band_amount(b['words'], *bands['state'])
                if v is not None and v < 100000:   # guard against check numbers
                    emp['statutory']['GA'] = v
            if 'Total Work Hrs:' in t:
                emp['gross'] = band_amount(b['words'], *bands['gross'])
                twh = [w['text'] for w in b['words'] if 150 < w['x0'] < 210]
                emp['total_work_hrs'] = adp_amount(' '.join(twh)) if twh else None
        # drop anything larger than gross (stray check / voucher numbers)
        g = emp['gross'] or 0
        emp['deductions'] = [d for d in emp['deductions']
                             if d['amount'] is not None and abs(d['amount']) <= g + 0.01]
        # trust the exploded deductions only when they reconcile
        # (direct-deposit employees:  statutory + deductions == gross)
        st = sum(emp['statutory'].values())
        ds = round(sum(d['amount'] for d in emp['deductions']), 2)
        emp['deductions_reconciled'] = abs(round(st + ds, 2) - g) <= 0.02
        employees.append(emp)
    return {'employees': employees, 'bands': bands}


def load_config(path):
    """Read config.ini -> dict with 'database' kwargs and 'defaults'."""
    import configparser
    cfg = configparser.ConfigParser()
    if not cfg.read(path, encoding='utf-8'):
        return {}
    out = {}
    if cfg.has_section('database'):
        out['database'] = dict(cfg.items('database'))
    if cfg.has_section('defaults'):
        out['defaults'] = {k: v for k, v in cfg.items('defaults') if v}
    return out


def connect_db(args, cfg):
    """Open a DB connection from --dsn, else PG_DSN env, else config.ini.

    NOTE: unlike payroll.db.connect_db (the v2 ingestor's connector), this v1
    version never checks the PG_DSN env var -- preserved as-is rather than
    unified, matching the original payroll_etl.py behaviour exactly.
    """
    if args.dsn:
        return psycopg2.connect(args.dsn)
    db = cfg.get('database')
    if db:
        return psycopg2.connect(host=db.get('host'), port=db.get('port', 5432),
                                user=db.get('user'), password=db.get('password'),
                                dbname=db.get('dbname'))
    sys.exit('No DB credentials: provide --dsn, PG_DSN, or a [database] section in config.ini')


def tbl(prop, name):
    return f'"{prop}_{name}"'
