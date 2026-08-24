#!/usr/bin/env python3
"""
payroll_etl.py  --  Payroll (P/L) extraction & load pipeline
============================================================

Extracts an ADP payroll batch (register / summary / stats + NetSuite JE) and
loads it into the property data-lake:

    RAW      : "<prop>_payroll_raw_landing"      (verbatim, per record_type)
    CURATED  : "<prop>_payroll_register_line"    (employee x dept)
               "<prop>_payroll_summary"          (department)
               "<prop>_stats_summary"            (pay-run recap)
               "<prop>_payroll_journal"          (GL line)
    RECONCILE: "<prop>_v_payroll_reconciliation" (view)

A batch folder is expected to contain (names matched case-insensitively):
    *register*.pdf          ADP Payroll Register
    *summary*.pdf (Payroll) ADP Payroll Summary
    *stats*.pdf             ADP Statistical Summary
    *.xlsx                  NetSuite payroll journal entry

Usage
-----
    # dry run - parse & validate only, no DB writes
    python payroll_etl.py --folder "<batch folder>" --property-id 383 \
        --company-code QVJ --dry-run

    # load (DB DSN from env PG_DSN or --dsn)
    export PG_DSN="postgresql://postgres:***@host:5432/integration_dev"
    python payroll_etl.py --folder "<batch folder>" --property-id 383 \
        --company-code QVJ

Design notes
------------
* ADP prints money as space-separated groups whose last group is cents
  ("1 046 68" -> 1046.68); totals sometimes use "1,046.68".  adp_amount()
  normalises both, plus trailing-minus ("75 00-" -> -75.00).
* The register text layer is scrambled; we rebuild rows from word x/y
  coordinates (reconstruct_lines) instead of extract_text().
* Every landing row keeps the raw reconstructed lines, so nothing is lost
  even if a structured field fails to parse.
* Load is validated: employee gross must equal the company total, the JE must
  balance (<=0.01), and register==summary==journal gross must agree, or the
  batch is rejected (unless --force).
"""
from __future__ import annotations
import argparse, configparser, glob, os, re, sys, json
from collections import defaultdict
from datetime import date

import pdfplumber
import openpyxl

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except ImportError:
    psycopg2 = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_AMOUNT_RE = re.compile(r'^-?[\d,]+(?:\.\d+)?-?$')

def adp_amount(text: str):
    """Normalise an ADP money token (or space-separated group) to float.
    '494 42'->494.42  '1 046 68'->1046.68  '1,759.32'->1759.32  '75 00-'->-75.0
    '.00'->0.0        returns None if not a number."""
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    neg = t.endswith('-')
    t = t.rstrip('-').strip()
    # normal formatted number with a decimal point
    if '.' in t:
        t2 = t.replace(',', '')
        try:
            v = float(t2)
            return -v if neg else v
        except ValueError:
            return None
    # space separated ADP groups: last group == cents
    parts = t.split()
    if parts and all(p.replace(',', '').isdigit() for p in parts):
        cents = parts[-1]
        if len(cents) == 2:
            whole = ''.join(p.replace(',', '') for p in parts[:-1]) or '0'
            try:
                v = int(whole) + int(cents) / 100.0
                return -v if neg else v
            except ValueError:
                return None
        # single integer group
        if len(parts) == 1 and parts[0].replace(',', '').isdigit():
            v = float(parts[0].replace(',', ''))
            return -v if neg else v
    return None


def reconstruct_lines(page, y_tol=3):
    """Rebuild visual text lines from word coordinates (robust for ADP PDFs).
    Returns list of (y, line_text) top-to-bottom, tokens left-to-right."""
    for y, ws in _word_lines(page, y_tol):
        yield y, ' '.join(w['text'] for w in ws)


def _word_lines(page, y_tol=3):
    """Return [(y, [word,...])] with each word = {'x0','x1','text'}, x-sorted."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    rows = defaultdict(list)
    for w in words:
        rows[round(w['top'] / y_tol) * y_tol].append(w)
    return [(y, sorted(rows[y], key=lambda w: w['x0'])) for y in sorted(rows)]


def band_amount(words, x_lo, x_hi):
    """Combine the numeric word tokens whose x0 falls in [x_lo, x_hi) into one
    ADP amount (their spaces are ADP's thousands/cents separators)."""
    toks = [w['text'] for w in words if x_lo <= w['x0'] < x_hi and
            w['text'].replace(',', '').isdigit()]
    return adp_amount(' '.join(toks)) if toks else None


def parse_coded(tokens):
    """Walk a token stream of the ADP voluntary/memo column and split it into
    deductions [{code,label,amount}] and memo [{code,amount}] entries.
    Each entry is <digit-tokens...> <CODE> [label]; a leading 'N-'/'M-'/'$R'
    (or 'N-XXX') marks a memo rather than a real deduction."""
    deds, memos = [], []
    i, n = 0, len(tokens)
    isnum = lambda t: t.replace(',', '').replace('-', '').isdigit()
    while i < n:
        if isnum(tokens[i]):
            amt = [tokens[i]]; i += 1
            while i < n and isnum(tokens[i]):
                amt.append(tokens[i]); i += 1
            val = adp_amount(' '.join(amt))
            if i >= n:
                break
            marker = tokens[i]; i += 1
            if marker.startswith(('N-', 'M-', '$R')):     # memo
                rest = marker.split('-', 1)[1] if '-' in marker else ''
                labels = [rest] if rest else []
                while i < n and not isnum(tokens[i]):
                    labels.append(tokens[i]); i += 1
                memos.append({'code': ' '.join([l for l in labels if l]).strip(), 'amount': val})
            else:                                          # deduction
                label = tokens[i] if i < n and not isnum(tokens[i]) else ''
                if label:
                    i += 1
                deds.append({'code': marker, 'label': label, 'amount': val})
        else:
            i += 1
    return deds, memos


def all_lines(pdf_path):
    res = []
    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, 1):
            for y, line in reconstruct_lines(page):
                res.append((pageno, y, line))
    return res


def find_file(folder, *must_contain, ext):
    for f in glob.glob(os.path.join(folder, '*' + ext)):
        base = os.path.basename(f).lower()
        if all(m.lower() in base for m in must_contain):
            return f
    return None


# amount + trailing-code helpers -------------------------------------------------
def trailing_amount(line, keyword):
    """Return the ADP amount that appears immediately before KEYWORD on a line."""
    m = re.search(r'((?:\d[\d,]*\s)*\d{2}|\.\d{2}|[\d,]+\.\d{2})\s*' + re.escape(keyword), line)
    return adp_amount(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
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


DEPT_HDR_RE = re.compile(r'^(\d{6})\s')

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


def _first_decimal(line):
    m = re.search(r'([\d,]+\.\d{2})', line)
    return adp_amount(m.group(1)) if m else None


def trailing_after(line, label):
    after = line.split(label, 1)[1] if label in line else ''
    return adp_amount(after.strip()) if after.strip() else None


NAME_FRAG_RE = re.compile(r"^[A-Z][A-Z' .\-]*(?:,[A-Z' .\-]+)?$")

def _register_word_lines(pdf_path):
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, 1):
            for y, ws in _word_lines(page):
                out.append({'page': pageno, 'y': y, 'words': ws,
                            'text': ' '.join(w['text'] for w in ws)})
    return out


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
    lines = _register_word_lines(pdf_path)
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


# ---------------------------------------------------------------------------
# Schema (parameterised by table prefix, usually the property id)
# ---------------------------------------------------------------------------
SCHEMA_DDL = r'''
CREATE TABLE IF NOT EXISTS public."{p}_payroll_raw_landing" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer, company_code text, batch_number text, pay_date date,
    source_type text, source_file text, row_seq integer, payload jsonb,
    ingested_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_landing" UNIQUE (source_file, source_type, row_seq));

CREATE TABLE IF NOT EXISTS public."{p}_payroll_register_line" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    report_period_start_date date, report_period_end_date date, check_date date, pay_date date,
    file_number text DEFAULT '', employee_name text DEFAULT '', department_code text DEFAULT '',
    gl_account text DEFAULT '',
    reg_hours numeric(10,2) DEFAULT 0, reg_amount numeric(12,2) DEFAULT 0,
    ot_hours numeric(10,2) DEFAULT 0, ot_amount numeric(12,2) DEFAULT 0,
    hol_hours numeric(10,2) DEFAULT 0, hol_amount numeric(12,2) DEFAULT 0,
    gross_amount numeric(12,2) DEFAULT 0,
    fit_amount numeric(12,2) DEFAULT 0, ss_ee_amount numeric(12,2) DEFAULT 0,
    medicare_ee_amount numeric(12,2) DEFAULT 0, state_amount numeric(12,2) DEFAULT 0,
    earnings_detail jsonb DEFAULT '[]'::jsonb, tax_detail jsonb DEFAULT '[]'::jsonb,
    deduction_detail jsonb DEFAULT '[]'::jsonb,
    deductions_reconciled boolean DEFAULT false,
    voucher_number text DEFAULT '', net_pay_amount numeric(12,2),
    currency char(3) DEFAULT 'USD', month smallint DEFAULT 0, year smallint DEFAULT 0,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_reg" UNIQUE (property_id, batch_number, pay_date, file_number, department_code));

CREATE TABLE IF NOT EXISTS public."{p}_payroll_summary" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    report_period_start_date date, report_period_end_date date, check_date date, pay_date date,
    job_code text DEFAULT '', job_code_description text DEFAULT '',
    department_code text DEFAULT '', gl_account text DEFAULT '',
    total_employees integer DEFAULT 0, female_employees integer DEFAULT 0, male_employees integer DEFAULT 0,
    earnings_summary jsonb DEFAULT '[]'::jsonb, tax_summary jsonb DEFAULT '[]'::jsonb,
    deduction_summary jsonb DEFAULT '[]'::jsonb, employer_cost_summary jsonb DEFAULT '{}'::jsonb,
    payment_summary jsonb DEFAULT '{}'::jsonb, cafeteria_125 jsonb DEFAULT '{}'::jsonb,
    total_earnings_hours numeric(12,2) DEFAULT 0, total_earnings_amount numeric(14,2) DEFAULT 0,
    total_tax_deductions_amount numeric(14,2) DEFAULT 0, total_other_deductions_amount numeric(14,2) DEFAULT 0,
    total_employer_cost_amount numeric(14,2) DEFAULT 0, total_net_pay_amount numeric(14,2) DEFAULT 0,
    currency char(3) DEFAULT 'USD', month smallint DEFAULT 0, year smallint DEFAULT 0,
    isactive boolean DEFAULT true, source_file text DEFAULT '',
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_sum" UNIQUE (property_id, batch_number, pay_date, job_code, gl_account));

CREATE TABLE IF NOT EXISTS public."{p}_stats_summary" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    report_period_end_date date, pay_date date, quarter_number smallint DEFAULT 0,
    net_pay_checks numeric(14,2) DEFAULT 0, net_pay_direct_deposit numeric(14,2) DEFAULT 0,
    net_cash numeric(14,2) DEFAULT 0,
    fed_income_tax numeric(14,2) DEFAULT 0, ss_ee_amount numeric(14,2) DEFAULT 0,
    ss_er_amount numeric(14,2) DEFAULT 0, medicare_ee_amount numeric(14,2) DEFAULT 0,
    medicare_er_amount numeric(14,2) DEFAULT 0, futa_amount numeric(14,2) DEFAULT 0,
    state_income_tax numeric(14,2) DEFAULT 0, sui_er_amount numeric(14,2) DEFAULT 0,
    total_taxes_debited numeric(14,2) DEFAULT 0, retirement_401k numeric(14,2) DEFAULT 0,
    wage_garnishments numeric(14,2) DEFAULT 0, total_amount_debited numeric(14,2) DEFAULT 0,
    detail jsonb DEFAULT '{}'::jsonb, currency char(3) DEFAULT 'USD',
    month smallint DEFAULT 0, year smallint DEFAULT 0, isactive boolean DEFAULT true,
    source_file text DEFAULT '', created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_stats" UNIQUE (property_id, batch_number, pay_date));

CREATE TABLE IF NOT EXISTS public."{p}_payroll_journal" (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_id integer NOT NULL, company_code text DEFAULT '', batch_number text DEFAULT '',
    external_id text DEFAULT '', transaction_date date, posting_period text DEFAULT '',
    memo text DEFAULT '', subsidiary text DEFAULT '',
    gl_account_number text DEFAULT '', gl_account_name text DEFAULT '',
    debit numeric(14,2) DEFAULT 0, credit numeric(14,2) DEFAULT 0,
    memo_line text DEFAULT '', outlet text DEFAULT '', currency char(3) DEFAULT 'USD',
    month smallint DEFAULT 0, year smallint DEFAULT 0, isactive boolean DEFAULT true,
    source_file text DEFAULT '', created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    CONSTRAINT "uq_{p}_je" UNIQUE (property_id, batch_number, gl_account_number, memo_line));

CREATE INDEX IF NOT EXISTS "ix_{p}_reg_period" ON public."{p}_payroll_register_line" (property_id, year, month);
CREATE INDEX IF NOT EXISTS "ix_{p}_sum_gl"     ON public."{p}_payroll_summary" (property_id, gl_account);
CREATE INDEX IF NOT EXISTS "ix_{p}_je_acct"    ON public."{p}_payroll_journal" (property_id, gl_account_number);

CREATE OR REPLACE VIEW public."{p}_v_payroll_reconciliation" AS
WITH reg AS (SELECT property_id,batch_number,pay_date,
        SUM(gross_amount) reg_gross,
        SUM(fit_amount+ss_ee_amount+medicare_ee_amount+state_amount) reg_ee_tax
     FROM public."{p}_payroll_register_line" GROUP BY 1,2,3),
sm AS (SELECT property_id,batch_number,pay_date,
        SUM(total_earnings_amount) sum_gross, SUM(total_net_pay_amount) sum_net_cash
     FROM public."{p}_payroll_summary" GROUP BY 1,2,3),
je AS (SELECT property_id,batch_number,SUM(debit) je_debit,SUM(credit) je_credit,
        SUM(CASE WHEN gl_account_number ~ '(R|OT|990)$' THEN debit ELSE 0 END) je_wages
     FROM public."{p}_payroll_journal" GROUP BY 1,2),
st AS (SELECT property_id,batch_number,pay_date,net_cash stats_net_cash,total_amount_debited
     FROM public."{p}_stats_summary")
SELECT sm.property_id,sm.batch_number,sm.pay_date,
    reg.reg_gross,sm.sum_gross,je.je_wages,
    sm.sum_net_cash,st.stats_net_cash,je.je_debit,je.je_credit,st.total_amount_debited,
    (reg.reg_gross=sm.sum_gross AND sm.sum_gross=je.je_wages) gross_ties,
    (sm.sum_net_cash=st.stats_net_cash) net_ties,
    (abs(je.je_debit-je.je_credit)<=0.01) je_balances
FROM sm JOIN reg USING(property_id,batch_number,pay_date)
        JOIN st USING(property_id,batch_number,pay_date)
        JOIN je ON je.property_id=sm.property_id AND je.batch_number=sm.batch_number;
'''


def ensure_schema(conn, prop):
    cur = conn.cursor()
    cur.execute(SCHEMA_DDL.replace('{p}', prop))
    conn.commit()


def load_config(path):
    """Read config.ini -> dict with 'database' kwargs and 'defaults'."""
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
    """Open a DB connection from --dsn, else PG_DSN env, else config.ini."""
    if args.dsn:
        return psycopg2.connect(args.dsn)
    db = cfg.get('database')
    if db:
        return psycopg2.connect(host=db.get('host'), port=db.get('port', 5432),
                                user=db.get('user'), password=db.get('password'),
                                dbname=db.get('dbname'))
    sys.exit('No DB credentials: provide --dsn, PG_DSN, or a [database] section in config.ini')


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def tbl(prop, name):
    return f'"{prop}_{name}"'


def load(conn, prop, meta, journal, stats, summary, register, replace=True):
    cur = conn.cursor()
    P = meta['property_id']
    cc = meta['company_code']
    bn = meta['batch_number']
    pd_ = meta['pay_date']

    if replace:
        for t, st in [('payroll_raw_landing', None)]:
            cur.execute(f'DELETE FROM public.{tbl(prop,"payroll_raw_landing")} '
                        f'WHERE batch_number=%s', (bn,))
        for t in ('payroll_journal', 'payroll_summary', 'stats_summary', 'payroll_register_line'):
            cur.execute(f'DELETE FROM public.{tbl(prop,t)} WHERE batch_number=%s', (bn,))

    def land(stype, sfile, seq, payload):
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_raw_landing")} '
            f'(property_id,company_code,batch_number,pay_date,source_type,source_file,row_seq,payload) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, pd_, stype, sfile, seq, Json(payload)))

    # --- journal (landing + curated) ---
    for i, r in enumerate(journal, 1):
        land('journal', meta['files']['journal'], i, {**r, 'record_type': 'je_line',
             'transaction_date': str(r['transaction_date'])})
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_journal")} '
            f'(property_id,company_code,batch_number,external_id,transaction_date,memo,subsidiary,'
            f'gl_account_number,gl_account_name,debit,credit,memo_line,outlet,month,year,source_file) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, r['external_id'], r['transaction_date'], r['memo'], r['subsidiary'],
             r['gl_account_number'], r['gl_account_name'], r['debit'], r['credit'],
             r['memo_line'], r['outlet'], meta['month'], meta['year'], meta['files']['journal']))

    # --- stats (landing) ---
    land('stats', meta['files']['stats'], 1, {'record_type': 'stats_recap', **{k: v for k, v in stats.items() if k != 'raw_lines'}})
    cur.execute(
        f'INSERT INTO public.{tbl(prop,"stats_summary")} '
        f'(property_id,company_code,batch_number,pay_date,net_cash,fed_income_tax,ss_ee_amount,ss_er_amount,'
        f'medicare_ee_amount,medicare_er_amount,state_income_tax,retirement_401k,wage_garnishments,'
        f'total_amount_debited,detail,month,year,source_file) '
        f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (P, cc, bn, pd_, stats.get('net_cash'), stats.get('federal_income_tax'), stats.get('ss_ee'),
         stats.get('ss_er'), stats.get('medicare_ee'), stats.get('medicare_er'), stats.get('state_income_tax'),
         stats.get('retirement_401k'), stats.get('wage_garnishments'), stats.get('total_amount_debited'),
         Json(stats), meta['month'], meta['year'], meta['files']['stats']))

    # --- summary (landing + curated) ---
    for i, d in enumerate(summary['departments'], 1):
        land('summary', meta['files']['summary'], i, {'record_type': 'dept_summary', **d})
        cur.execute(
            f'INSERT INTO public.{tbl(prop,"payroll_summary")} '
            f'(property_id,company_code,batch_number,pay_date,job_code,department_code,'
            f'earnings_summary,tax_summary,payment_summary,total_earnings_amount,total_net_pay_amount,'
            f'month,year,source_file) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (P, cc, bn, pd_, d['dept'], d['dept'], Json([]), Json(d.get('taxes', {})),
             Json({'gross': d.get('gross'), 'net_cash': d.get('net_cash')}),
             d.get('gross'), d.get('net_cash'), meta['month'], meta['year'], meta['files']['summary']))

    # --- register (landing + curated) ---
    for i, e in enumerate(register['employees'], 1):
        land('register', meta['files']['register'], i, {'record_type': 'employee', **e})
        earn = e.get('earnings', [])
        reg_amt = round(sum(x.get('reg_earn', 0) for x in earn), 2)
        ot_amt = round(sum(x.get('ot_earn', 0) for x in earn), 2)
        reg_hrs = round(sum(x.get('reg_hours', 0) for x in earn), 2)
        ot_hrs = round(sum(x.get('ot_hours', 0) for x in earn), 2)
        st = e.get('statutory', {})
        tax_detail = [{'code': k, 'amount': v} for k, v in st.items()]
        ded_detail = e.get('deductions', []) if e.get('deductions_reconciled') else []
        depts = e['depts'] or ['']
        for dep in depts:
            primary = dep == depts[0]
            cur.execute(
                f'INSERT INTO public.{tbl(prop,"payroll_register_line")} '
                f'(property_id,company_code,batch_number,pay_date,file_number,employee_name,department_code,'
                f'reg_hours,reg_amount,ot_hours,ot_amount,gross_amount,'
                f'fit_amount,ss_ee_amount,medicare_ee_amount,state_amount,'
                f'earnings_detail,tax_detail,deduction_detail,deductions_reconciled,'
                f'month,year,source_file) '
                f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (P, cc, bn, pd_, e.get('file', ''), e['name'], dep,
                 reg_hrs if primary else 0, reg_amt if primary else 0,
                 ot_hrs if primary else 0, ot_amt if primary else 0,
                 e.get('gross') if primary else 0,
                 st.get('FIT') if primary else 0, st.get('SS') if primary else 0,
                 st.get('MED') if primary else 0, st.get('GA') if primary else 0,
                 Json(earn if primary else []), Json(tax_detail if primary else []),
                 Json(ded_detail if primary else []), bool(e.get('deductions_reconciled')),
                 meta['month'], meta['year'], meta['files']['register']))
    conn.commit()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(journal, summary, register):
    issues = []
    jd = round(sum(r['debit'] for r in journal), 2)
    jc = round(sum(r['credit'] for r in journal), 2)
    if round(abs(jd - jc), 2) > 0.01:
        issues.append(f'JE not balanced: debit {jd} vs credit {jc}')
    sg = round(sum(d.get('gross') or 0 for d in summary['departments']), 2)
    rg = round(sum(e.get('gross') or 0 for e in register['employees']), 2)
    je_wages = round(sum(r['debit'] for r in journal
                         if re.search(r'(R|OT|990)$', r['gl_account_number'])), 2)
    if abs(sg - je_wages) > 0.01:
        issues.append(f'summary gross {sg} != JE wages {je_wages}')
    if abs(rg - je_wages) > 0.01:
        issues.append(f'register gross {rg} != JE wages {je_wages}')
    return {'je_debit': jd, 'je_credit': jc, 'summary_gross': sg,
            'register_gross': rg, 'je_wages': je_wages, 'issues': issues}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description='Payroll batch ETL')
    ap.add_argument('--folder', required=True, help='batch folder with the ADP files')
    ap.add_argument('--property-id', type=int, required=True)
    ap.add_argument('--company-code', default='')
    ap.add_argument('--batch-number', default='')
    ap.add_argument('--pay-date', default='', help='YYYY-MM-DD')
    ap.add_argument('--table-prefix', default=None, help='table name prefix (default = property id)')
    ap.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini'),
                    help='path to config.ini (DB credentials + defaults)')
    ap.add_argument('--dsn', default=os.environ.get('PG_DSN'),
                    help='override DB connection string (else taken from config.ini)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='load even if validation fails')
    ap.add_argument('--init-schema', action='store_true',
                    help='CREATE TABLE IF NOT EXISTS (+ indexes + recon view) before loading')
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    dflt = cfg.get('defaults', {})
    if not a.company_code and dflt.get('company_code'):
        a.company_code = dflt['company_code']
    if not a.table_prefix and dflt.get('table_prefix'):
        a.table_prefix = dflt['table_prefix']

    folder = a.folder
    files = {
        'register': find_file(folder, 'register', ext='.pdf'),
        'summary':  find_file(folder, 'summary', ext='.pdf') or find_file(folder, 'payroll summary', ext='.pdf'),
        'stats':    find_file(folder, 'stats', ext='.pdf'),
        'journal':  find_file(folder, ext='.xlsx'),
    }
    for k, v in files.items():
        print(f'  {k:9}: {os.path.basename(v) if v else "** NOT FOUND **"}')
    missing = [k for k, v in files.items() if not v]
    if missing:
        sys.exit(f'Missing required files: {missing}')

    print('\nParsing ...')
    journal = parse_journal(files['journal'])
    stats = parse_stats(files['stats'])
    summary = parse_summary(files['summary'])
    register = parse_register(files['register'])
    print(f'  journal lines : {len(journal)}')
    print(f'  summary depts : {len(summary["departments"])}')
    print(f'  register emps : {len(register["employees"])}')

    v = validate(journal, summary, register)
    print('\nValidation:')
    for k in ('je_debit', 'je_credit', 'summary_gross', 'register_gross', 'je_wages'):
        print(f'  {k:15}: {v[k]}')
    if v['issues']:
        print('  ISSUES:')
        for i in v['issues']:
            print('   -', i)
    else:
        print('  all reconciliation checks passed')

    meta = {
        'property_id': a.property_id, 'company_code': a.company_code,
        'batch_number': a.batch_number, 'pay_date': a.pay_date or None,
        'files': {k: os.path.basename(vv) for k, vv in files.items()},
    }
    if a.pay_date:
        y, m, _ = a.pay_date.split('-')
        meta['year'], meta['month'] = int(y), int(m)
    else:
        meta['year'], meta['month'] = 0, 0

    if a.dry_run:
        print('\n[dry-run] no DB writes.')
        return
    if v['issues'] and not a.force:
        sys.exit('\nValidation failed; refusing to load (use --force to override).')
    if psycopg2 is None:
        sys.exit('psycopg2 not installed.')
    prop = a.table_prefix or str(a.property_id)
    conn = connect_db(a, cfg)
    if a.init_schema:
        ensure_schema(conn, prop)
        print(f'\nSchema ensured for prefix "{prop}".')
    load(conn, prop, meta, journal, stats, summary, register)
    print('Loaded to DB.')


if __name__ == '__main__':
    main()
