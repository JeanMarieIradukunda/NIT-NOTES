"""Pictures for knowledge cards: what is accepted, how it is recognised, and what a file is called."""

import os
import re

MAX_IMAGE_BYTES = 3_500_000          # Vercel rejects a request body over ~4.5 MB, so keep each file well under that
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
LABELS = {".png": "PNG", ".jpg": "JPG", ".webp": "WebP"}


def detect_image(data):
    """'.png', '.jpg' or '.webp' from the file's own first bytes (never from its name), else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def title_from_filename(name, limit):
    """'ping-pong_card.png' -> 'Ping pong card'. Always returns something, never longer than ``limit``."""
    base = os.path.splitext(os.path.basename((name or "").replace("\\", "/")))[0]
    if base.lower() in (".png", ".jpg", ".jpeg", ".webp", ".svg"):        # a file that is only an extension
        base = ""
    base = re.sub(r"[\s_\-.]+", " ", base).strip()
    base = re.sub(r"\s+(copy|final|v\d+|\(\d+\))$", "", base, flags=re.I).strip()
    if not base:
        return "Untitled card"
    if base == base.lower():
        base = base[0].upper() + base[1:]
    return base[:limit].rstrip()


def size_text(n):
    return f"{n / 1048576:.1f} MB"
