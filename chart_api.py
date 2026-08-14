#!/usr/bin/env python3
"""
Natal Chart API -- Flask backend
====================================================================
Requires:  pip install flask pyswisseph timezonefinder geopy
Run:       python3 chart_api.py
Then:      http://localhost:5000/api/chart?year=1990&month=6&day=15&hour=14&minute=30&city=New+York,+NY,+USA

This wraps the exact same calculation engine already verified in
natal_chart_prototype.py (sidereal Lahiri, Whole Sign houses, automatic
city/timezone lookup, North Indian chart layout) behind a web API, so a
website's JavaScript front-end can request a chart instead of someone
running the script by hand in Terminal.

Endpoints:
  GET  /api/chart     -- returns chart data as JSON (positions, houses, etc.)
  GET  /api/chart-svg -- returns the North Indian chart as an SVG image
  GET  /api/chart-pdf -- returns a full downloadable PDF report (intro
                          explainer, the chart, Placements table, Houses table)
  GET  /health        -- simple check that the server is running

This is a DEVELOPMENT server (Flask's built-in one). It is not meant to
be exposed to the public internet as-is -- see the bottom of this file
for notes on what changes before real deployment (e.g. to Render/Railway).
"""

import datetime as dt
import io
import swisseph as swe
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder
from geopy.geocoders import Nominatim
from flask import Flask, request, jsonify, Response

# ----------------------------------------------------------------------
# CONFIG -- identical to natal_chart_prototype.py
# ----------------------------------------------------------------------
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAG = swe.FLG_SIDEREAL | swe.FLG_MOSEPH | swe.FLG_SPEED

SIGNS = ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
         "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"]

BODIES = {
    "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY,
    "Venus": swe.VENUS, "Mars": swe.MARS, "Jupiter": swe.JUPITER,
    "Saturn": swe.SATURN, "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE,
    "Pluto": swe.PLUTO, "Rahu": swe.TRUE_NODE,
}

PLANET_ABBR = {
    "Sun": "Su", "Moon": "Mo", "Mercury": "Me", "Venus": "Ve", "Mars": "Ma",
    "Jupiter": "Ju", "Saturn": "Sa", "Uranus": "Ur", "Neptune": "Ne",
    "Pluto": "Pl", "Rahu": "Ra", "Ketu": "Ke", "Ascendant": "As",
}
DISPLAY_NAME = {"Rahu": "North Node", "Ketu": "South Node"}

# Classical Parashari graha drishti (whole-sign aspects). Every graha
# aspects the 7th house from itself (offset 6, 0-indexed) -- universal.
# These bodies also cast additional "special" aspects:
SPECIAL_ASPECT_OFFSETS = {
    "Mars": [3, 7],       # 4th and 8th from itself
    "Jupiter": [4, 8],    # 5th and 9th
    "Saturn": [2, 9],     # 3rd and 10th
    "Rahu": [4, 8],       # 5th and 9th (same convention as Jupiter)
    "Ketu": [4, 8],       # 5th and 9th (same convention as Jupiter)
}


def compute_vedic_aspects(planet_placements):
    """planet_placements: list of (name, house_number, degree) for every
    graha (NOT the Ascendant -- it doesn't cast aspects, it's just the
    House 1 reference point). Returns {house_number: [(name, abbr, degree), ...]}
    for every house that has at least one incoming aspect."""
    aspects_by_house = {h: [] for h in range(1, 13)}
    for name, house, degree in planet_placements:
        offsets = [6] + SPECIAL_ASPECT_OFFSETS.get(name, [])
        house_index = house - 1
        for off in offsets:
            target_house = ((house_index + off) % 12) + 1
            aspects_by_house[target_house].append(
                (name, PLANET_ABBR.get(name, name), degree)
            )
    return aspects_by_house


# ----------------------------------------------------------------------
# EPHEMERIS HELPERS -- identical logic to natal_chart_prototype.py
# ----------------------------------------------------------------------
def sidereal_lon_and_speed(jd, name):
    if name == "Ketu":
        lon, speed, retro = sidereal_lon_and_speed(jd, "Rahu")
        return (lon + 180.0) % 360.0, speed, retro
    res = swe.calc_ut(jd, BODIES[name], FLAG)[0]
    lon = res[0] % 360.0
    speed = res[3]
    return lon, speed, speed < 0


def sign_and_degree(lon):
    sign = int(lon // 30) % 12
    deg = lon - sign * 30.0
    return SIGNS[sign], deg


def whole_sign_house(planet_sign_index, ascendant_sign_index):
    return ((planet_sign_index - ascendant_sign_index) % 12) + 1


# ----------------------------------------------------------------------
# CORE CALCULATION -- takes parameters instead of reading module-level
# constants, since a web server handles many different requests, not
# just one hardcoded birth
# ----------------------------------------------------------------------
def compute_natal_chart(year, month, day, hour, minute, city):
    geolocator = Nominatim(user_agent="natal_chart_api")
    location = geolocator.geocode(city)
    if location is None:
        raise ValueError(f"Could not find coordinates for '{city}'")
    lat, lon = location.latitude, location.longitude

    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=lat, lng=lon)
    if tz_name is None:
        raise ValueError(f"Could not determine a timezone for ({lat}, {lon})")

    birth_dt_local = dt.datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz_name))
    birth_dt_utc = birth_dt_local.astimezone(dt.timezone.utc)
    utc_offset = birth_dt_local.utcoffset().total_seconds() / 3600.0

    jd = swe.julday(birth_dt_utc.year, birth_dt_utc.month, birth_dt_utc.day,
                     birth_dt_utc.hour + birth_dt_utc.minute / 60.0)

    cusps, ascmc = swe.houses_ex(jd, lat, lon, b'W', FLAG)
    asc_lon = ascmc[0] % 360.0
    asc_sign, asc_deg = sign_and_degree(asc_lon)
    asc_sign_index = SIGNS.index(asc_sign)

    planets = []
    for name in list(BODIES.keys()) + ["Ketu"]:
        p_lon, speed, retro = sidereal_lon_and_speed(jd, name)
        sign, deg = sign_and_degree(p_lon)
        sign_index = SIGNS.index(sign)
        house = whole_sign_house(sign_index, asc_sign_index)
        planets.append({
            "name": name,
            "display_name": DISPLAY_NAME.get(name, name),
            "abbr": PLANET_ABBR.get(name, name),
            "sign": sign,
            "degree": round(deg, 2),
            "house": house,
            "retrograde": retro,
        })

    graha_placements = [(p["name"], p["house"], p["degree"]) for p in planets]
    aspects_by_house = compute_vedic_aspects(graha_placements)

    by_house_bodies = {h: [] for h in range(1, 13)}
    by_house_bodies[1].append({"name": "Ascendant", "display_name": "Ascendant",
                                 "abbr": "As", "degree": round(asc_deg, 2)})
    for p in planets:
        by_house_bodies[p["house"]].append({
            "name": p["name"], "display_name": p["display_name"],
            "abbr": p["abbr"], "degree": p["degree"],
        })

    houses = []
    for h in range(1, 13):
        houses.append({
            "house": h,
            "sign": SIGNS[(asc_sign_index + h - 1) % 12],
            "bodies": by_house_bodies[h],
            "aspects": [
                {"name": name, "abbr": abbr, "degree": round(degree, 2)}
                for name, abbr, degree in aspects_by_house[h]
            ],
        })

    return {
        "birth": {
            "local_datetime": birth_dt_local.strftime("%Y-%m-%d %H:%M"),
            "utc_datetime": birth_dt_utc.strftime("%Y-%m-%d %H:%M"),
            "timezone": tz_name,
            "utc_offset_hours": round(utc_offset, 2),
            "city_input": city,
            "resolved_address": location.address,
            "latitude": lat,
            "longitude": lon,
        },
        "ascendant": {
            "sign": asc_sign,
            "degree": round(asc_deg, 2),
            "house": 1,
        },
        "planets": planets,
        "houses": houses,
        "ayanamsa": "Lahiri (sidereal)",
        "house_system": "Whole Sign",
    }


# ----------------------------------------------------------------------
# NORTH INDIAN CHART SVG -- identical logic to natal_chart_prototype.py
# ----------------------------------------------------------------------
def build_house_polygons(x0, y0, side):
    A = (x0, y0)
    B = (x0 + side, y0)
    C = (x0 + side, y0 + side)
    D = (x0, y0 + side)
    T = (x0 + side / 2, y0)
    R = (x0 + side, y0 + side / 2)
    Bo = (x0 + side / 2, y0 + side)
    L = (x0, y0 + side / 2)
    O = (x0 + side / 2, y0 + side / 2)
    X1 = (x0 + side / 4, y0 + side / 4)
    Y1 = (x0 + 3 * side / 4, y0 + side / 4)
    X2 = (x0 + 3 * side / 4, y0 + 3 * side / 4)
    Y2 = (x0 + side / 4, y0 + 3 * side / 4)
    return {
        1: [T, X1, O, Y1], 2: [A, T, X1], 3: [A, X1, L], 4: [L, X1, O, Y2],
        5: [D, L, Y2], 6: [D, Y2, Bo], 7: [Bo, X2, O, Y2], 8: [C, Bo, X2],
        9: [C, X2, R], 10: [R, Y1, O, X2], 11: [B, R, Y1], 12: [B, Y1, T],
    }


def polygon_centroid(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def polygon_bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def estimate_text_width(text, font_size, avg_char_ratio=0.56):
    return len(text) * font_size * avg_char_ratio


def fit_font_size(text, max_width, candidates=(36, 32, 28, 26, 24, 22, 20, 18, 16, 14, 12, 10, 8)):
    for size in candidates:
        if estimate_text_width(text, size) <= max_width:
            return size
    return candidates[-1]


def generate_north_indian_chart_svg(chart_data, canvas_size=900):
    asc_sign = chart_data["ascendant"]["sign"]
    margin = canvas_size * 0.08
    side = canvas_size - 2 * margin
    x0 = y0 = margin
    houses = build_house_polygons(x0, y0, side)

    by_house = {h: [] for h in range(1, 13)}
    by_house[1].append(("Ascendant", chart_data["ascendant"]["degree"]))
    graha_placements = []  # for aspect calc -- excludes Ascendant, it doesn't cast aspects
    for p in chart_data["planets"]:
        by_house[p["house"]].append((p["name"], p["degree"]))
        graha_placements.append((p["name"], p["house"], p["degree"]))

    aspects_by_house = compute_vedic_aspects(graha_placements)

    asc_index = SIGNS.index(asc_sign)
    house_sign = {h: SIGNS[(asc_index + h - 1) % 12] for h in range(1, 13)}

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_size}" '
        f'height="{canvas_size}" viewBox="0 0 {canvas_size} {canvas_size}">',
        f'<rect x="0" y="0" width="{canvas_size}" height="{canvas_size}" fill="white"/>',
    ]

    HOUSE_HEADER_GREEN = "#2E7D4F"

    for house_num, pts in houses.items():
        points_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        svg_lines.append(
            f'<polygon points="{points_str}" fill="none" '
            f'stroke="black" stroke-width="1.5"/>'
        )

        bbox_w = polygon_bbox(pts)[2] - polygon_bbox(pts)[0]
        bbox_h = polygon_bbox(pts)[3] - polygon_bbox(pts)[1]
        safe_width = min(bbox_w, bbox_h) * 0.62
        centroid = polygon_centroid(pts)

        header = f"House {house_num}: {house_sign[house_num]}"
        occupants = by_house[house_num]
        resident_labels = [f"{PLANET_ABBR.get(n, n)} {d:.1f}\u00b0" for n, d in occupants]

        aspecting = aspects_by_house[house_num]
        aspect_label = None
        if aspecting:
            abbrs = [a[1] for a in aspecting]
            aspect_label = "Asp: " + ", ".join(abbrs)

        black_lines = resident_labels + ([aspect_label] if aspect_label else [])
        all_lines_for_sizing = [header] + black_lines

        block_font = min(fit_font_size(ln, safe_width) for ln in all_lines_for_sizing)
        line_height = block_font * 1.2
        gap_units = 0.6  # extra vertical space (in line-heights) between the
                          # green header and the black resident/aspect section

        # Build a list of (text, style, vertical_offset_in_line_heights)
        items = [(header, "header", 0.0)]
        cursor = 1.0 + (gap_units if black_lines else 0.0)
        for i, ln in enumerate(resident_labels):
            items.append((ln, "resident", cursor))
            cursor += 1.0
        if aspect_label:
            items.append((aspect_label, "aspect", cursor))
            cursor += 1.0

        total_span = cursor - 1.0  # total vertical space used, in line-heights
        center_offset = total_span / 2.0
        base_y = centroid[1] - center_offset * line_height

        for text, kind, offset in items:
            if kind == "header":
                style_attr = f'fill="{HOUSE_HEADER_GREEN}"'
            elif kind == "resident":
                style_attr = 'font-weight="bold" fill="black"'
            else:  # aspect
                style_attr = 'fill="black"'
            svg_lines.append(
                f'<text x="{centroid[0]:.1f}" y="{base_y + offset * line_height:.1f}" '
                f'font-size="{block_font}" text-anchor="middle" '
                f'{style_attr} '
                f'font-family="sans-serif">{text}</text>'
            )

    svg_lines.append('</svg>')
    return "\n".join(svg_lines)


# ----------------------------------------------------------------------
# FULL PDF REPORT -- intro/explanation page, the chart itself, and both
# tables, all in one downloadable document.
# ----------------------------------------------------------------------
INTRO_SECTIONS = [
    ("What is this chart?",
     "This is your natal (birth) chart, calculated using the sidereal "
     "zodiac with the Lahiri ayanamsa -- the system used in traditional "
     "Vedic (Jyotish) astrology. It's a snapshot of exactly where the Sun, "
     "Moon, and every planet were positioned at the moment and place you "
     "were born."),
    ("The Ascendant (marked 'As')",
     "Your Ascendant is the zodiac sign that was rising on the eastern "
     "horizon at your exact birth time. It always defines House 1 -- "
     "everything else in the chart is read relative to it."),
    ("Houses",
     "The chart is divided into 12 houses, each representing a different "
     "area of life (self, money, communication, home, and so on). This "
     "chart uses the Whole Sign house system: House 1 is always your "
     "Ascendant's entire sign, and each house that follows is simply the "
     "next sign in zodiac order."),
    ("Reading the North Indian chart layout",
     "Unlike a circular chart, a North Indian style chart keeps the "
     "houses in fixed positions -- House 1 is always the top diamond, and "
     "the rest follow counter-clockwise around the square. The sign name "
     "shown in green inside each section tells you which zodiac sign "
     "occupies that house for you specifically."),
    ("Planets in bold black",
     "Any planet listed in bold black inside a house is physically placed "
     "in that house for your chart -- this is the most direct, primary "
     "influence in that area of life."),
    ("Aspects, in plain black",
     "Planets don't only affect the house they sit in -- they also cast "
     "influence ('aspects', or drishti) onto other houses. Every planet "
     "aspects the house directly opposite its own (the 7th house from "
     "itself). Mars, Jupiter, Saturn, and the lunar nodes (Rahu and Ketu) "
     "each cast two additional special aspects. These are listed in plain "
     "(non-bold) black text, prefixed 'Asp:', in whichever house receives "
     "them."),
    ("Rahu and Ketu (the lunar nodes)",
     "Rahu (the North Node) and Ketu (the South Node) aren't physical "
     "planets -- they're the two points where the Moon's orbital path "
     "crosses the Sun's. They're treated as full participants in Vedic "
     "astrology, always positioned exactly opposite one another."),
    ("Retrograde",
     "A planet marked RETROGRADE appeared to be moving backward through "
     "the zodiac from Earth's point of view at the moment of your birth "
     "-- a real, if temporary, optical effect of orbital mechanics, "
     "traditionally considered to change how that planet's energy "
     "expresses itself."),
]


def draw_chart_on_pdf_canvas(c, chart_data, x0, y0, side):
    """Draws the North Indian chart directly onto a reportlab canvas at
    the given position/size (reportlab's own bottom-up coordinate system).
    Mirrors generate_north_indian_chart_svg's logic exactly, just with
    reportlab drawing calls instead of building an SVG string."""
    houses = build_house_polygons(x0, y0, side)

    by_house = {h: [] for h in range(1, 13)}
    by_house[1].append(("Ascendant", chart_data["ascendant"]["degree"]))
    graha_placements = []
    for p in chart_data["planets"]:
        by_house[p["house"]].append((p["name"], p["degree"]))
        graha_placements.append((p["name"], p["house"], p["degree"]))
    aspects_by_house = compute_vedic_aspects(graha_placements)

    asc_sign = chart_data["ascendant"]["sign"]
    asc_index = SIGNS.index(asc_sign)
    house_sign = {h: SIGNS[(asc_index + h - 1) % 12] for h in range(1, 13)}

    def to_pdf_point(pt):
        # This function draws in a top-down coordinate space (y increases
        # downward, matching build_house_polygons' convention) -- this
        # converts a point into reportlab's actual bottom-up page space.
        x, y = pt
        return x, (y0 * 2 + side) - y  # mirror around the shape's own vertical center

    c.setLineWidth(1.2)
    for house_num, pts in houses.items():
        pdf_pts = [to_pdf_point(p) for p in pts]
        path = c.beginPath()
        path.moveTo(*pdf_pts[0])
        for p in pdf_pts[1:]:
            path.lineTo(*p)
        path.close()
        c.drawPath(path, stroke=1, fill=0)

        bbox_w = polygon_bbox(pts)[2] - polygon_bbox(pts)[0]
        bbox_h = polygon_bbox(pts)[3] - polygon_bbox(pts)[1]
        safe_width = min(bbox_w, bbox_h) * 0.62
        centroid = to_pdf_point(polygon_centroid(pts))

        header = f"House {house_num}: {house_sign[house_num]}"
        occupants = by_house[house_num]
        resident_labels = [f"{PLANET_ABBR.get(n, n)} {d:.1f}\u00b0" for n, d in occupants]
        aspecting = aspects_by_house[house_num]
        aspect_label = ("Asp: " + ", ".join(a[1] for a in aspecting)) if aspecting else None
        black_lines = resident_labels + ([aspect_label] if aspect_label else [])

        def fits(text, size, font):
            return c.stringWidth(text, font, size) <= safe_width

        def best_size(text, font):
            for size in (20, 18, 16, 14, 13, 12, 11, 10, 9, 8, 7, 6):
                if fits(text, size, font):
                    return size
            return 6

        block_font = min([best_size(header, "Helvetica")] +
                          [best_size(ln, "Helvetica-Bold") for ln in resident_labels] +
                          ([best_size(aspect_label, "Helvetica")] if aspect_label else []))
        line_height = block_font * 1.3
        gap_units = 0.6
        cursor = 1.0 + (gap_units if black_lines else 0.0)
        n_black = len(black_lines)
        total_span = (cursor - 1.0) + n_black
        center_offset = total_span / 2.0
        base_y = centroid[1] + center_offset * line_height  # reportlab y grows upward

        c.setFont("Helvetica", block_font)
        c.setFillColorRGB(0.18, 0.49, 0.31)  # green
        c.drawCentredString(centroid[0], base_y, header)
        c.setFillColorRGB(0, 0, 0)

        y_cursor = base_y - gap_units * line_height
        for ln in resident_labels:
            y_cursor -= line_height
            c.setFont("Helvetica-Bold", block_font)
            c.drawCentredString(centroid[0], y_cursor, ln)
        if aspect_label:
            y_cursor -= line_height
            c.setFont("Helvetica", block_font)
            c.drawCentredString(centroid[0], y_cursor, aspect_label)


def generate_full_chart_pdf(chart_data):
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.units import inch
    from reportlab.lib.pagesizes import letter

    buf = io.BytesIO()
    page_w, page_h = letter
    c = pdfcanvas.Canvas(buf, pagesize=letter)

    # -- Page 1: intro / how to read this chart --
    c.setFont("Helvetica-Bold", 20)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "Read This Before You Try to Interpret")
    c.setFont("Helvetica", 10)
    c.drawString(0.75 * inch, page_h - 1.15 * inch,
                 f"Your Vedic Birth Chart -- {chart_data['birth']['resolved_address']}")

    y = page_h - 1.6 * inch
    for heading, body in INTRO_SECTIONS:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(0.75 * inch, y, heading)
        y -= 0.22 * inch
        c.setFont("Helvetica", 10)
        # simple word-wrap to fit the page width
        words = body.split()
        line = ""
        max_width = page_w - 1.5 * inch
        for word in words:
            trial = (line + " " + word).strip()
            if c.stringWidth(trial, "Helvetica", 10) > max_width:
                c.drawString(0.75 * inch, y, line)
                y -= 0.18 * inch
                line = word
            else:
                line = trial
        if line:
            c.drawString(0.75 * inch, y, line)
            y -= 0.18 * inch
        y -= 0.18 * inch
        if y < 1 * inch:
            c.showPage()
            y = page_h - 0.9 * inch

    # -- Page 2: the chart itself --
    c.showPage()
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(page_w / 2, page_h - 0.7 * inch, "Your Vedic Birth Chart")
    c.setFont("Helvetica", 9)
    c.drawCentredString(page_w / 2, page_h - 0.9 * inch,
                         f"{chart_data['birth']['local_datetime']} ({chart_data['birth']['timezone']}) "
                         f"-- {chart_data['birth']['resolved_address']}")
    chart_side = 6.5 * inch
    chart_x0 = (page_w - chart_side) / 2
    chart_y0 = 1.1 * inch
    draw_chart_on_pdf_canvas(c, chart_data, chart_x0, chart_y0, chart_side)

    # -- Page 3: Placements table --
    c.showPage()
    c.setFont("Helvetica-Bold", 16)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "Placements")
    y = page_h - 1.3 * inch
    col_x = [0.75 * inch, 3.5 * inch, 4.8 * inch, 5.8 * inch]
    headers = ["Body", "Sign", "Degree", "House"]
    c.setFont("Helvetica-Bold", 9)
    for cx, h in zip(col_x, headers):
        c.drawString(cx, y, h)
    y -= 0.22 * inch
    c.setFont("Helvetica", 9)

    rows = [("Ascendant", "As", chart_data["ascendant"]["sign"],
              chart_data["ascendant"]["degree"], 1, False)]
    for p in chart_data["planets"]:
        rows.append((p["display_name"], p["abbr"], p["sign"], p["degree"], p["house"], p["retrograde"]))

    for name, abbr, sign, degree, house, retro in rows:
        label = f"{name} ({abbr})" + (" RETROGRADE" if retro else "")
        c.drawString(col_x[0], y, label)
        c.drawString(col_x[1], y, sign)
        c.drawString(col_x[2], y, f"{degree:.1f}\u00b0")
        c.drawString(col_x[3], y, f"House {house}")
        y -= 0.2 * inch
        if y < 0.75 * inch:
            c.showPage()
            y = page_h - 0.9 * inch
            c.setFont("Helvetica", 9)

    # -- Page 4: Houses table --
    c.showPage()
    c.setFont("Helvetica-Bold", 16)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "Houses")
    y = page_h - 1.3 * inch
    col_x2 = [0.75 * inch, 1.9 * inch, 3.0 * inch, 5.0 * inch]
    col_widths2 = [1.15 * inch, 1.1 * inch, 2.0 * inch, (page_w - 0.75 * inch) - 5.0 * inch]
    headers2 = ["House", "Sign", "Body", "Aspects"]
    c.setFont("Helvetica-Bold", 9)
    for cx, h in zip(col_x2, headers2):
        c.drawString(cx, y, h)
    y -= 0.22 * inch
    row_font_size = 8
    row_line_height = 0.16 * inch
    c.setFont("Helvetica", row_font_size)

    def wrap_cell_text(text, max_width, font_size):
        """Word-wraps text to fit max_width, returns a list of lines."""
        words = text.split(" ")
        lines = []
        line = ""
        for word in words:
            trial = (line + " " + word).strip()
            if c.stringWidth(trial, "Helvetica", font_size) > max_width and line:
                lines.append(line)
                line = word
            else:
                line = trial
        if line:
            lines.append(line)
        return lines or [""]

    for house_data in chart_data["houses"]:
        bodies_text = ", ".join(f"{b['abbr']} {b['degree']:.1f}\u00b0" for b in house_data["bodies"]) or "None"
        aspects_text = ", ".join(f"{a['abbr']} {a['degree']:.1f}\u00b0" for a in house_data["aspects"]) or "None"

        body_lines = wrap_cell_text(bodies_text, col_widths2[2] - 0.1 * inch, row_font_size)
        aspect_lines = wrap_cell_text(aspects_text, col_widths2[3] - 0.1 * inch, row_font_size)
        n_lines = max(len(body_lines), len(aspect_lines), 1)
        row_height = n_lines * row_line_height

        if y - row_height < 0.75 * inch:
            c.showPage()
            y = page_h - 0.9 * inch
            c.setFont("Helvetica", row_font_size)

        c.drawString(col_x2[0], y, f"House {house_data['house']}")
        c.drawString(col_x2[1], y, house_data["sign"])
        for i, ln in enumerate(body_lines):
            c.drawString(col_x2[2], y - i * row_line_height, ln)
        for i, ln in enumerate(aspect_lines):
            c.drawString(col_x2[3], y - i * row_line_height, ln)

        y -= row_height + 0.1 * inch

    c.save()
    buf.seek(0)
    return buf


# ----------------------------------------------------------------------
# FLASK APP
# ----------------------------------------------------------------------
app = Flask(__name__)

# Which website(s) are allowed to call this API from a browser. "*" (any
# site) is fine for local testing, but before this handles real traffic,
# change this to your actual Squarespace domain -- otherwise any website
# could embed and use your API, running up your geocoding usage and
# server load for free.
ALLOWED_ORIGIN = "*"


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def parse_birth_args(args):
    """Shared argument parsing/validation for both endpoints."""
    required = ["year", "month", "day", "hour", "minute", "city"]
    missing = [r for r in required if r not in args]
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}")
    return (
        int(args["year"]), int(args["month"]), int(args["day"]),
        int(args["hour"]), int(args["minute"]), args["city"],
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/chart")
def api_chart():
    try:
        year, month, day, hour, minute, city = parse_birth_args(request.args)
        chart_data = compute_natal_chart(year, month, day, hour, minute, city)
        return jsonify(chart_data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.route("/api/chart-svg")
def api_chart_svg():
    try:
        year, month, day, hour, minute, city = parse_birth_args(request.args)
        chart_data = compute_natal_chart(year, month, day, hour, minute, city)
        svg = generate_north_indian_chart_svg(chart_data)
        return Response(svg, mimetype="image/svg+xml")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.route("/api/chart-pdf")
def api_chart_pdf():
    try:
        year, month, day, hour, minute, city = parse_birth_args(request.args)
        chart_data = compute_natal_chart(year, month, day, hour, minute, city)
        pdf_buffer = generate_full_chart_pdf(chart_data)
        return Response(
            pdf_buffer.read(),
            mimetype="application/pdf",
            headers={"Content-Disposition": "attachment; filename=vedic_birth_chart.pdf"},
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


if __name__ == "__main__":
    import os
    # Hosting services (Render, Railway, etc.) assign a port dynamically
    # via the PORT environment variable -- they will NOT let you hardcode
    # 5000 in production. Locally, this still defaults to 5000 as before.
    port = int(os.environ.get("PORT", 5000))
    # debug=True (auto-reload, detailed error pages) is convenient locally
    # but must be off in production -- it can leak internal details to
    # anyone who hits an error. FLASK_DEBUG=1 turns it on explicitly when
    # you want it (e.g. while developing locally).
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
