"""
challenge.characters
====================

The cartoon characters that star on every card. Each is drawn in a 400 x 360 box and has a face whose mood can be
changed, because the joke is usually in the expression. Plain SVG fragments, no images, so cards stay small and sharp.
"""

INK = "#12163a"

CHARACTERS = [
    ("laptop", "Laptop"), ("router", "Router"), ("server", "Server"), ("bug", "Bug"),
    ("padlock", "Padlock"), ("cloud", "Cloud"), ("robot", "Robot"), ("chip", "Chip"),
]
MOODS = [
    ("happy", "Happy"), ("worried", "Worried"), ("shocked", "Shocked"), ("smug", "Smug"), ("cool", "Cool (sunglasses)"),
]
CHARACTER_KEYS = [k for k, _ in CHARACTERS]
MOOD_KEYS = [k for k, _ in MOODS]


def face(cx, cy, mood, scale=1.0, ink=INK, white="#ffffff"):
    """Eyes and mouth centred on (cx, cy). Eyes sit 34 units either side of the centre."""
    s = scale
    ex, ey, r = 34 * s, -8 * s, 17 * s
    g = [f'<g transform="translate({cx} {cy})" stroke-linecap="round" stroke-linejoin="round">']

    def eye(x, look=(0, 0), big=1.0):
        rr = r * big
        return (f'<circle cx="{x}" cy="{ey}" r="{rr:.1f}" fill="{white}" stroke="{ink}" stroke-width="{4 * s:.1f}"/>'
                f'<circle cx="{x + look[0] * s:.1f}" cy="{ey + look[1] * s:.1f}" r="{7 * s * big:.1f}" fill="{ink}"/>'
                f'<circle cx="{x + (look[0] + 2.5) * s:.1f}" cy="{ey + (look[1] - 3) * s:.1f}" r="{2.4 * s:.1f}" fill="{white}"/>')

    if mood == "cool":
        g.append(f'<rect x="{-ex - 26 * s:.1f}" y="{ey - 15 * s:.1f}" width="{52 * s:.1f}" height="{32 * s:.1f}" rx="{9 * s:.1f}" fill="{ink}"/>')
        g.append(f'<rect x="{ex - 26 * s:.1f}" y="{ey - 15 * s:.1f}" width="{52 * s:.1f}" height="{32 * s:.1f}" rx="{9 * s:.1f}" fill="{ink}"/>')
        g.append(f'<line x1="{-ex + 26 * s:.1f}" y1="{ey - 6 * s:.1f}" x2="{ex - 26 * s:.1f}" y2="{ey - 6 * s:.1f}" stroke="{ink}" stroke-width="{5 * s:.1f}"/>')
        g.append(f'<path d="M{-ex - 16 * s:.1f} {ey - 7 * s:.1f} l{14 * s:.1f} {-5 * s:.1f}" stroke="{white}" stroke-width="{4 * s:.1f}" opacity=".55"/>')
        g.append(f'<path d="M{-30 * s:.1f} {26 * s:.1f} Q0 {52 * s:.1f} {30 * s:.1f} {26 * s:.1f}" fill="none" stroke="{ink}" stroke-width="{6 * s:.1f}"/>')
    elif mood == "worried":
        g.append(eye(-ex, (0, -4)) + eye(ex, (0, -4)))
        g.append(f'<path d="M{-ex - 16 * s:.1f} {ey - 28 * s:.1f} L{-ex + 14 * s:.1f} {ey - 38 * s:.1f}" stroke="{ink}" stroke-width="{5 * s:.1f}" fill="none"/>')
        g.append(f'<path d="M{ex + 16 * s:.1f} {ey - 28 * s:.1f} L{ex - 14 * s:.1f} {ey - 38 * s:.1f}" stroke="{ink}" stroke-width="{5 * s:.1f}" fill="none"/>')
        g.append(f'<path d="M{-26 * s:.1f} {38 * s:.1f} q{13 * s:.1f} {-16 * s:.1f} {26 * s:.1f} 0 q{13 * s:.1f} {16 * s:.1f} {26 * s:.1f} 0" fill="none" stroke="{ink}" stroke-width="{6 * s:.1f}"/>')
        g.append(f'<path d="M{ex + 38 * s:.1f} {-30 * s:.1f} q{9 * s:.1f} {16 * s:.1f} 0 {24 * s:.1f} q{-9 * s:.1f} {-8 * s:.1f} 0 {-24 * s:.1f}z" fill="#7fd3ff" stroke="{ink}" stroke-width="{3 * s:.1f}"/>')
    elif mood == "shocked":
        g.append(eye(-ex, (0, 0), 1.25) + eye(ex, (0, 0), 1.25))
        g.append(f'<ellipse cx="0" cy="{36 * s:.1f}" rx="{15 * s:.1f}" ry="{20 * s:.1f}" fill="{ink}"/>')
        g.append(f'<ellipse cx="0" cy="{44 * s:.1f}" rx="{8 * s:.1f}" ry="{9 * s:.1f}" fill="#ff7a8a"/>')
    elif mood == "smug":
        for x in (-ex, ex):
            g.append(f'<path d="M{x - 20 * s:.1f} {ey + 2 * s:.1f} Q{x:.1f} {ey - 22 * s:.1f} {x + 20 * s:.1f} {ey + 2 * s:.1f}" fill="{white}" stroke="{ink}" stroke-width="{4 * s:.1f}"/>')
            g.append(f'<circle cx="{x + 5 * s:.1f}" cy="{ey - 3 * s:.1f}" r="{6 * s:.1f}" fill="{ink}"/>')
            g.append(f'<line x1="{x - 22 * s:.1f}" y1="{ey + 3 * s:.1f}" x2="{x + 22 * s:.1f}" y2="{ey + 3 * s:.1f}" stroke="{ink}" stroke-width="{5 * s:.1f}"/>')
        g.append(f'<path d="M{-28 * s:.1f} {34 * s:.1f} Q{4 * s:.1f} {46 * s:.1f} {32 * s:.1f} {24 * s:.1f}" fill="none" stroke="{ink}" stroke-width="{6 * s:.1f}"/>')
        g.append(f'<path d="M{-ex - 18 * s:.1f} {ey - 30 * s:.1f} L{-ex + 18 * s:.1f} {ey - 26 * s:.1f}" stroke="{ink}" stroke-width="{5 * s:.1f}" fill="none"/>')
    else:                                                     # happy
        g.append(eye(-ex) + eye(ex))
        g.append(f'<path d="M{-30 * s:.1f} {24 * s:.1f} Q0 {58 * s:.1f} {30 * s:.1f} {24 * s:.1f}" fill="#ff7a8a" stroke="{ink}" stroke-width="{5 * s:.1f}"/>')
        g.append(f'<circle cx="{-ex - 14 * s:.1f}" cy="{22 * s:.1f}" r="{9 * s:.1f}" fill="#ff9fb0" opacity=".7"/>')
        g.append(f'<circle cx="{ex + 14 * s:.1f}" cy="{22 * s:.1f}" r="{9 * s:.1f}" fill="#ff9fb0" opacity=".7"/>')
    g.append("</g>")
    return "".join(g)


def _stroke(width=6):
    return f'stroke="{INK}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"'


def laptop(mood, light):
    return (
        f'<rect x="22" y="236" width="356" height="40" rx="14" fill="#c9cde8" {_stroke()}/>'
        f'<rect x="160" y="236" width="80" height="12" rx="6" fill="#98a0cc"/>'
        f'<rect x="52" y="22" width="296" height="222" rx="22" fill="#2b2f5a" {_stroke()}/>'
        f'<rect x="72" y="42" width="256" height="182" rx="12" fill="{light}"/>'
        + face(200, 138, mood, 1.15)
    )


def router(mood, light):
    return (
        f'<line x1="108" y1="176" x2="70" y2="46" {_stroke(9)}/><circle cx="68" cy="40" r="13" fill="#ffd24a" {_stroke()}/>'
        f'<line x1="292" y1="176" x2="330" y2="46" {_stroke(9)}/><circle cx="332" cy="40" r="13" fill="#ffd24a" {_stroke()}/>'
        f'<path d="M352 12 q22 22 0 44 M368 0 q34 34 0 68" fill="none" stroke="#ffd24a" stroke-width="7" stroke-linecap="round"/>'
        f'<rect x="34" y="168" width="332" height="150" rx="30" fill="#3d4a8f" {_stroke()}/>'
        f'<rect x="58" y="190" width="284" height="96" rx="18" fill="{light}"/>'
        + face(200, 242, mood, 1.0)
        + '<circle cx="86" cy="302" r="7" fill="#46e08a"/><circle cx="112" cy="302" r="7" fill="#ffd24a"/><circle cx="138" cy="302" r="7" fill="#46e08a"/>'
    )


def server(mood, light):
    return (
        f'<rect x="104" y="14" width="192" height="326" rx="26" fill="#3d4a8f" {_stroke()}/>'
        f'<rect x="124" y="36" width="152" height="150" rx="16" fill="{light}"/>'
        + face(200, 112, mood, 1.0)
        + '<rect x="124" y="204" width="152" height="22" rx="8" fill="#2b2f5a"/><rect x="124" y="236" width="152" height="22" rx="8" fill="#2b2f5a"/>'
        '<rect x="124" y="268" width="152" height="22" rx="8" fill="#2b2f5a"/>'
        '<circle cx="256" cy="215" r="5" fill="#46e08a"/><circle cx="256" cy="247" r="5" fill="#ffd24a"/><circle cx="256" cy="279" r="5" fill="#46e08a"/>'
        f'<rect x="130" y="312" width="140" height="10" rx="5" fill="{light}"/>'
    )


def bug(mood, light):
    legs = "".join(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y + dy}" {_stroke(9)}/>'
                   for x1, x2, y, dy in ((98, 44, 196, -26), (92, 36, 244, 6), (104, 54, 290, 34), (302, 356, 196, -26), (308, 364, 244, 6), (296, 346, 290, 34)))
    return (
        legs
        + f'<line x1="170" y1="62" x2="140" y2="14" {_stroke()}/><circle cx="136" cy="10" r="10" fill="#ffd24a" {_stroke()}/>'
        f'<line x1="230" y1="62" x2="260" y2="14" {_stroke()}/><circle cx="264" cy="10" r="10" fill="#ffd24a" {_stroke()}/>'
        f'<ellipse cx="200" cy="228" rx="112" ry="104" fill="#ef5b3f" {_stroke()}/>'
        '<line x1="200" y1="126" x2="200" y2="330" stroke="#12163a" stroke-width="5" opacity=".5"/>'
        '<circle cx="150" cy="222" r="15" fill="#12163a" opacity=".85"/><circle cx="250" cy="250" r="17" fill="#12163a" opacity=".85"/><circle cx="170" cy="292" r="12" fill="#12163a" opacity=".85"/>'
        f'<circle cx="200" cy="112" r="66" fill="#ff8466" {_stroke()}/>'
        + face(200, 116, mood, 0.9)
    )


def padlock(mood, light):
    return (
        f'<path d="M128 156 V112 a72 72 0 0 1 144 0 V156" fill="none" stroke="{INK}" stroke-width="40" stroke-linecap="round"/>'
        '<path d="M128 156 V112 a72 72 0 0 1 144 0 V156" fill="none" stroke="#d7daf0" stroke-width="26" stroke-linecap="round"/>'
        f'<rect x="76" y="150" width="248" height="196" rx="34" fill="#f2b134" {_stroke()}/>'
        + face(200, 232, mood, 1.0, white="#ffffff")
        + f'<circle cx="200" cy="318" r="10" fill="{INK}"/><rect x="195" y="318" width="10" height="18" rx="4" fill="{INK}"/>'
    )


def cloud(mood, light):
    shapes = ('<circle cx="138" cy="196" r="72"/><circle cx="232" cy="136" r="88"/><circle cx="306" cy="206" r="66"/>'
              '<rect x="84" y="196" width="262" height="84" rx="42"/>')
    return (
        f'<g fill="#ffffff" {_stroke()}>{shapes}</g>'
        f'<g fill="#ffffff">{shapes}</g>'
        + face(214, 206, mood, 1.1)
        + f'<path d="M110 300 q-20 0 -20 20 M300 300 q20 0 20 20" fill="none" stroke="{light}" stroke-width="8" stroke-linecap="round"/>'
    )


def robot(mood, light):
    return (
        f'<line x1="200" y1="28" x2="200" y2="76" {_stroke(8)}/><circle cx="200" cy="22" r="14" fill="#ff5b6e" {_stroke()}/>'
        f'<rect x="64" y="128" width="30" height="76" rx="12" fill="#98a0cc" {_stroke()}/><rect x="306" y="128" width="30" height="76" rx="12" fill="#98a0cc" {_stroke()}/>'
        f'<rect x="86" y="70" width="228" height="186" rx="40" fill="#c9cde8" {_stroke()}/>'
        f'<rect x="108" y="96" width="184" height="124" rx="26" fill="#1e2354"/>'
        + face(200, 160, mood, 1.0, ink="#9cf0ff", white="#1e2354")
        + f'<rect x="128" y="262" width="144" height="84" rx="26" fill="#3d4a8f" {_stroke()}/>'
        f'<circle cx="170" cy="304" r="9" fill="#46e08a"/><circle cx="200" cy="304" r="9" fill="#ffd24a"/><circle cx="230" cy="304" r="9" fill="#ff5b6e"/>'
    )


def chip(mood, light):
    pins = "".join(f'<line x1="{x}" y1="40" x2="{x}" y2="62" {_stroke(11)}/><line x1="{x}" y1="298" x2="{x}" y2="320" {_stroke(11)}/>' for x in range(122, 290, 40))
    pins += "".join(f'<line x1="66" y1="{y}" x2="88" y2="{y}" {_stroke(11)}/><line x1="312" y1="{y}" x2="334" y2="{y}" {_stroke(11)}/>' for y in range(100, 270, 40))
    return (
        pins
        + f'<rect x="86" y="60" width="228" height="240" rx="28" fill="#1f8a63" {_stroke()}/>'
        f'<rect x="120" y="94" width="160" height="172" rx="16" fill="#1e2354"/>'
        + face(200, 168, mood, 0.95, ink="#9cf0ff", white="#1e2354")
        + '<text x="200" y="252" text-anchor="middle" font-family="Consolas, Menlo, monospace" font-size="22" font-weight="700" fill="#9cf0ff">CPU</text>'
    )


DRAW = {"laptop": laptop, "router": router, "server": server, "bug": bug, "padlock": padlock, "cloud": cloud, "robot": robot, "chip": chip}


def draw(character, mood, light="#8bd3ff"):
    """SVG fragment for a character in a 400 x 360 box."""
    return DRAW.get(character, laptop)(mood if mood in MOOD_KEYS else "happy", light)
