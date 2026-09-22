"""
core.legacy_import
=====================

Support code for `manage.py import_legacy`. This is the same reference-repair
approach used for the original static-site rebuild — the legacy notes contain
~100 links written with hyphens/spaces that don't match the real filenames on
disk — adapted to write into the database and `media/legacy/` instead of a
static output folder.
"""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
import urllib.parse

from bs4 import BeautifulSoup, NavigableString, Tag

from .html_processing import (BANNER_RE, HEADING_TAGS, NAV_HINTS, STRIP_TAGS,
                              WORDS_PER_MINUTE, _looks_like_nav, clean_title, slugify_id)

APP_SCRIPT_BYTES = 3000
APP_MAX_WORDS = 400

ASSET_KINDS = {
    ".pdf": "pdf", ".docx": "doc", ".doc": "doc", ".txt": "text",
    ".py": "code", ".cs": "code", ".json": "code",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".svg": "image", ".webp": "image",
    ".mp4": "video", ".webm": "video",
}

SKIP_DIRS = {".git", ".vscode", "__pycache__"}
SKIP_FILES = {".ds_store", "thumbs.db"}
SKIP_EXT = {".bak", ".swp", ".rev", ".idx", ".pack", ".sample", ".orig"}

LO_PATTERNS = [re.compile(r"(?i)\blo[-_ ]?(\d{1,2})(?!\d)"),
              re.compile(r"(?i)learning\s*outcome\s*[-_ :]?\s*(\d{1,2})")]


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def title_case(value: str) -> str:
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if value and (value.isupper() or value.islower()):
        small = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or",
                 "the", "to", "with", "into", "from"}
        words = []
        for i, w in enumerate(value.split(" ")):
            lw = w.lower()
            if i and lw in small:
                words.append(lw)
            elif len(w) <= 4 and w.isupper():
                words.append(w)
            else:
                words.append(lw.capitalize())
        return " ".join(words)
    return value


def should_skip(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts):
        return True
    if parts[-1].lower() in SKIP_FILES:
        return True
    return os.path.splitext(parts[-1])[1].lower() in SKIP_EXT


def detect_outcome(*candidates: str):
    for text in candidates:
        if not text:
            continue
        for pattern in LO_PATTERNS:
            m = pattern.search(text)
            if m:
                return f"LO{int(m.group(1))}"
    return None


def is_app_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    script_bytes = sum(len(s.string or "") for s in soup.find_all("script"))
    body = soup.body
    words = len(body.get_text(" ", strip=True).split()) if body else 0
    return script_bytes > APP_SCRIPT_BYTES and words < APP_MAX_WORDS


class Tree:
    """Indexes every real file under `root` so broken references can be repaired."""

    def __init__(self, root: str):
        self.root = root
        self.files, self.dirs = [], []
        self._by_norm_path, self._by_dir_norm_name, self._dir_norm = {}, {}, {}

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            if rel_dir:
                self.dirs.append(rel_dir)
                self._dir_norm[norm(rel_dir)] = rel_dir
            for name in filenames:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                self.files.append(rel)
                self._by_norm_path.setdefault(norm(rel), rel)
                self._by_dir_norm_name.setdefault((rel_dir, norm(name)), rel)

        self.file_set = set(self.files)
        self.dir_set = set(self.dirs)

    def resolve(self, base_dir: str, url: str, _depth: int = 0):
        if not url:
            return None
        if url.startswith(("http://", "https://", "//", "#", "mailto:", "tel:",
                           "javascript:", "data:")):
            return None
        raw = urllib.parse.unquote(url.split("#")[0].split("?")[0])
        if not raw:
            return None
        target = os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
        target = target.lstrip("./").strip("/")

        if target in self.dir_set:
            idx = f"{target}/index.html"
            return idx if idx in self.file_set else target
        if target in self.file_set:
            return target

        base_norm = norm(os.path.basename(target))
        target_dir = os.path.dirname(target)
        hit = self._by_dir_norm_name.get((target_dir, base_norm))
        if hit:
            return hit
        hit = self._by_norm_path.get(norm(target))
        if hit:
            return hit
        hit = self._dir_norm.get(norm(target))
        if hit:
            idx = f"{hit}/index.html"
            return idx if idx in self.file_set else hit

        if _depth == 0:
            m = re.match(r"(?i)^(.*\.(?:png|jpe?g|gif|svg|webp|pdf|docx?|html?|py|txt))", target)
            if m and m.group(1) != target:
                again = self.resolve("", m.group(1), _depth + 1)
                if again:
                    return again

        candidates = [f for f in self.files if norm(os.path.basename(f)) == base_norm]
        if len(candidates) == 1:
            return candidates[0]
        return None


class LegacyExtractor:
    """
    Extracts a legacy lesson page into a stored fragment, copying any image it
    references into `media/legacy/<same relative path>` and rewriting its `src`
    to `/media/legacy/...` so the fragment is self-contained once saved.
    """

    def __init__(self, tree: Tree, media_root: str, media_url: str = "/media/legacy/"):
        self.tree = tree
        self.media_root = media_root
        self.media_url = media_url
        self._copied = set()

    def _publish(self, rel: str) -> str:
        """Copy a legacy file into media/legacy/<rel> once, return its public URL."""
        if rel not in self._copied:
            src = os.path.join(self.tree.root, rel)
            dst = os.path.join(self.media_root, "legacy", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            self._copied.add(rel)
        return self.media_url + urllib.parse.quote(rel)

    def extract(self, html: str, source_rel: str):
        soup = BeautifulSoup(html, "lxml")
        doc_title = soup.title.get_text(strip=True) if soup.title else ""
        body = soup.body or soup
        base_dir = os.path.dirname(source_rel)

        for tag in body.find_all(list(STRIP_TAGS)):
            tag.decompose()
        for tag in body.find_all(True):
            if tag.parent is not None and _looks_like_nav(tag):
                tag.decompose()

        broken_refs, fixed_refs = [], []
        for img in body.find_all("img"):
            src = img.get("src")
            resolved = self.tree.resolve(base_dir, src or "")
            if resolved:
                if norm(resolved) != norm(os.path.normpath(os.path.join(base_dir, src or ""))):
                    fixed_refs.append((src, resolved))
                img["src"] = self._publish(resolved)
                img["loading"] = "lazy"
                if not img.get("alt"):
                    img["alt"] = title_case(os.path.splitext(os.path.basename(resolved))[0])
            elif src and not src.startswith(("http", "data:")):
                broken_refs.append(src)
                img.decompose()

        for a in body.find_all("a"):
            href = a.get("href")
            if not href:
                continue
            if href.startswith(("http://", "https://")):
                a["target"] = "_blank"
                a["rel"] = "noopener noreferrer"
                continue
            if href.startswith("#"):
                continue
            resolved = self.tree.resolve(base_dir, href)
            if resolved:
                a["href"] = self._publish(resolved)
            else:
                broken_refs.append(href)
                a.unwrap()

        for tag in body.find_all(True):
            for attr in ("style", "bgcolor", "background", "align", "width", "height"):
                tag.attrs.pop(attr, None)
            for attr in list(tag.attrs):
                if attr.lower().startswith("on"):
                    del tag.attrs[attr]
            for attr in ("href", "src", "action", "formaction"):
                val = tag.attrs.get(attr, "")
                if isinstance(val, str) and val.strip().lower().startswith("javascript:"):
                    del tag.attrs[attr]
            if tag.name in ("font", "center", "big", "blink", "marquee"):
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
            text = tag.get_text(" ", strip=True)[:90].lower()
            kind = None
            if "warn" in classes or text.startswith(("warning", "⚠")):
                kind = "warning"
            elif "tip" in classes or text.startswith(("tip", "💡")):
                kind = "tip"
            elif "note" in classes or "important" in classes or text.startswith(("note", "important")):
                kind = "note"
            elif "example" in classes or text.startswith(("example", "e.g.")):
                kind = "example"
            elif "definition" in classes or text.startswith("definition"):
                kind = "definition"
            if kind:
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
                hid = f"{base}-{n}"
                n += 1
            used_ids.add(hid)
            h["id"] = hid
            if 2 <= level <= 3:
                headings.append({"id": hid, "level": level, "text": text})

        first_h1 = body.find("h1")
        if first_h1:
            first_h1.decompose()

        fragment = body.decode_contents().strip()
        plain = re.sub(r"\s+", " ", BeautifulSoup(fragment, "lxml").get_text(" ", strip=True))

        candidates = [h1_text, doc_title]
        title = ""
        for cand in candidates:
            cand = clean_title(cand or "")
            if cand and not BANNER_RE.match(cand) and len(cand) > 2:
                title = cand
                break

        return {
            "title": title, "doc_title": doc_title, "fragment": fragment,
            "words": len(plain.split()), "text": plain[:6000],
            "tables": len(body.find_all("table")), "images": len(body.find_all("img")),
            "code_blocks": len(body.find_all("pre")), "headings": headings,
            "broken_refs": broken_refs, "fixed_refs": fixed_refs,
        }

    def publish_file(self, rel: str) -> str:
        return self._publish(rel)
