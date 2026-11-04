"""
assessments.charts
==================

Chart geometry for the item analysis page, as percentages of the plot area. The template draws plain HTML
boxes at these positions (no SVG, no JavaScript library), so the charts print, scale, follow the light and
dark theme and render the same in every browser and in the PDF engine.
"""

import math


def _nice_axis(max_count):
    """(step, top) for a count axis with at most ~5 whole-number ticks."""
    step = 1 if max_count <= 5 else math.ceil(max_count / 5)
    top = max(step * math.ceil(max_count / step), step)
    return step, top


def histogram(bins, total, mean=None, median=None):
    """Score distribution: one bar per 10% band, count ticks, and mean / median markers on the same scale."""
    if not bins or not total:
        return None
    step, top = _nice_axis(max(b["count"] for b in bins))
    n = len(bins)
    col = 100 / n
    bars = [{"left": round(i * col + col * 0.1, 2), "width": round(col * 0.8, 2), "h": round(100 * b["count"] / top, 2),
             "count": b["count"], "label": b["label"], "pct_lo": i * 10, "pct_hi": (i + 1) * 10}
            for i, b in enumerate(bins)]

    def marker(value):
        if value is None:
            return None
        left = min(max(value / total, 0), 1) * 100
        return {"left": round(left, 2), "flip": left > 78}

    return {"bars": bars,
            "yticks": [{"bottom": round(100 * v / top, 2), "label": v} for v in range(0, top + 1, step)],
            "xticks": [{"left": round(i * col, 2), "label": f"{i * 10}%"} for i in range(n + 1)],
            "mean": marker(mean), "median": marker(median)}


LO, HI = -0.4, 1.0          # discrimination range drawn


def scatter(items):
    """
    Difficulty (share of the marks candidates earned) against discrimination (does the question separate
    strong from weak candidates). Only questions with both figures appear.
    """
    pts = [it for it in items if it.get("score_pct") is not None and it.get("disc") is not None]
    if not pts:
        return None

    def y(d):
        return round((min(max(d, LO), HI) - LO) / (HI - LO) * 100, 2)

    return {
        "weak_h": y(0.1), "zero": y(0),
        "xticks": [{"left": v, "label": f"{v}%"} for v in range(0, 101, 20)],
        "yticks": [{"bottom": y(v), "label": f"{v:g}"} for v in (-0.4, -0.2, 0, 0.2, 0.4, 0.6, 0.8, 1.0)],
        "points": [{"left": min(max(it["score_pct"], 0), 100), "bottom": y(it["disc"]), "n": it["n"], "band": it["band"],
                    "title": f"Q{it['n']}: {it['score_pct']}% of marks, discrimination {it['disc']}"} for it in pts],
    }
