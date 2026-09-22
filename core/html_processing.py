"""
core.html_processing
======================

Turns a raw HTML lesson file (whether uploaded live through the web form or
walked in from the legacy archive by `import_legacy`) into:

  * a clean HTML fragment safe to store and render — scripts, styles, inline
    presentation and stray navigation chrome removed, structure kept intact
  * plain text for search
  * a heading outline for the table of contents

This is deliberately conservative: it never rewrites the curriculum's own
wording, numbering or structure. It only strips presentation and page chrome
that doesn't belong once the lesson lives inside the platform's own template.
"""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString, Tag

STRIP_TAGS = {"script", "style", "noscript", "link", "meta", "iframe", "base",
              # Embedding / plugin content and live form controls have no place
              # in static notes and are a common route for smuggling content in.
              "object", "applet", "frame", "frameset", "portal", "template",
              "input", "select", "textarea"}
# NOTE: <embed> and <button> are deliberately NOT in STRIP_TAGS — they are
# unwrapped instead (tag removed, content kept):
#   * <embed> isn't a void element to the HTML parser, so everything after it
#     becomes its children; deleting it would delete the rest of the document.
#   * <button> often carries real text (e.g. accordion section titles).
UNWRAP_TAGS = ("embed", "button")

# The only classes that survive sanitising: the ones this module applies itself.
# Everything else came from the author's own stylesheet/framework and must go —
# the site loads Bootstrap, so a stray `collapse`, `d-none` or `modal` class on
# uploaded markup would otherwise silently HIDE the author's content.
KEEP_CLASSES = {"callout", "callout--warning", "callout--tip", "callout--note",
                "callout--example", "callout--definition", "codeblock",
                "table", "table-bordered", "table-sm", "data-table", "upload-figure-note"}
# Attributes that can carry a script or a foreign document, whatever the tag.
BLOCKED_ATTRS = {"srcdoc", "formaction", "ping"}
SCRIPT_URL_RE = re.compile(r"^(javascript|vbscript|data\s*:\s*text/html)", re.I)
NAV_HINTS = ("back-link", "backlink", "back-btn", "topbar", "navbar", "breadcrumb",
             "site-nav", "top-nav", "portal-header", "page-nav", "footer-nav")
HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
WORDS_PER_MINUTE = 190

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u27BF\uFE0F\u2B00-\u2BFF]+")
BANNER_RE = re.compile(r"(?i)^(padri\s*vjeko.*|.*\bTSS\b\s*$|nit[- ]resources.*)$")


class LessonContentError(ValueError):
    """Raised when an uploaded file can't reasonably be treated as a lesson."""


def slugify_id(value: str, fallback="section") -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or fallback


def clean_title(value: str) -> str:
    value = EMOJI_RE.sub(" ", value or "")
    value = re.sub(r"\s+", " ", value).strip(" -–—:|·").strip()
    value = re.sub(r"\s*[–—|-]\s*(padri\s*vjeko.*|nit[- ]resources.*|.*\bTSS\b.*)$",
                   "", value, flags=re.I).strip()
    return value


def _looks_like_nav(tag: Tag) -> bool:
    ident = " ".join(filter(None, [" ".join(tag.get("class", []) or []),
                                    tag.get("id", "") or ""])).lower()
    if any(h in ident for h in NAV_HINTS) or tag.name == "nav":
        return True
    if tag.name == "a":
        text = tag.get_text(" ", strip=True).lower()
        href = (tag.get("href") or "").strip()
        if href in ("../", "..", "./", "/", "index.html", "../index.html") and \
                len(text) < 40 and any(k in text for k in
                                       ("back", "parent", "home", "return", "directory")):
            return True
    return False


LESSON_FIGURE_NOTE = "[Figure: {alt} — re-attach this image separately after upload]"


def process_lesson_html(raw_html: str, *, max_bytes: int = 8 * 1024 * 1024,
                        figure_note: str = LESSON_FIGURE_NOTE) -> dict:
    """
    Parse an uploaded or imported HTML file into a lesson-ready dict:
    {title, fragment, words, minutes, tables, images, code_blocks, headings, text}

    Raises LessonContentError for empty, oversized or clearly non-lesson files
    (e.g. a renamed video, or a page that is almost entirely JavaScript).

    `figure_note` is the placeholder text (with an `{alt}` slot) shown in place
    of images that can't be resolved from a bare upload.
    """
    if len(raw_html.encode("utf-8", "ignore")) > max_bytes:
        raise LessonContentError(
            f"That file is larger than the {max_bytes // (1024 * 1024)} MB limit for notes.")

    soup = BeautifulSoup(raw_html, "lxml")
    body = soup.body or soup

    script_bytes = sum(len(s.string or "") for s in body.find_all("script"))
    plain_probe = body.get_text(" ", strip=True)
    if script_bytes > 3000 and len(plain_probe.split()) < 120:
        raise LessonContentError(
            "This looks like an interactive page (mostly script, little text) rather "
            "than lesson notes. Upload it as a resource file instead, or ask an "
            "administrator to add it as an assessment.")

    for tag in body.find_all(list(STRIP_TAGS)):
        tag.decompose()
    for tag in body.find_all(list(UNWRAP_TAGS)):
        tag.unwrap()
    for tag in body.find_all(True):
        if tag.parent is not None and _looks_like_nav(tag):
            tag.decompose()

    # External links open safely in a new tab; internal links to files the
    # uploader referenced by relative path can't be resolved here, so they are
    # left as plain text rather than a dead link.
    for a in list(body.find_all("a")):
        href = a.get("href") or ""
        if href.startswith(("http://", "https://")):
            a["target"] = "_blank"
            a["rel"] = "noopener noreferrer"
        elif href.startswith("#"):
            continue
        else:
            a.replace_with(NavigableString(a.get_text()))

    # Images referenced by relative path can't be resolved outside the
    # uploader's own machine; keep the alt text as a labelled placeholder
    # rather than a broken image icon.
    for img in list(body.find_all("img")):
        src = img.get("src") or ""
        if src.startswith("data:"):
            img["loading"] = "lazy"
            continue
        alt = img.get("alt") or "figure"
        note = soup.new_tag("p")
        note["class"] = ["upload-figure-note"]
        note.string = figure_note.format(alt=alt)
        img.replace_with(note)

    for tag in body.find_all(True):
        for attr in ("style", "bgcolor", "background", "align", "width", "height"):
            tag.attrs.pop(attr, None)
        # Defense in depth: an uploader could paste markup with inline event
        # handlers or a javascript: URL. Strip both regardless of source.
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
        for attr in list(tag.attrs):
            if attr.lower() in BLOCKED_ATTRS:
                del tag.attrs[attr]
                continue
            val = tag.attrs.get(attr)
            if isinstance(val, str):
                # Browsers ignore whitespace/control characters inside a URL
                # scheme ("java\tscript:"), so normalise before testing.
                compact = re.sub(r"[\x00-\x20]+", "", val)
                if SCRIPT_URL_RE.match(compact):
                    del tag.attrs[attr]
        if tag.name in ("font", "center", "big", "blink", "marquee", "form"):
            tag.unwrap()

    for _ in range(6):
        changed = False
        for tag in list(body.find_all(["div", "section", "main", "article"])):
            classes = " ".join(tag.get("class", []) or []).lower()
            if any(k in classes for k in ("container", "wrapper", "content",
                                          "portal", "page", "main", "card-body")):
                tag.unwrap()
                changed = True
        if not changed:
            break

    for tag in body.find_all(["div", "p", "blockquote"]):
        classes = " ".join(tag.get("class", []) or []).lower()
        full_text = tag.get_text(" ", strip=True)
        text = full_text[:90].lower()
        # A callout is a short, single notice. Without this guard, a wrapper div
        # from the source document whose class merely CONTAINS "note" (e.g.
        # "notes-container", "footnote-list", a Word/Google-Docs export class)
        # gets its entire multi-line content — headings and all — turned into
        # one large highlighted callout box, instead of just an actual notice.
        is_short_notice = len(full_text) <= 400 and (
            tag.name != "div" or tag.find(["div", "p", "h1", "h2", "h3", "h4", "h5", "h6",
                                            "ul", "ol", "table", "blockquote"]) is None
        )
        kind = None
        if "warn" in classes or text.startswith(("warning", "⚠")):
            kind = "warning"
        elif "tip" in classes or text.startswith(("tip", "💡")):
            kind = "tip"
        elif (re.search(r"\b(note|important)\b", classes)
              or text.startswith(("note", "important"))):
            # "note"/"important" are common English words and common substrings
            # of unrelated class names, so this one requires a whole-word class
            # match (not "notes-container") on top of the short-notice guard.
            kind = "note"
        elif "example" in classes or text.startswith(("example", "e.g.")):
            kind = "example"
        elif "definition" in classes or text.startswith("definition"):
            kind = "definition"
        if kind and is_short_notice:
            tag["class"] = ["callout", f"callout--{kind}"]

    for pre in body.find_all("pre"):
        pre["class"] = ["codeblock"]
        if not pre.find("code"):
            inner = pre.get_text()
            pre.clear()
            code = soup.new_tag("code")
            code.append(NavigableString(inner))
            pre.append(code)

    for table in body.find_all("table"):
        table["class"] = ["table", "table-bordered", "table-sm", "data-table"]
        if table.find("th") is None:
            first = table.find("tr")
            if first:
                for cell in first.find_all("td"):
                    cell.name = "th"

    for tag in body.find_all(True):
        if tag.get("class"):
            keep = [c for c in tag["class"] if c in KEEP_CLASSES]
            if keep:
                tag["class"] = keep
            else:
                del tag.attrs["class"]
        for attr in [a for a in tag.attrs if a.lower().startswith("data-")]:
            del tag.attrs[attr]     # would otherwise drive Bootstrap's JS behaviours

    used_ids, headings, h1_text = set(), [], ""
    for h in body.find_all(HEADING_TAGS):
        text = h.get_text(" ", strip=True)
        if not text:
            h.decompose()
            continue
        level = int(h.name[1])
        if level == 1 and not h1_text:
            h1_text = text
        base = slugify_id(text)[:60]
        hid, n = base, 2
        while hid in used_ids:
            hid = f"{base}-{n}"; n += 1
        used_ids.add(hid)
        h["id"] = hid
        if 2 <= level <= 3:
            headings.append({"id": hid, "level": level, "text": text})

    first_h1 = body.find("h1")
    if first_h1:
        first_h1.decompose()

    fragment = body.decode_contents().strip()
    plain = re.sub(r"\s+", " ", BeautifulSoup(fragment, "lxml").get_text(" ", strip=True))

    if len(plain.split()) < 15:
        raise LessonContentError(
            "This file has almost no readable text once scripts and styling are "
            "removed. Check the file contains the lesson notes, not just a wrapper page.")

    title = clean_title(h1_text) or clean_title(
        soup.title.get_text(strip=True) if soup.title else "")
    if not title or BANNER_RE.match(title):
        title = "Untitled lesson"

    words = len(plain.split())
    return {
        "title": title,
        "fragment": fragment,
        "words": words,
        "minutes": max(1, round(words / WORDS_PER_MINUTE)),
        "tables": len(body.find_all("table")),
        "images": len(body.find_all("img")),
        "code_blocks": len(body.find_all("pre")),
        "headings": headings,
        "text": plain[:6000],
    }
