"""
assessments.roster
==================

Reads a class list pasted into a box or uploaded as CSV/TXT. One candidate per
line: "candidate number, full name" (comma, semicolon or tab). A header row, quotes
and a name-first order are all tolerated.
"""

import csv
import io
import re

from .models import normalise_reg_no

MAX_ROWS = 2000
MAX_BYTES = 1024 * 1024
_HEADER_NUM = re.compile(r"^(?:candidate|student|reg|registration|admission|index|id|no\b|number|roll)", re.I)
_HEADER_NAME = re.compile(r"name", re.I)


def decode(data):
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", "replace")


def _delimiter(line):
    for d in ("\t", ";", ","):
        if d in line:
            return d
    return ","


def _looks_like_number(s):
    return bool(re.search(r"\d", s)) and " " not in s.strip()


def parse_roster(text):
    """Returns (rows, problems). rows: [{reg_no, reg_no_display, name}]; problems: [str]."""
    rows, problems, seen = [], [], set()
    def lines():
        for ln in text.splitlines():                       # delimiter is detected per line
            if ln.strip():
                yield next(csv.reader([ln], delimiter=_delimiter(ln), skipinitialspace=True), [])
    first = True
    for n, cells in enumerate(lines(), 1):
        cells = [c.strip() for c in cells]
        if not any(cells):
            continue
        if first:
            first = False
            if len(cells) >= 2 and _HEADER_NUM.match(cells[0]) and _HEADER_NAME.search(" ".join(cells[1:])):
                continue                                     # header row
            if len(cells) >= 2 and _HEADER_NAME.search(cells[0]) and _HEADER_NUM.match(cells[1]):
                continue
        if len(rows) >= MAX_ROWS:
            problems.append(f"Only the first {MAX_ROWS} lines were read.")
            break
        number, name = cells[0], " ".join(c for c in cells[1:] if c)
        if name and not _looks_like_number(number) and _looks_like_number(cells[1]):
            number, name = cells[1], " ".join([cells[0]] + cells[2:]).strip()     # name first
        reg = normalise_reg_no(number)
        if not reg or not re.search(r"[A-Za-z0-9]", reg):
            problems.append(f"Line {n}: no candidate number.")
            continue
        if len(reg) > 60 or len(name) > 150:
            problems.append(f"Line {n}: number or name is too long.")
            continue
        if reg in seen:
            problems.append(f"Line {n}: {number} appears twice; the first one was kept.")
            continue
        seen.add(reg)
        rows.append({"reg_no": reg, "reg_no_display": number.strip()[:60], "name": name[:150]})
    return rows, problems
