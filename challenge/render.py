"""
challenge.render
================

Builds a 1080 x 1080 SVG card from its parts: title, funny illustration, speech bubble, question, answer, short
explanation and the brand footer. Every card has exactly the same layout, so a set of them looks like one series.

All text is XML-escaped and wrapped to fit; text that is still too long at the smallest size is cut with an
ellipsis rather than spilling out of its panel.
"""

import textwrap
from xml.sax.saxutils import escape

from . import brand, characters

SIZE = 1080
FONT = "'Segoe UI','Helvetica Neue',Arial,sans-serif"
INK = characters.INK

# Limits the forms enforce, so a card never needs cutting.
LIMITS = {"title": 60, "bubble": 110, "question": 150, "answer": 120, "explanation": 200}

PALETTES = [
    {"bg1": "#141a45", "bg2": "#2b2070", "accent": "#ffd24a", "light": "#8bd3ff"},
    {"bg1": "#083038", "bg2": "#0d6272", "accent": "#ff9f43", "light": "#9ff0e0"},
    {"bg1": "#2a1054", "bg2": "#7a2f8f", "accent": "#ffd24a", "light": "#f0b3ff"},
    {"bg1": "#0b2a5b", "bg2": "#1456b8", "accent": "#7cf0c2", "light": "#b9dcff"},
    {"bg1": "#4a1042", "bg2": "#a1286b", "accent": "#ffe66d", "light": "#ffc2e0"},
    {"bg1": "#0b3b2e", "bg2": "#1c7a4f", "accent": "#ffd24a", "light": "#b8f5c9"},
]


def palette_for(key):
    return PALETTES[abs(int(key or 0)) % len(PALETTES)]


def _fit(text, width, sizes, factor):
    """
    (size, lines). ``sizes`` is [(font size, most lines allowed at that size), ...], largest first. ``factor`` is the
    average character width as a share of the font size; it is deliberately generous so the text also fits in wide
    fallback fonts. Text that still does not fit at the smallest size is cut with an ellipsis.
    """
    text = " ".join((text or "").split())
    for size, limit in sizes:
        lines = textwrap.wrap(text, width=max(8, int(width / (size * factor))), break_long_words=True) or [""]
        if len(lines) <= limit:
            return size, lines
    size, limit = sizes[-1]
    lines = textwrap.wrap(text, width=max(8, int(width / (size * factor))), break_long_words=True)[:limit]
    lines[-1] = lines[-1].rstrip(" .,;:")[:-1] + "…" if len(lines[-1]) > 2 else lines[-1] + "…"
    return size, lines


def _lines(lines, x, y, size, weight, fill, anchor="start", lh=1.22, style=""):
    out = []
    for i, line in enumerate(lines):
        out.append(f'<text x="{x}" y="{y + round(i * size * lh)}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
                   f'text-anchor="{anchor}" {style}>{escape(line)}</text>')
    return "".join(out)


def alt_text(card):
    return (f"{card['title']}. {card['bubble']} Question: {card['question']} Answer: {card['answer']}. "
            f"{card['explanation']}")


def render_card(card, institution=""):
    """
    ``card`` is a dict with: title, bubble, question, answer, explanation, character, mood, module_code, module_name,
    number, palette (any int). Returns the SVG as a string.
    """
    p = palette_for(card.get("palette", 0))
    acc, light = p["accent"], p["light"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}" '
        f'role="img" aria-labelledby="t d" font-family="{FONT}">',
        f'<title id="t">{escape(card["title"])}</title><desc id="d">{escape(alt_text(card))}</desc>',
        f'<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{p["bg1"]}"/><stop offset="1" stop-color="{p["bg2"]}"/></linearGradient>'
        '<filter id="sh" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="8" stdDeviation="8" flood-color="#000" flood-opacity=".28"/></filter></defs>',
        f'<rect width="{SIZE}" height="{SIZE}" fill="url(#bg)"/>',
        f'<circle cx="980" cy="120" r="260" fill="{light}" opacity=".07"/><circle cx="70" cy="1010" r="230" fill="{acc}" opacity=".07"/>',
    ]

    # ---- header: brand and topic
    code = escape((card.get("module_code") or "")[:18])
    parts.append(f'<text x="60" y="64" font-size="27" font-weight="800" letter-spacing="5" fill="{acc}">{escape(brand.NAME)}</text>')
    if code:
        w = 40 + len(code) * 17
        parts.append(f'<rect x="{SIZE - 60 - w - 112}" y="32" width="{w}" height="46" rx="23" fill="#ffffff" fill-opacity=".14"/>'
                     f'<text x="{SIZE - 60 - w / 2 - 112}" y="64" font-size="24" font-weight="700" fill="#ffffff" text-anchor="middle">{code}</text>')
    parts.append(f'<rect x="{SIZE - 60 - 96}" y="32" width="96" height="46" rx="23" fill="{acc}"/>'
                 f'<text x="{SIZE - 60 - 48}" y="65" font-size="26" font-weight="800" fill="{INK}" text-anchor="middle">#{int(card.get("number") or 0):02d}</text>')

    # ---- title
    size, lines = _fit(card["title"], 940, [(64, 2), (56, 2), (48, 2), (42, 2)], 0.7)
    parts.append(_lines(lines, 60, 150, size, 800, "#ffffff", lh=1.12))
    parts.append(f'<rect x="60" y="{150 + round((len(lines) - 1) * size * 1.12) + 22}" width="120" height="8" rx="4" fill="{acc}"/>')

    # ---- scene: character + speech bubble
    parts.append(f'<g transform="translate(44 262) scale(.9)" filter="url(#sh)">{characters.draw(card.get("character"), card.get("mood"), light)}</g>')
    parts.append('<g filter="url(#sh)"><path d="M474 296 a40 40 0 0 1 40 -40 H980 a40 40 0 0 1 40 40 V474 a40 40 0 0 1 -40 40 H514 a40 40 0 0 1 -40 -40 V430 L428 410 L474 372 Z" '
                 f'fill="#ffffff" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/></g>')
    size, lines = _fit(card["bubble"], 450, [(38, 4), (34, 5), (30, 5), (27, 5)], 0.66)
    top = 385 - round((len(lines) * size * 1.2) / 2) + size - 6
    parts.append(_lines(lines, 746, top, size, 700, INK, anchor="middle", lh=1.2))

    # ---- question
    parts.append(f'<g filter="url(#sh)"><rect x="60" y="588" width="960" height="150" rx="26" fill="#ffffff"/></g>'
                 f'<rect x="60" y="588" width="150" height="38" rx="19" fill="{acc}"/>'
                 f'<text x="135" y="615" font-size="20" font-weight="800" letter-spacing="2" fill="{INK}" text-anchor="middle">QUESTION</text>')
    size, lines = _fit(card["question"], 890, [(36, 2), (32, 2), (28, 3)], 0.66)
    parts.append(_lines(lines, 84, 668, size, 700, INK, lh=1.15))

    # ---- answer
    parts.append(f'<g filter="url(#sh)"><rect x="60" y="750" width="960" height="136" rx="26" fill="#0e6b44"/></g>'
                 '<rect x="60" y="750" width="120" height="38" rx="19" fill="#7cf0b4"/>'
                 f'<text x="120" y="777" font-size="20" font-weight="800" letter-spacing="2" fill="#06301f" text-anchor="middle">ANSWER</text>')
    size, lines = _fit(card["answer"], 890, [(38, 2), (32, 2), (28, 3)], 0.7)
    parts.append(_lines(lines, 84, 826 if len(lines) < 3 else 814, size, 800, "#ffffff", lh=1.1))

    # ---- explanation
    size, lines = _fit("Why? " + card["explanation"], 950, [(26, 3), (24, 3), (22, 3)], 0.6)
    parts.append(_lines(lines, 60, 924, size, 500, "#eef0ff", lh=1.22))

    # ---- footer
    parts.append(f'<rect x="0" y="1004" width="{SIZE}" height="76" fill="#000000" fill-opacity=".28"/>'
                 f'<text x="{SIZE / 2}" y="1038" font-size="26" font-weight="800" letter-spacing="4" fill="{acc}" text-anchor="middle">{escape(brand.NAME)}</text>'
                 f'<text x="{SIZE / 2}" y="1064" font-size="21" font-weight="600" fill="#ffffff" text-anchor="middle">{escape(brand.TAGLINE)}'
                 f'{(" · " + escape(institution)) if institution else ""}</text>')
    parts.append("</svg>")
    return "".join(parts)
