"""
assessments.importer
====================

Turns a trainer's assessment document (.docx or .html) into draft questions in
the system's own question format (MCQ, fill-in-the-blank, matching, open).

Nothing here executes or renders the uploaded file. Word and HTML are reduced to
plain text lines (with bold/underline hints) and read by a rule-based parser, so
an upload can never put markup or script on a candidate's page. The result is
shown to the trainer for review before anything is saved.

Layouts it understands
----------------------
* Section headings: "Section A: Multiple choice (20 marks)", "Part B - Fill in
  the blanks", "True or False", "Matching", "Open / structured questions".
* Questions numbered "1.", "1)", "Q1." (typed, or Word/HTML automatic numbering).
* Options "A." / "a)" / "(a)" on separate lines, in one line, or bullets.
* Answers, any of: a line "Answer: B" (or "Answer: A, C" when several options are correct) under the question, a bold/underlined
  correct option, or an "Answer Key" / "Marking guide" block at the end
  ("1. B", "1-B 2-C", or a two-column table).
* Blanks written as ____, ...., … or ( ).
* Matching as a two-column table, or numbered items plus lettered choices with
  a key such as "1-C, 2-A".
* Marks written as "(2 marks)" or "[2]" at the end of a question.
* Lines starting with "//" are notes to the trainer and are ignored.
"""

import io
import re
import zipfile
from dataclasses import dataclass, field

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UNZIPPED_BYTES = 40 * 1024 * 1024
MAX_QUESTIONS = 300
MAX_TEXT = 4000
MAX_OPTION = 500


class ImportProblem(Exception):
    """A problem with the uploaded file that the trainer can act on."""


@dataclass
class Line:
    text: str
    emph: bool = False            # most of the text is bold / underlined / highlighted
    cells: list = None            # table row cells, when the line is a table row
    bullet: bool = False
    heading: bool = False


# --------------------------------------------------------------------------- #
# Word (.docx) → lines
# --------------------------------------------------------------------------- #

def _fmt_number(n, fmt):
    if fmt in ("lowerLetter", "upperLetter"):
        s = ""
        k = n
        while k > 0:
            k, r = divmod(k - 1, 26)
            s = chr(97 + r) + s
        return s.upper() if fmt == "upperLetter" else s
    if fmt in ("lowerRoman", "upperRoman"):
        vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
        s, k = "", n
        for v, sym in vals:
            while k >= v:
                s += sym
                k -= v
        return s.upper() if fmt == "upperRoman" else s
    return str(n)


class _Numbering:
    """Reproduces Word's automatic list numbers, which are not part of the paragraph text."""

    def __init__(self, doc):
        self.levels = {}      # abstract id -> {ilvl: (fmt, text, start)}
        self.num_to_abs = {}
        self.counters = {}
        try:
            from docx.oxml.ns import qn
            root = doc.part.numbering_part.element
            for a in root.findall(qn("w:abstractNum")):
                aid = a.get(qn("w:abstractNumId"))
                lv = {}
                for l in a.findall(qn("w:lvl")):
                    ilvl = int(l.get(qn("w:ilvl")))
                    fmt = l.find(qn("w:numFmt"))
                    txt = l.find(qn("w:lvlText"))
                    st = l.find(qn("w:start"))
                    lv[ilvl] = (fmt.get(qn("w:val")) if fmt is not None else "decimal",
                                txt.get(qn("w:val")) if txt is not None else "%1.",
                                int(st.get(qn("w:val"))) if st is not None else 1)
                self.levels[aid] = lv
            for n in root.findall(qn("w:num")):
                ab = n.find(qn("w:abstractNumId"))
                if ab is not None:
                    self.num_to_abs[n.get(qn("w:numId"))] = ab.get(qn("w:val"))
        except Exception:
            pass

    def label(self, num_id, ilvl):
        """('1.', False) for numbered, ('', True) for bullets, None if unknown."""
        lv = self.levels.get(self.num_to_abs.get(str(num_id)))
        if not lv or ilvl not in lv:
            return None
        fmt, text, start = lv[ilvl]
        cur = self.counters.setdefault(str(num_id), [0] * 9)
        cur[ilvl] = start if cur[ilvl] == 0 else cur[ilvl] + 1
        for j in range(ilvl + 1, 9):
            cur[j] = 0
        if fmt == "bullet":
            return "", True
        def sub(m):
            k = int(m.group(1)) - 1
            f = lv.get(k, ("decimal",))[0]
            return _fmt_number(cur[k] or lv.get(k, (0, 0, 1))[2], f)
        return re.sub(r"%(\d)", sub, text), False


def _para_numpr(p):
    try:
        pPr = p._p.pPr
        if pPr is not None and pPr.numPr is not None and pPr.numPr.numId is not None:
            n = pPr.numPr
            return (n.numId.val, n.ilvl.val if n.ilvl is not None else 0) if n.numId.val else None
        st = p.style
        while st is not None:
            spPr = st.element.pPr
            if spPr is not None and spPr.numPr is not None and spPr.numPr.numId is not None:
                n = spPr.numPr
                return (n.numId.val, n.ilvl.val if n.ilvl is not None else 0) if n.numId.val else None
            st = st.base_style
    except Exception:
        pass
    return None


def _run_emphasised(run):
    try:
        f = run.font
        if run.bold or run.underline or f.highlight_color:
            return True
        c = f.color
        if c is not None and c.rgb is not None and str(c.rgb).upper() not in ("000000", "FFFFFF", "404040", "262626"):
            return True
    except Exception:
        pass
    return False


def read_docx(data):
    lines, stats = [], {"images": 0}
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        if data[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
            raise ImportProblem("This looks like an old Word .doc file. Open it in Word and use Save As → Word Document (.docx), then upload that.")
        raise ImportProblem("That file is not a valid Word .docx document.")
    names = set(zf.namelist())
    if "word/document.xml" not in names:
        raise ImportProblem("That file is not a valid Word .docx document.")
    if sum(i.file_size for i in zf.infolist()) > MAX_UNZIPPED_BYTES:
        raise ImportProblem("That document is too large to import.")
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    try:
        doc = Document(io.BytesIO(data))
    except Exception:
        raise ImportProblem("That Word document could not be read. Re-save it as .docx and try again.")
    numbering = _Numbering(doc)
    stats["images"] = len(doc.inline_shapes)

    def paragraph_lines(p, use_numbering=True):
        out, cur = [], {"buf": [], "chars": 0, "emph": 0}
        def flush():
            text = re.sub(r"[ \t\u00a0]+", " ", "".join(cur["buf"])).strip()
            if text:
                out.append([text, cur["chars"] and cur["emph"] / cur["chars"] >= 0.5])
            cur["buf"], cur["chars"], cur["emph"] = [], 0, 0
        runs = list(p.runs)
        if "".join(r.text for r in runs).strip() != p.text.strip():
            runs = None
        if runs is None:
            for i, piece in enumerate(p.text.split("\n")):
                cur["buf"].append(piece); flush()
        else:
            for r in runs:
                em = _run_emphasised(r)
                pieces = r.text.replace("\t", " ").split("\n")
                for i, piece in enumerate(pieces):
                    cur["buf"].append(piece)
                    n = len(piece.strip())
                    cur["chars"] += n
                    cur["emph"] += n if em else 0
                    if i < len(pieces) - 1:
                        flush()
            flush()
        heading = False
        try:
            name = (p.style.name or "").lower()
            heading = name.startswith("heading") or name == "title"
        except Exception:
            pass
        result, bullet = [], False
        label = None
        if use_numbering:
            np_ = _para_numpr(p)
            if np_:
                label = numbering.label(*np_)
        for k, (text, emph) in enumerate(out):
            ln = Line(text=text, emph=bool(emph), heading=heading)
            if k == 0 and label is not None:
                lab, is_bullet = label
                if is_bullet:
                    ln.bullet = True
                else:
                    ln.text = f"{lab} {text}"
            result.append(ln)
        return result

    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            lines.extend(paragraph_lines(block))
        elif isinstance(block, Table):
            for row in block.rows:
                cells, seen = [], set()
                for c in row.cells:
                    if id(c._tc) in seen:
                        continue
                    seen.add(id(c._tc))
                    cells.append(re.sub(r"\s+", " ", c.text).strip())
                filled = [c for c in cells if c]
                if len(filled) >= 2:
                    lines.append(Line(text="\t".join(filled), cells=filled))
                elif len(filled) == 1:
                    lines.append(Line(text=filled[0]))
    return lines, stats


# --------------------------------------------------------------------------- #
# HTML → lines
# --------------------------------------------------------------------------- #

_BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote",
          "pre", "dt", "dd", "caption", "ul", "ol", "table", "header", "footer", "form", "fieldset",
          "legend", "main", "nav", "aside", "figure", "figcaption", "tbody", "thead", "tfoot", "dl", "hr"}
_EMPH_TAGS = {"b", "strong", "u", "mark", "ins"}


def _style_emphasised(style):
    s = (style or "").lower().replace(" ", "")
    return bool(re.search(r"font-weight:(bold|[6-9]00)", s) or "text-decoration:underline" in s
                or re.search(r"background(-color)?:(?!transparent|none|#fff\b|#ffffff|white)", s))


def read_html(data):
    from bs4 import BeautifulSoup, NavigableString, Tag
    soup = BeautifulSoup(data, "lxml")
    for t in soup(["script", "style", "noscript", "template", "svg", "head"]):
        t.decompose()
    stats = {"images": len(soup.find_all("img"))}
    root = soup.body or soup
    lines = []
    buf = {"parts": [], "chars": 0, "emph": 0}
    state = {"label": "", "bullet": False, "heading": False}

    def flush():
        text = re.sub(r"\s+", " ", "".join(buf["parts"]).replace("\u00a0", " ")).strip()
        if text:
            ln = Line(text=text, emph=bool(buf["chars"] and buf["emph"] / buf["chars"] >= 0.5),
                      heading=state["heading"])
            if state["bullet"]:
                ln.bullet = True
            elif state["label"]:
                ln.text = f"{state['label']} {text}"
            lines.append(ln)
            state["label"], state["bullet"] = "", False
        buf["parts"], buf["chars"], buf["emph"] = [], 0, 0

    def add(text, em):
        buf["parts"].append(text)
        n = len(text.strip())
        buf["chars"] += n
        buf["emph"] += n if em else 0

    def walk(node, em, lst):
        if isinstance(node, NavigableString):
            if type(node) is NavigableString:        # skips comments, doctype, CDATA
                add(str(node), em)
            return
        if not isinstance(node, Tag):
            return
        name = node.name.lower()
        if name == "br":
            flush(); return
        if name == "input":
            if (node.get("type") or "text").lower() in ("text", "number", "tel", "search", "email", ""):
                add(" ____ ", em)
            return
        if name == "textarea":
            add(" ____ ", em); return
        if name == "select":
            return
        if name == "tr":
            flush()
            cells = []
            for c in node.find_all(["td", "th"], recursive=False):
                cells.append(re.sub(r"\s+", " ", c.get_text(" ", strip=True).replace("\u00a0", " ")).strip())
            filled = [c for c in cells if c]
            if len(filled) >= 2:
                lines.append(Line(text="\t".join(filled), cells=filled))
            elif len(filled) == 1:
                lines.append(Line(text=filled[0]))
            return
        if name in ("ol", "ul"):
            flush()
            start = node.get("start") or ""
            ctx = {"kind": name, "n": (int(start) - 1) if start.isdigit() else 0, "type": node.get("type") or "1"}
            for ch in node.children:
                walk(ch, em, ctx)
            flush(); return
        if name == "li":
            flush()
            if lst and lst["kind"] == "ol":
                lst["n"] += 1
                state["label"] = _fmt_li(lst["n"], lst["type"]) + "."
                state["bullet"] = False
            else:
                state["bullet"], state["label"] = True, ""
            for ch in node.children:
                walk(ch, em, lst)
            flush(); return
        block = name in _BLOCK
        if block:
            flush()
            was = state["heading"]
            state["heading"] = was or name in ("h1", "h2", "h3", "h4", "h5", "h6")
        em2 = em or name in _EMPH_TAGS or _style_emphasised(node.get("style"))
        for ch in node.children:
            walk(ch, em2, lst)
        if block:
            flush()
            state["heading"] = was

    walk(root, False, None)
    flush()
    return lines, stats


def _fmt_li(n, kind):
    return {"a": _fmt_number(n, "lowerLetter"), "A": _fmt_number(n, "upperLetter"),
            "i": _fmt_number(n, "lowerRoman"), "I": _fmt_number(n, "upperRoman")}.get(kind, str(n))


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

Q_RE = re.compile(r"^\s*(?:q(?:uestion)?\.?\s*)?(\d{1,3})\s*[.):\-–—]\s*(\S.*)$", re.I)
OPT_RE = re.compile(r"^\s*\(?([A-Ha-h])\s*[.)]\s*(\S.*)$")
ANS_RE = re.compile(r"^\s*(?:correct\s+)?(?:answers?|ans|key|solution|model\s+answer|marking\s+(?:guide|scheme)|"
                    r"guide|expected\s+answer)\s*[:\-–—=]\s*(.*)$", re.I)
KEY_RE = re.compile(r"^\s*(?:answer\s*(?:key|sheet|guide)s?|answers|marking\s*(?:guide|scheme)|memorandum|memo|"
                    r"solutions|mark\s*scheme)\b[^:]{0,40}:?\s*$", re.I)
SECTION_PREFIX_RE = re.compile(r"^\s*(?:section|part|paper)\s*[A-Za-z0-9]{0,4}\b", re.I)
MARKS_WORD_RE = re.compile(r"\s*[\(\[]\s*(\d+(?:\.\d+)?)\s*(?:marks?|mks?|pts?|points?)\s*[\)\]]\s*$", re.I)
HEADER_MARKS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:marks?|mks?)", re.I)
BLANK_RE = re.compile(r"_{2,}|\.{4,}|…+|\[\s*\]|\(\s{2,}\)")
LABEL_RE = re.compile(r"^\(?([0-9]{1,3}|[A-Ha-h])\s*[.):\-]\s*(.*)$")
PAIR_RE = re.compile(r"(?<![\w])(?:q\.?\s*)?(\d{1,3})\s*[-–—.):=→]+\s*\(?([A-Ha-h])\)?(?![\w])", re.I)
MATCH_STEM_RE = re.compile(r"\bmatch(?:ing)?\b|\bcolumn\s*[ab]\b|\bpair\b", re.I)
SECTION_NAMES = {"mcq": "Multiple choice", "fill": "Fill in the blank", "match": "Matching", "open": "Open questions"}


def classify_header(text):
    t = text.lower()
    if len(t) > 110:
        return None
    if re.search(r"true\s*(?:or|/|and)\s*false|\bt\s*/\s*f\b", t):
        return "tf"
    if re.search(r"multiple[\s-]*choice|\bmcqs?\b|choose the (?:correct|best)|select the (?:correct|best)", t):
        return "mcq"
    if re.search(r"fill[\s-]*(?:in)?[\s-]*(?:the)?[\s-]*(?:blanks?|gaps?)|complete the (?:sentences?|statements?)|gap[\s-]*fill", t):
        return "fill"
    if re.search(r"\bmatch(?:ing)?\b", t):
        return "match"
    if re.search(r"open[\s-]*(?:ended)?\s*questions?|essay|short[\s-]*answer|structured|long[\s-]*answer|"
                 r"written\s+(?:questions?|section|part)|theory\s+(?:questions?|section)|descriptive|"
                 r"practical\s+(?:questions?|section)", t):
        return "open"
    return None


def _is_header(ln):
    """A heading-like line that names a section type."""
    text = ln.text.strip()
    if not text or Q_RE.match(text) or OPT_RE.match(text) or ln.cells:
        return None
    kind = classify_header(text)
    if kind is None:
        return None
    letters = re.sub(r"[^A-Za-z]", "", text)
    shouting = len(letters) >= 6 and letters.isupper()
    if ln.heading or SECTION_PREFIX_RE.match(text) or shouting or (ln.emph and len(text) <= 90 and not text.endswith(("?", "."))):
        return kind
    return None


def _norm_label(s):
    return re.sub(r"[^0-9a-z]", "", (s or "").casefold())


def _norm_text(s):
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


class _Q:
    def __init__(self, number, text, section=None):
        self.number = number
        self.text = text
        self.section = section            # forced by a section heading, else None (inferred later)
        self.options = []                 # [[label, text, emph]]
        self.left, self.right = [], []    # [[label, text]]
        self.ans = []                     # raw answer lines
        self.ans_open = False
        self.hint = None
        self.rows_aligned = False
        self.key_source = ""
        self.tf = False


class _Parser:
    def run(self, lines, stats):
        self.qs, self.cur = [], None
        self.section, self.tf = None, False
        self.in_key, self.key_section = False, None
        self.title, self.intro, self.started = "", [], False
        self.header_marks = {}
        self.key_scoped, self.key_loose = {}, {}
        for ln in lines:
            self.feed(ln)
        self.close()
        return self.finish(stats)

    # -- feeding ------------------------------------------------------------ #
    def close(self):
        if self.cur is not None:
            self.qs.append(self.cur)
        self.cur = None

    def feed(self, ln):
        text = ln.text.strip()
        if not text or text.startswith("//") or (text.startswith("[[") and text.endswith("]]")):
            return                                     # blank lines and guidance notes
        if not self.in_key and KEY_RE.match(text) and not ANS_RE.match(text):
            self.close(); self.in_key = True; self.key_section = None
            return
        if self.in_key:
            return self.key_line(ln)
        kind = _is_header(ln)
        if kind:
            self.close()
            self.section = "mcq" if kind == "tf" else kind
            self.tf = kind == "tf"
            m = HEADER_MARKS_RE.search(text)
            if m:
                # True/False and multiple choice are both the MCQ section, so their marks add up.
                self.header_marks[self.section] = self.header_marks.get(self.section, 0) + float(m.group(1))
            self.started = True
            return
        m = ANS_RE.match(text)
        if m and self.cur is not None:
            self.cur.ans.append(m.group(1).strip())
            self.cur.ans_open = True
            return
        match_mode = self.section == "match" or (self.cur is not None and self.cur.hint == "match")
        if ln.cells and len(ln.cells) >= 2:
            if match_mode:
                return self.match_row(ln.cells)
            ln = Line(text=" — ".join(ln.cells), emph=ln.emph)
            text = ln.text
        if match_mode and self.match_line(ln, text):
            return
        qm = Q_RE.match(text)
        if qm and not ln.bullet:
            self.close()
            self.started = True
            q = _Q(int(qm.group(1)), qm.group(2).strip(), self.section)
            q.tf = self.tf
            if self.section is None and MATCH_STEM_RE.search(q.text) and re.search(r"column|following|with", q.text, re.I):
                q.hint = "match"
            self.cur = q
            return
        om = OPT_RE.match(text)
        if self.cur is not None and self.cur.section not in ("open", "fill") and (om or ln.bullet):
            self.add_options(self.cur, ln, text, om)
            return
        if self.cur is not None:
            c = self.cur
            if c.ans_open:
                c.ans.append(text)
            elif c.options:
                c.options[-1][1] += " " + text
            else:
                c.text += "\n" + text
            return
        if not self.started and not self.title:
            self.title = text
        elif not self.started:
            self.intro.append(text)

    def add_options(self, q, ln, text, om):
        if om:
            parts = self.split_inline(text)
            if parts:
                for lab, t in parts:
                    q.options.append([lab, t, False])
                return
            q.options.append([om.group(1).upper(), om.group(2).strip(), ln.emph])
        else:
            q.options.append([chr(65 + len(q.options)), text, ln.emph])

    @staticmethod
    def split_inline(text):
        marks = list(re.finditer(r"(?:^|\s)\(?([A-Ha-h])[.)]\s+", text))
        if len(marks) < 3:
            return None
        letters = [m.group(1).upper() for m in marks]
        if any(ord(letters[i + 1]) - ord(letters[i]) != 1 for i in range(len(letters) - 1)):
            return None
        out = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out.append((letters[i], text[m.end():end].strip()))
        return out

    # -- matching ------------------------------------------------------------ #
    def new_match(self, text="Match each item in the first column with the correct item in the second column."):
        self.close()
        q = _Q(None, text, "match" if self.section == "match" else None)
        q.hint = "match"
        self.cur = q
        return q

    def match_row(self, cells):
        q = self.cur if (self.cur is not None and self.cur.hint == "match") else self.new_match()
        if not q.left and not q.right and re.search(r"column\s*[ab]\b", " ".join(cells[:2]), re.I):
            return                                                  # header row
        a, b = cells[0], cells[1]
        la, lb = LABEL_RE.match(a), LABEL_RE.match(b)
        q.left.append([la.group(1) if la else "", (la.group(2) if la else a).strip()])
        q.right.append([lb.group(1) if lb else "", (lb.group(2) if lb else b).strip()])
        q.rows_aligned = not (la and lb)

    def match_line(self, ln, text):
        """Returns True if the line was consumed as part of a matching question."""
        q = self.cur if (self.cur is not None and self.cur.hint == "match") else None
        qm = Q_RE.match(text)
        om = OPT_RE.match(text)
        if q is None:
            if qm and MATCH_STEM_RE.search(qm.group(2)) and len(qm.group(2)) > 20:
                q = self.new_match(qm.group(2).strip()); q.number = int(qm.group(1)); self.started = True
                return True
            if qm:
                q = self.new_match(); self.started = True
                q.left.append([qm.group(1), qm.group(2).strip()])
                return True
            if om:
                q = self.new_match(); self.started = True
                q.right.append([om.group(1).upper(), om.group(2).strip()])
                return True
            q = self.new_match(text); self.started = True
            return True
        if om and not (qm and not q.left):
            q.right.append([om.group(1).upper(), om.group(2).strip()])
            return True
        if qm:
            if q.right:                                              # items finished: a new question starts
                if self.section == "match":
                    q2 = self.new_match(); q2.left.append([qm.group(1), qm.group(2).strip()])
                    return True
                self.close()
                return False
            if not q.left and MATCH_STEM_RE.search(qm.group(2)) and len(qm.group(2)) > 20:
                q.text, q.number = qm.group(2).strip(), int(qm.group(1))
                return True
            q.left.append([qm.group(1), qm.group(2).strip()])
            return True
        if ln.bullet:
            (q.right if q.left else q.left).append(["", text])
            return True
        if not q.left and not q.right:
            q.text += "\n" + text
            return True
        return False

    # -- answer key block ------------------------------------------------------ #
    def key_line(self, ln):
        text = ln.text.strip()
        kind = _is_header(ln)
        if kind:
            self.key_section = "mcq" if kind == "tf" else kind
            return
        if ln.cells:
            c = ln.cells
            if len(c) >= 4 and len(c) % 2 == 0 and re.fullmatch(r"\d{1,3}", c[0]) and re.fullmatch(r"\d{1,3}", c[2]):
                for i in range(0, len(c), 2):
                    self.put_key(c[i], c[i + 1])
                return
            m = re.fullmatch(r"(?:q\.?\s*)?(\d{1,3})\.?", c[0], re.I)
            if m:
                self.put_key(m.group(1), " ".join(c[1:]))
            return
        found = list(PAIR_RE.finditer(text))
        if found and not re.sub(r"[\s,;.|/\-–—:()]", "", PAIR_RE.sub("", text)):
            for m in found:
                self.put_key(m.group(1), m.group(2))
            return
        m = Q_RE.match(text)
        if m:
            self.put_key(m.group(1), m.group(2))
        elif getattr(self, "_last_key", None):
            n, sec = self._last_key
            if self.key_scoped.get((sec, n)) is not None:
                self.key_scoped[(sec, n)] += "\n" + text
            if self.key_loose.get(n):
                self.key_loose[n][-1] += "\n" + text

    def put_key(self, number, raw):
        n = int(number)
        raw = raw.strip()
        self._last_key = (n, self.key_section)
        if self.key_section:
            self.key_scoped[(self.key_section, n)] = raw
        self.key_loose.setdefault(n, []).append(raw)

    # -- finishing --------------------------------------------------------------- #
    def finish(self, stats):
        questions, warnings = [], []
        used_loose = {}
        for q in self.qs:
            d = self.build(q, warnings, used_loose)
            if d:
                questions.append(d)
        if len(questions) > MAX_QUESTIONS:
            warnings.append(f"Only the first {MAX_QUESTIONS} questions were read.")
            questions = questions[:MAX_QUESTIONS]
        if stats.get("images"):
            warnings.append(f"The document has {stats['images']} image(s). Images are not imported, so check any "
                            "question that depends on a picture.")
        return {"title": self.title[:200], "intro": "\n".join(self.intro)[:3000], "questions": questions,
                "warnings": warnings, "header_marks": self.header_marks}

    def raw_key(self, q, kind):
        sec = kind
        if q.number is not None:
            if (sec, q.number) in self.key_scoped:
                return self.key_scoped[(sec, q.number)], "answer key"
            loose = self.key_loose.get(q.number)
            if loose:
                idx = self._loose_used.get(q.number, 0)
                if idx < len(loose):
                    self._loose_used[q.number] = idx + 1
                    return loose[idx], "answer key"
        return None, ""

    def build(self, q, warnings, used):
        self._loose_used = used
        text = q.text.strip()
        marks = None
        m = MARKS_WORD_RE.search(text) or re.search(r"\s*\[\s*(\d+(?:\.\d+)?)\s*\]\s*$", text)
        if m:
            marks = float(m.group(1))
            text = text[:m.start()].strip()
        text = BLANK_RE.sub("____", text)[:MAX_TEXT]
        kind = q.section
        n_opts = len(q.options)
        if kind is None:
            if q.hint == "match":
                kind = "match"
            elif n_opts >= 2:
                kind = "mcq"
            elif "____" in text:
                kind = "fill"
            elif re.search(r"\(\s*t\s*/\s*f\s*\)|true\s+or\s+false", text, re.I):
                kind = "mcq"
            else:
                kind = "open"
        if kind != "mcq" and q.options:                              # lettered parts belong to the text
            text += "".join(f"\n{o[0].lower()}) {o[1]}" for o in q.options)
            q.options = []
        elif kind == "mcq" and 0 < len(q.options) < 2:
            text += "".join(f"\n{o[0].lower()}) {o[1]}" for o in q.options)
            q.options = []
        if not text and not (q.left or q.right):
            return None
        d = {"number": q.number, "section": kind, "text": text, "marks": marks, "warnings": [],
             "key_source": "", "options": [], "correct": [], "multi": False, "accepted": [], "guide": "",
             "left": [], "right": [], "pairs": {}}
        raw = "\n".join(q.ans).strip() if q.ans else None
        source = "answer line" if raw else ""
        if raw is None:
            raw, source = self.raw_key(q, kind)
        if kind == "mcq":
            opts = [[l, t[:MAX_OPTION], e] for l, t, e in q.options]
            if not opts and (q.tf or re.search(r"true\s+or\s+false|\(\s*t\s*/\s*f\s*\)", text, re.I)):
                opts = [["A", "True", False], ["B", "False", False]]
            d["options"] = [o[1] for o in opts]
            if len(opts) < 2:
                d["warnings"].append("Fewer than two options were found, so this was kept as an open question.")
                d["section"] = "open"
                kind = "open"
            else:
                idx = self.resolve_mcq(raw, opts) if raw else None
                if idx:
                    d["correct"], d["key_source"] = idx, source
                else:
                    flagged = [i for i, o in enumerate(opts) if o[2]]
                    if 1 <= len(flagged) < len(opts):
                        d["correct"], d["key_source"] = flagged, "bold / underlined option"
                    elif raw:
                        d["warnings"].append(f"Could not tell which option “{raw[:40]}” refers to.")
                if len(d["correct"]) > 1:
                    d["multi"] = True            # several correct options: default to "select all that apply"
                    d["warnings"].append("More than one correct option. Imported as “select all that apply”; "
                                         "untick that below if any one of them should be accepted instead.")
                if not d["correct"]:
                    d["warnings"].append("No correct answer found. Choose it below.")
        if kind == "fill":
            if raw:
                parts = [re.sub(r"\.$", "", p.strip().strip("\"'“”‘’")) for p in re.split(r"\s*/\s*|\s*;\s*|\s+\|\s+|\n", raw)]
                d["accepted"] = [p for p in parts if p][:20]
                d["key_source"] = source
            if not d["accepted"]:
                d["warnings"].append("No answer found. Type the accepted answer(s) below.")
        if kind == "open":
            if raw:
                d["guide"], d["key_source"] = raw[:2000], source
        if kind == "match":
            self.build_match(q, d, raw, source)
        d["text"] = d["text"] or "Match each item in the first column with the correct item in the second column."
        return d

    def resolve_mcq(self, raw, opts):
        """Returns the list of correct option indexes, or None if the answer can't be understood."""
        r = raw.strip()
        if re.fullmatch(r"(?:t|true)", r, re.I) and [o[1].casefold() for o in opts[:2]] == ["true", "false"]:
            return [0]
        if re.fullmatch(r"(?:f|false)", r, re.I) and [o[1].casefold() for o in opts[:2]] == ["true", "false"]:
            return [1]
        tokens = [t for t in re.split(r"\s*(?:,|;|/|&|\+|\band\b)\s*", r, flags=re.I) if t]
        if len(tokens) >= 2 and all(re.fullmatch(r"\(?[A-Ha-h]\)?", t) for t in tokens):
            idx = sorted({ord(t.strip("()").upper()) - 65 for t in tokens})
            return idx if all(0 <= i < len(opts) for i in idx) else None
        m = re.fullmatch(r"\(?([A-Ha-h])\)?", r) or re.match(r"^\(?([A-Ha-h])(?:\)|[.:\-–—])\s+(.+)$", r)
        if m:
            i = ord(m.group(1).upper()) - 65
            if 0 <= i < len(opts):
                return [i]
        for i, o in enumerate(opts):
            if _norm_text(o[1]) == _norm_text(r):
                return [i]
        return None

    def build_match(self, q, d, raw, source):
        left = [[l, t[:MAX_OPTION]] for l, t in q.left if t]
        right = [[l, t[:MAX_OPTION]] for l, t in q.right if t]
        d["left"], d["right"] = [t for _l, t in left], [t for _l, t in right]
        if len(left) < 2:
            d["warnings"].append("Fewer than two items to match were found.")
            return
        pairs = {}
        if raw:
            lmap = {_norm_label(l): i for i, (l, _t) in enumerate(left) if l}
            rmap = {_norm_label(l): i for i, (l, _t) in enumerate(right) if l}
            for x, y in re.findall(r"([A-Za-z0-9]{1,3})\s*[-–—→=:.>]+\s*([A-Za-z0-9]{1,3})", raw):
                x, y = _norm_label(x), _norm_label(y)
                if x in lmap and y in rmap:
                    pairs[str(lmap[x])] = rmap[y]
                elif y in lmap and x in rmap:
                    pairs[str(lmap[y])] = rmap[x]
            if pairs:
                d["key_source"] = source
        if not pairs and q.rows_aligned and len(left) == len(right):
            pairs = {str(i): i for i in range(len(left))}
            d["key_source"] = "table rows"
            d["warnings"].append("No key found: each table row was taken as a correct pair. Check the pairs below.")
        if len(pairs) < 2:
            d["warnings"].append("No matching key found, so this question cannot be imported yet. Add it by hand "
                                 "after importing the others.")
            pairs = {}
        d["pairs"] = pairs


def parse_upload(filename, data):
    """Entry point. Returns the parsed draft (JSON-serialisable dict)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportProblem(f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    name = (filename or "").lower()
    if name.endswith(".docx"):
        lines, stats = read_docx(data)
        kind = "Word document"
    elif name.endswith((".html", ".htm")):
        lines, stats = read_html(data)
        kind = "HTML file"
    elif name.endswith(".doc"):
        raise ImportProblem("Old .doc files can't be read. Open it in Word, use Save As → Word Document (.docx), then upload that.")
    else:
        raise ImportProblem("Upload a Word (.docx) or HTML (.html) file.")
    draft = _Parser().run(lines, stats)
    draft["source"] = kind
    if not draft["questions"]:
        raise ImportProblem("No questions were found. Number each question (1. 2. 3.) and put the options on lines "
                            "starting A. B. C. See the layout guide on this page.")
    return draft
