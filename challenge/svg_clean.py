"""
challenge.svg_clean
===================

Makes an uploaded SVG safe to store and to serve. An SVG is code: it can carry scripts, event handlers, links to other
sites and embedded pages. This keeps only drawing elements and safe attributes, and rejects anything that looks like an
XML entity trick. Cards are also served as images with a restrictive Content-Security-Policy, so this is a second layer.
"""

import re
import xml.etree.ElementTree as ET

MAX_BYTES = 1_500_000
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

ALLOWED_TAGS = {
    "svg", "g", "defs", "symbol", "use", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "text", "tspan", "textPath", "image", "style", "linearGradient", "radialGradient", "stop", "clipPath",
    "mask", "pattern", "marker", "filter", "feBlend", "feColorMatrix", "feComposite", "feDropShadow", "feFlood",
    "feGaussianBlur", "feMerge", "feMergeNode", "feOffset", "feMorphology",
}
SAFE_DATA_IMAGE = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]+$", re.I)
BAD_STYLE = re.compile(r"(@import|expression\s*\(|javascript:|behavior\s*:|-moz-binding|url\s*\(\s*['\"]?(?!#|data:image/))", re.I)

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


class SVGError(ValueError):
    """The upload cannot be used. The message is plain English for the trainer."""


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _clean_css(text):
    """Stylesheet text with every @import, every url() that is not local, and the old script-in-CSS tricks taken out."""
    text = re.sub(r"@import[^;{}]*;?", "", text, flags=re.I)
    text = re.sub(r"url\s*\(\s*['\"]?(?!#|data:image/)[^)]*\)", "", text, flags=re.I)
    return re.sub(r"expression\s*\([^)]*\)|javascript:|behavior\s*:[^;}]*|-moz-binding\s*:[^;}]*", "", text, flags=re.I)


def _clean_attrs(el):
    for name in list(el.attrib):
        value = el.attrib[name]
        local = _local(name).lower()
        if local.startswith("on"):
            del el.attrib[name]
        elif local == "href":
            v = value.strip()
            if not (v.startswith("#") or SAFE_DATA_IMAGE.match(v)):
                del el.attrib[name]
        elif local == "style" and BAD_STYLE.search(value):
            del el.attrib[name]
        elif BAD_STYLE.search(value) and local not in ("d", "points"):
            del el.attrib[name]


def _clean_tree(el):
    for child in list(el):
        if _local(child.tag) not in ALLOWED_TAGS:
            el.remove(child)
            continue
        _clean_tree(child)
    _clean_attrs(el)
    if _local(el.tag) == "style" and el.text:
        el.text = _clean_css(el.text)


def clean_svg(raw):
    """Return (clean_svg_text, warnings). Raises SVGError when the file cannot be used."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            raise SVGError(f"This file is too big. Keep SVG cards under {MAX_BYTES // 1_000_000:.1f} MB.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise SVGError("This is not a text SVG file.")
    else:
        text = raw
    if len(text) > MAX_BYTES:
        raise SVGError(f"This file is too big. Keep SVG cards under {MAX_BYTES // 1_000_000:.1f} MB.")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)|<\?xml-stylesheet", text, re.I):
        raise SVGError("This SVG contains DOCTYPE or ENTITY parts, which are not allowed. Export it again as a plain SVG.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise SVGError("This file could not be read as an SVG. Export it again from your design tool.")
    if _local(root.tag) != "svg":
        raise SVGError("This file is not an SVG image.")
    _clean_tree(root)
    warnings = []
    if "xmlns" not in root.attrib and not root.tag.startswith("{"):
        root.set("xmlns", SVG_NS)
    view = re.split(r"[\s,]+", (root.get("viewBox") or "").strip())
    try:
        w, h = float(view[2]), float(view[3])
    except (IndexError, ValueError):
        w = h = 0
    if not w:
        root.set("viewBox", "0 0 1080 1080")
        warnings.append("This SVG had no viewBox, so 1080 x 1080 was assumed.")
    elif abs(w - h) > 1 or round(w) != 1080:
        warnings.append(f"This SVG is {w:g} x {h:g}. Cards are designed at 1080 x 1080 and are shown square.")
    for attr in ("width", "height"):                       # let the page decide the size
        root.attrib.pop(attr, None)
    return ET.tostring(root, encoding="unicode"), warnings
