"""Shared PDF word/line reconstruction utilities.

ADP's PDFs have a scrambled text layer, so every parser (legacy and v2) rebuilds
rows from word x/y coordinates instead of using pdfplumber's extract_text().
"""
import re
from collections import defaultdict

import pdfplumber

DEPT6_RE = re.compile(r'^\d{6}$')


def word_lines(page, y_tol=3):
    """Return [(y, [word,...])] with each word = {'x0','x1','text'}, x-sorted."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    rows = defaultdict(list)
    for w in words:
        rows[round(w['top'] / y_tol) * y_tol].append(w)
    return [(y, sorted(rows[y], key=lambda w: w['x0'])) for y in sorted(rows)]


def reconstruct_lines(page, y_tol=3):
    """Rebuild visual text lines from word coordinates (robust for ADP PDFs).
    Returns list of (y, line_text) top-to-bottom, tokens left-to-right."""
    for y, ws in word_lines(page, y_tol):
        yield y, ' '.join(w['text'] for w in ws)


def all_lines(pdf_path):
    res = []
    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, 1):
            for y, line in reconstruct_lines(page):
                res.append((pageno, y, line))
    return res


def extract_word_rows(pdf_path):
    """Return [{'page','y','words','text'}] for every reconstructed row in the PDF."""
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, 1):
            for y, ws in word_lines(page):
                out.append({'page': pageno, 'y': y, 'words': ws,
                            'text': ' '.join(w['text'] for w in ws)})
    return out


def find_file(folder, *must_contain, ext):
    import glob
    import os
    for f in glob.glob(os.path.join(folder, '*' + ext)):
        base = os.path.basename(f).lower()
        if all(m.lower() in base for m in must_contain):
            return f
    return None
