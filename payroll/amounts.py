"""ADP money-token normalisation and coded voluntary/memo column parsing."""


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
