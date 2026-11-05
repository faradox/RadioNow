"""Generate app/templates/_scene.svg (mountains, lake, pagoda, radio).

Usage (from websites/radionow): python3 tools/scene.py > app/templates/_scene.svg
Colours come from CSS variables in app/static/landing.css.
"""
import math, random, sys

W, H, HORIZON = 1600, 1000, 540


def ridge(seed, base, amp, peaks, rough, step=8):
    """Ridge line y(x): sum of gaussian peaks + fractal noise."""
    rng = random.Random(seed)
    octaves = [(rng.uniform(0, 1000), f, a) for f, a in ((0.004, 1), (0.011, 0.45), (0.03, 0.18), (0.08, 0.06))]
    pts = []
    for x in range(-20, W + 21, step):
        y = base
        for px, ph, pw in peaks:
            y -= ph * math.exp(-((x - px) / pw) ** 2)
        n = sum(a * math.sin(x * f + o) * math.sin(x * f * 0.53 + o * 1.7) for o, f, a in octaves)
        y -= n * amp * rough
        pts.append((x, min(y, HORIZON + 2)))
    return pts


def path(pts, bottom=HORIZON + 2):
    d = f"M{pts[0][0]},{bottom} " + " ".join(f"L{x},{y:.1f}" for x, y in pts)
    return d + f" L{pts[-1][0]},{bottom} Z"


layers = [
    ("far", ridge(3, 410, 30, [(330, 210, 170), (610, 130, 120), (1010, 250, 160), (1330, 170, 150)], 1.0, 6)),
    ("mid", ridge(11, 480, 26, [(150, 190, 180), (430, 90, 140), (1180, 120, 130), (1480, 210, 170)], 1.0)),
    ("near", ridge(23, 530, 22, [(-40, 260, 260), (260, 120, 120), (1360, 140, 140), (1660, 280, 260)], 0.9)),
]

out = []
o = out.append
o(f'<svg class="scene" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid slice" aria-hidden="true">')
o("""<defs>
  <linearGradient id="g-sky" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" style="stop-color:var(--sky-top)"/>
    <stop offset="0.55" style="stop-color:var(--sky-mid)"/>
    <stop offset="1" style="stop-color:var(--sky-bottom)"/>
  </linearGradient>
  <linearGradient id="g-water" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" style="stop-color:var(--water-top)"/>
    <stop offset="1" style="stop-color:var(--water-bottom)"/>
  </linearGradient>
  <radialGradient id="g-sun">
    <stop offset="0" style="stop-color:var(--sun)" stop-opacity="1"/>
    <stop offset="0.25" style="stop-color:var(--sun)" stop-opacity="0.9"/>
    <stop offset="0.3" style="stop-color:var(--sun)" stop-opacity="0.25"/>
    <stop offset="1" style="stop-color:var(--sun)" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="g-glow">
    <stop offset="0" style="stop-color:var(--glow)" stop-opacity="0.9"/>
    <stop offset="1" style="stop-color:var(--glow)" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="g-mist" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" style="stop-color:var(--mist)" stop-opacity="0"/>
    <stop offset="0.7" style="stop-color:var(--mist)" stop-opacity="0.55"/>
    <stop offset="1" style="stop-color:var(--mist)" stop-opacity="0.2"/>
  </linearGradient>
  <linearGradient id="g-fade" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#fff" stop-opacity="0.75"/>
    <stop offset="0.35" stop-color="#fff" stop-opacity="0.25"/>
    <stop offset="1" stop-color="#fff" stop-opacity="0"/>
  </linearGradient>
  <mask id="m-reflect"><rect x="0" y="%d" width="%d" height="%d" fill="url(#g-fade)"/></mask>
  <filter id="f-ripple" x="0" y="0" width="1" height="1">
    <feTurbulence type="fractalNoise" baseFrequency="0.002 0.09" numOctaves="2" seed="4"/>
    <feDisplacementMap in="SourceGraphic" scale="14" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
  <filter id="f-soft"><feGaussianBlur stdDeviation="1.2"/></filter>
</defs>""" % (HORIZON, W, H - HORIZON))
o(f'<rect width="{W}" height="{HORIZON + 5}" fill="url(#g-sky)"/>')

# Stars (night only, via --stars opacity)
rng = random.Random(7)
stars = "".join(
    f'<circle cx="{rng.uniform(0, W):.0f}" cy="{rng.uniform(0, 420) ** 1.0:.0f}" r="{rng.choice((0.7, 0.9, 1.2, 1.6))}"/>'
    for _ in range(140)
)
o(f'<g class="stars" fill="#fff">{stars}</g>')
o('<circle class="sun" cx="1120" cy="215" r="170" fill="url(#g-sun)"/>')

# Clouds: soft long bands
o('<g class="clouds" fill="var(--cloud)">'
  '<ellipse cx="380" cy="190" rx="260" ry="9"/><ellipse cx="470" cy="204" rx="170" ry="6"/>'
  '<ellipse cx="1230" cy="150" rx="210" ry="7"/><ellipse cx="1320" cy="163" rx="120" ry="5"/>'
  '</g>')


def mountains():
    parts = []
    for name, pts in layers:
        parts.append(f'<path class="m-{name}" d="{path(pts)}" fill="var(--m-{name})"/>')
        if name == "far":
            # snow caps: light strokes near the top of the far peaks
            parts.append(f'<path d="{path(pts)}" fill="url(#g-mist)" opacity="0.6"/>')
        if name == "mid":
            parts.append(f'<rect y="{HORIZON - 120}" width="{W}" height="122" fill="url(#g-mist)" opacity="0.7"/>')
    # treeline on the near shores
    trees = []
    rng = random.Random(5)
    for x0, x1 in ((-20, 470), (1130, 1620)):
        x = x0
        while x < x1:
            h = rng.uniform(10, 26)
            w = h * 0.32
            trees.append(f"M{x:.1f},{HORIZON + 1} L{x + w:.1f},{HORIZON - h:.1f} L{x + 2 * w:.1f},{HORIZON + 1}Z")
            x += rng.uniform(5, 11)
    parts.append(f'<path d="{" ".join(trees)}" fill="var(--m-front)"/>')
    return "".join(parts)


def pagoda():
    """Pagoda on a small island, a cathedral radio underneath. Local coords:
    origin at the waterline centre; scaled into place."""
    return """
<path class="island" d="M-150,2 C-120,-10 -80,-14 -40,-13 L40,-13 C85,-14 125,-9 155,2 Z" fill="var(--m-front)"/>
<rect x="-78" y="-20" width="156" height="8" rx="1.5" fill="var(--wood)"/>
<g stroke="var(--wood)" stroke-width="4.2" stroke-linecap="round">
  <line x1="-62" y1="-20" x2="-62" y2="-86"/><line x1="62" y1="-20" x2="62" y2="-86"/>
  <line x1="-30" y1="-20" x2="-30" y2="-84"/><line x1="30" y1="-20" x2="30" y2="-84"/>
</g>
<path d="M-62,-80 L62,-80" stroke="var(--wood)" stroke-width="2.5"/>
<path class="roof" d="M-112,-90 C-90,-92 -70,-96 -52,-104 L-26,-122 L26,-122 L52,-104 C70,-96 90,-92 112,-90 C102,-84 92,-86 74,-86 L-74,-86 C-92,-86 -102,-84 -112,-90 Z" fill="var(--roof)"/>
<rect x="-30" y="-140" width="60" height="20" fill="var(--wood)"/>
<rect x="-22" y="-137" width="12" height="13" fill="var(--glow)" opacity="0.35" class="window"/>
<rect x="10" y="-137" width="12" height="13" fill="var(--glow)" opacity="0.35" class="window"/>
<path class="roof" d="M-66,-142 C-52,-144 -40,-149 -30,-156 L-12,-170 L12,-170 L30,-156 C40,-149 52,-144 66,-142 C58,-137 50,-138 40,-138 L-40,-138 C-50,-138 -58,-137 -66,-142 Z" fill="var(--roof)"/>
<path d="M0,-170 L0,-196" stroke="var(--roof)" stroke-width="2.5"/>
<circle cx="0" cy="-184" r="3" fill="var(--roof)"/><circle cx="0" cy="-192" r="2.2" fill="var(--roof)"/>
<circle class="lantern" cx="-96" cy="-84" r="3.2" fill="var(--glow)"/>
<circle class="lantern" cx="96" cy="-84" r="3.2" fill="var(--glow)"/>
<rect x="-26" y="-34" width="52" height="4" fill="var(--wood)"/>
<line x1="-22" y1="-30" x2="-22" y2="-20" stroke="var(--wood)" stroke-width="3"/>
<line x1="22" y1="-30" x2="22" y2="-20" stroke="var(--wood)" stroke-width="3"/>
<g class="radio">
  <circle class="radio-glow" cx="0" cy="-50" r="40" fill="url(#g-glow)"/>
  <path d="M-15,-34 L-15,-56 C-15,-68 15,-68 15,-56 L15,-34 Z" fill="var(--radio)"/>
  <path d="M-11,-38 L-11,-55 C-11,-63 11,-63 11,-55 L11,-38 Z" fill="var(--radio-face)"/>
  <g stroke="var(--radio)" stroke-width="0.9" opacity="0.8">
    <line x1="-6" y1="-60" x2="-6" y2="-48"/><line x1="-2" y1="-62" x2="-2" y2="-48"/>
    <line x1="2" y1="-62" x2="2" y2="-48"/><line x1="6" y1="-60" x2="6" y2="-48"/>
  </g>
  <rect class="dial" x="-8" y="-45" width="16" height="4" rx="1" fill="var(--glow)"/>
  <circle cx="-6" cy="-39.5" r="1.4" fill="var(--radio)"/><circle cx="6" cy="-39.5" r="1.4" fill="var(--radio)"/>
</g>"""


PX, PS = 800, 1.25  # pagoda x and scale
IY = HORIZON + 62   # island waterline

o(f'<g class="mountains">{mountains()}</g>')
o(f'<rect y="{HORIZON}" width="{W}" height="{H - HORIZON}" fill="url(#g-water)"/>')
# Reflection: mountains mirrored at the horizon, rippled and faded
o(f'<g class="reflection" mask="url(#m-reflect)" filter="url(#f-ripple)" opacity="0.55">'
  f'<g transform="translate(0,{2 * HORIZON}) scale(1,-1)">{mountains()}</g>'
  f'<g transform="translate({PX},{IY}) scale({PS},{-PS})" opacity="0.8">{pagoda()}</g></g>')
# Shimmer lines
rng = random.Random(9)
lines = "".join(
    f'<rect x="{rng.uniform(0, W):.0f}" y="{y:.0f}" width="{rng.uniform(20, 140):.0f}" height="1.2" rx="0.6"/>'
    for y in [HORIZON + 8 + (i ** 1.6) * 1.2 for i in range(60)]
)
o(f'<g class="shimmer" fill="var(--shimmer)">{lines}</g>')
# Ripples from the island when playing
o(f'<g class="ripples" fill="none" stroke="var(--shimmer)" stroke-width="1.4" transform="translate({PX},{IY + 2})">'
  + "".join(f'<ellipse class="ripple r{i}" rx="120" ry="14"/>' for i in range(3)) + "</g>")
o(f'<g class="pagoda" transform="translate({PX},{IY}) scale({PS})">{pagoda()}</g>')
o("</svg>")
sys.stdout.write("\n".join(out) + "\n")
