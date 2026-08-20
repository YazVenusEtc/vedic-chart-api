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
  GET  /api/chart-now     -- like /api/chart, but for the current moment at
                              a given lat/lon (no birth info -- takes ?lat=&lon=)
  GET  /api/chart-now-svg -- like /api/chart-svg, but for the current moment
  GET  /api/chart-now-pdf -- like /api/chart-pdf, but for the current moment
  GET  /health        -- simple check that the server is running

This is a DEVELOPMENT server (Flask's built-in one). It is not meant to
be exposed to the public internet as-is -- see the bottom of this file
for notes on what changes before real deployment (e.g. to Render/Railway).
"""

import datetime as dt
import io
import os
import swisseph as swe
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder
from geopy.geocoders import Nominatim
from flask import Flask, request, jsonify, Response
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# The chart diagram itself (house numbers, planet abbreviations) uses
# Poppins -- a relaxed, friendly geometric sans-serif, distinct from the
# rest of the document's more formal Times family. Registration is
# wrapped in a try/except so a missing font file degrades gracefully to
# Times rather than crashing the whole app (e.g. if the fonts/ folder
# wasn't deployed alongside this file for some reason).
_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
CHART_FONT_REGULAR = "Times-Roman"
CHART_FONT_BOLD = "Times-Bold"
try:
    pdfmetrics.registerFont(TTFont("Poppins", os.path.join(_FONTS_DIR, "Poppins-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Poppins-Bold", os.path.join(_FONTS_DIR, "Poppins-Bold.ttf")))
    CHART_FONT_REGULAR = "Poppins"
    CHART_FONT_BOLD = "Poppins-Bold"
except Exception:
    pass

# ----------------------------------------------------------------------
# CONFIG -- identical to natal_chart_prototype.py
# ----------------------------------------------------------------------
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAG = swe.FLG_SIDEREAL | swe.FLG_MOSEPH | swe.FLG_SPEED

SIGNS = ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
         "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"]

# Traditional (not modern/outer-planet) sign rulerships -- the classical
# 7-planet scheme used in Vedic astrology. Rahu and Ketu are lunar nodes,
# not physical planets, and don't rule any sign in this system.
SIGN_RULER = {
    "Aries": "Mars", "Taurus": "Venus", "Gemini": "Mercury", "Cancer": "Moon",
    "Leo": "Sun", "Virgo": "Mercury", "Libra": "Venus", "Scorpio": "Mars",
    "Sagittarius": "Jupiter", "Capricorn": "Saturn", "Aquarius": "Saturn", "Pisces": "Jupiter",
}

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
# CLASSICAL PLANET-TO-PLANET ASPECTS (conjunction, sextile, square, trine,
# opposition) -- degree-based angular relationships between two bodies'
# actual positions, as distinct from the whole-sign house-based Vedic
# drishti aspects above. A single, uniform orb is used for all five
# aspect types, which keeps this simple and predictable rather than
# trying to replicate every tradition's own variable per-planet orbs.
# ----------------------------------------------------------------------
ASPECT_ANGLE_DEFINITIONS = [
    ("Conjunction", 0),
    ("Sextile", 60),
    ("Square", 90),
    ("Trine", 120),
    ("Opposition", 180),
]
ASPECT_ANGLE_ORB = 6.0  # degrees of allowed deviation from an exact angle


def compute_planetary_aspects(bodies):
    """bodies: list of (display_name, abbr, longitude) tuples -- every
    body to check pairwise against every other (typically the Ascendant
    plus all planets). Returns a list of dicts, one per aspect actually
    found (i.e. within orb), each noting the two bodies, the aspect type,
    and how many degrees off from exact ("orb") it is."""
    results = []
    n = len(bodies)
    for i in range(n):
        for j in range(i + 1, n):
            name1, abbr1, lon1 = bodies[i]
            name2, abbr2, lon2 = bodies[j]
            diff = abs(lon1 - lon2) % 360
            if diff > 180:
                diff = 360 - diff
            for aspect_name, exact_deg in ASPECT_ANGLE_DEFINITIONS:
                orb = abs(diff - exact_deg)
                if orb <= ASPECT_ANGLE_ORB:
                    results.append({
                        "body1": name1, "abbr1": abbr1,
                        "body2": name2, "abbr2": abbr2,
                        "aspect": aspect_name,
                        "orb": round(orb, 2),
                    })
    return results


# ----------------------------------------------------------------------
# VIMSHOTTARI DASHA -- the classical Vedic timing system, based on which
# Nakshatra (lunar mansion) the Moon occupied at birth. A fixed 120-year
# cycle is divided among 9 planetary lords in a fixed order; each
# Mahadasha (main period) is itself subdivided into 9 Antardashas (sub
# periods) in the same order, starting from that Mahadasha's own lord.
# ----------------------------------------------------------------------
NAKSHATRA_SPAN = 360.0 / 27  # each nakshatra spans 13 deg 20 min
DASHA_YEARS = {
    "Ketu": 7, "Venus": 20, "Sun": 6, "Moon": 10, "Mars": 7,
    "Rahu": 18, "Jupiter": 16, "Saturn": 19, "Mercury": 17,
}
DASHA_ORDER = ["Ketu", "Venus", "Sun", "Moon", "Mars", "Rahu", "Jupiter", "Saturn", "Mercury"]
DASHA_YEAR_DAYS = 365.25  # standard convention for a "dasha year" in this system


def _generate_mahadashas(moon_longitude, birth_dt_utc, cycles=4):
    """Returns a list of (lord, start_dt, end_dt, full_years) mahadasha
    periods, starting with the (partial) one active at birth and
    continuing for several full 120-year cycles -- comfortably more than
    a lifetime, so any reasonable evaluation date safely falls inside it."""
    nak_index = int(moon_longitude // NAKSHATRA_SPAN) % 27
    frac_elapsed = (moon_longitude % NAKSHATRA_SPAN) / NAKSHATRA_SPAN
    start_lord_index = nak_index % 9

    periods = []
    first_lord = DASHA_ORDER[start_lord_index]
    first_full_years = DASHA_YEARS[first_lord]
    first_balance_years = first_full_years * (1 - frac_elapsed)
    cursor = birth_dt_utc
    first_end = cursor + dt.timedelta(days=first_balance_years * DASHA_YEAR_DAYS)
    periods.append((first_lord, cursor, first_end, first_full_years))
    cursor = first_end

    lord_idx = start_lord_index
    for _ in range(9 * cycles):
        lord_idx = (lord_idx + 1) % 9
        lord = DASHA_ORDER[lord_idx]
        full_years = DASHA_YEARS[lord]
        end = cursor + dt.timedelta(days=full_years * DASHA_YEAR_DAYS)
        periods.append((lord, cursor, end, full_years))
        cursor = end
    return periods


def _generate_antardashas(mahadasha_lord, mahadasha_start, mahadasha_full_years):
    """Returns the 9 Antardasha (sub-period) sub-divisions of a single
    Mahadasha, each proportional to (sub-lord's own years / 120), in the
    fixed dasha order starting from the Mahadasha's own lord."""
    start_idx = DASHA_ORDER.index(mahadasha_lord)
    periods = []
    cursor = mahadasha_start
    for i in range(9):
        sub_lord = DASHA_ORDER[(start_idx + i) % 9]
        sub_years = mahadasha_full_years * (DASHA_YEARS[sub_lord] / 120.0)
        sub_end = cursor + dt.timedelta(days=sub_years * DASHA_YEAR_DAYS)
        periods.append((sub_lord, cursor, sub_end))
        cursor = sub_end
    return periods


def compute_vimshottari_dashas(moon_longitude, birth_dt_utc, evaluation_dt_utc,
                                planet_house, planet_longitude, houses):
    """Returns {'previous': {...}, 'current': {...}, 'next': {...}}, each
    with the Antardasha (and its parent Mahadasha) active at that point in
    the sequence, relative to evaluation_dt_utc -- 'current' is whichever
    Antardasha evaluation_dt_utc actually falls inside.

    Each period is enriched with reading context: which house each dasha
    planet is placed in, the angular relationship (aspect) between the two
    dasha planets, and which houses each dasha planet rules (by sign)."""
    mahadashas = _generate_mahadashas(moon_longitude, birth_dt_utc)

    all_antardashas = []
    for lord, start, end, full_years in mahadashas:
        for sub_lord, sub_start, sub_end in _generate_antardashas(lord, start, full_years):
            all_antardashas.append({
                "mahadasha_lord": lord, "antardasha_lord": sub_lord,
                "start": sub_start, "end": sub_end,
            })

    current_idx = None
    for i, period in enumerate(all_antardashas):
        if period["start"] <= evaluation_dt_utc < period["end"]:
            current_idx = i
            break
    if current_idx is None:
        current_idx = 0

    def houses_ruled_by(planet_name):
        return [h["house"] for h in houses if SIGN_RULER.get(h["sign"]) == planet_name]

    def angular_relationship(name1, name2):
        lon1, lon2 = planet_longitude.get(name1), planet_longitude.get(name2)
        if lon1 is None or lon2 is None:
            return "Unknown"
        diff = abs(lon1 - lon2) % 360
        if diff > 180:
            diff = 360 - diff
        for aspect_name, exact_deg in ASPECT_ANGLE_DEFINITIONS:
            if abs(diff - exact_deg) <= ASPECT_ANGLE_ORB:
                return aspect_name
        return "No major aspect"

    def describe(i):
        if i < 0 or i >= len(all_antardashas):
            return None
        p = all_antardashas[i]
        maha, antar = p["mahadasha_lord"], p["antardasha_lord"]
        return {
            "mahadasha_lord": maha,
            "mahadasha_display_name": DISPLAY_NAME.get(maha, maha),
            "mahadasha_house": planet_house.get(maha),
            "mahadasha_rules_houses": houses_ruled_by(maha),
            "antardasha_lord": antar,
            "antardasha_display_name": DISPLAY_NAME.get(antar, antar),
            "antardasha_house": planet_house.get(antar),
            "antardasha_rules_houses": houses_ruled_by(antar),
            "angular_relationship": angular_relationship(maha, antar),
            "start": p["start"].strftime("%Y-%m-%d"),
            "end": p["end"].strftime("%Y-%m-%d"),
        }

    return {
        "previous": describe(current_idx - 1),
        "current": describe(current_idx),
        "next": describe(current_idx + 1),
    }


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
def compute_chart_from_coords(year, month, day, hour, minute, lat, lon, location_label):
    """The actual math, given a moment (local date/time, resolved via
    timezone lookup from the coordinates) and a location already reduced
    to lat/lon. Used both by the city-based birth chart flow (after
    geocoding) and the "chart for this moment" flow (which already has
    coordinates from the browser's own geolocation, so it skips geocoding
    entirely)."""
    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=lat, lng=lon)
    if tz_name is None:
        raise ValueError(f"Could not determine a timezone for ({lat}, {lon})")

    local_dt = dt.datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz_name))
    utc_dt = local_dt.astimezone(dt.timezone.utc)
    utc_offset = local_dt.utcoffset().total_seconds() / 3600.0

    jd = swe.julday(utc_dt.year, utc_dt.month, utc_dt.day,
                     utc_dt.hour + utc_dt.minute / 60.0)

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
            "longitude": round(p_lon, 4),
            "house": house,
            "retrograde": retro,
            "meaning": PLANET_MEANINGS.get(name, ""),
        })

    graha_placements = [(p["name"], p["house"], p["degree"]) for p in planets]
    aspects_by_house = compute_vedic_aspects(graha_placements)

    aspect_bodies = [("Ascendant", "As", asc_lon)] + [
        (p["display_name"], p["abbr"], p["longitude"]) for p in planets
    ]
    planetary_aspects = compute_planetary_aspects(aspect_bodies)

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
        sign = SIGNS[(asc_sign_index + h - 1) % 12]
        houses.append({
            "house": h,
            "sign": sign,
            "ruler": SIGN_RULER.get(sign),
            "ruler_display_name": DISPLAY_NAME.get(SIGN_RULER.get(sign), SIGN_RULER.get(sign)),
            "bodies": by_house_bodies[h],
            "aspects": [
                {"name": name, "display_name": DISPLAY_NAME.get(name, name),
                 "abbr": abbr, "degree": round(degree, 2)}
                for name, abbr, degree in aspects_by_house[h]
            ],
        })

    moon_longitude = next(p["longitude"] for p in planets if p["name"] == "Moon")
    planet_house = {p["name"]: p["house"] for p in planets}
    planet_longitude = {p["name"]: p["longitude"] for p in planets}
    dashas = compute_vimshottari_dashas(
        moon_longitude, utc_dt, dt.datetime.now(dt.timezone.utc),
        planet_house, planet_longitude, houses,
    )

    return {
        "birth": {
            "local_datetime": local_dt.strftime("%Y-%m-%d %H:%M"),
            "utc_datetime": utc_dt.strftime("%Y-%m-%d %H:%M"),
            "timezone": tz_name,
            "utc_offset_hours": round(utc_offset, 2),
            "city_input": location_label,
            "resolved_address": location_label,
            "latitude": lat,
            "longitude": lon,
        },
        "ascendant": {
            "sign": asc_sign,
            "degree": round(asc_deg, 2),
            "house": 1,
            "meaning": PLANET_MEANINGS.get("Ascendant", ""),
        },
        "planets": planets,
        "houses": houses,
        "planetary_aspects": planetary_aspects,
        "dashas": dashas,
        "ayanamsa": "Lahiri (sidereal)",
        "house_system": "Whole Sign",
    }


def compute_natal_chart(year, month, day, hour, minute, city):
    geolocator = Nominatim(user_agent="natal_chart_api")
    location = geolocator.geocode(city)
    if location is None:
        raise ValueError(f"Could not find coordinates for '{city}'")
    return compute_chart_from_coords(
        year, month, day, hour, minute,
        location.latitude, location.longitude, location.address,
    )


# ----------------------------------------------------------------------
# NORTH INDIAN CHART SVG
# ----------------------------------------------------------------------
def build_house_polygons(x0, y0, width, height):
    """Generalized to a rectangle (width may differ from height) rather
    than forcing a square -- a wider rectangle gives the narrow corner
    triangles genuinely more horizontal room for text, which a square
    (the traditional proportions) doesn't allow."""
    A = (x0, y0)
    B = (x0 + width, y0)
    C = (x0 + width, y0 + height)
    D = (x0, y0 + height)
    T = (x0 + width / 2, y0)
    R = (x0 + width, y0 + height / 2)
    Bo = (x0 + width / 2, y0 + height)
    L = (x0, y0 + height / 2)
    O = (x0 + width / 2, y0 + height / 2)
    X1 = (x0 + width / 4, y0 + height / 4)
    Y1 = (x0 + 3 * width / 4, y0 + height / 4)
    X2 = (x0 + 3 * width / 4, y0 + 3 * height / 4)
    Y2 = (x0 + width / 4, y0 + 3 * height / 4)
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


def wrap_line_to_width(text, max_width, font_size, avg_char_ratio=0.56):
    """Word-wraps a comma-joined list (like an aspect list) onto as many
    lines as needed to fit max_width at the given font size, breaking at
    ', ' boundaries rather than mid-item. Returns a list of lines."""
    items = text.split(", ")
    lines = []
    current = ""
    for item in items:
        trial = (current + ", " + item) if current else item
        if estimate_text_width(trial, font_size, avg_char_ratio) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = item
    if current:
        lines.append(current)
    return lines


def wrap_colored_items(items, max_width, font_size, avg_char_ratio=0.56):
    """Like wrap_line_to_width, but for a list of (label, color) tuples --
    e.g. several differently-colored planets that need to share a line
    when there's room. Returns a list of lines, each line itself a list
    of (label, color) tuples that belong on it together."""
    lines = []
    current = []
    current_width = 0.0
    sep_width = estimate_text_width(", ", font_size, avg_char_ratio)
    for label, color in items:
        item_width = estimate_text_width(label, font_size, avg_char_ratio)
        added_width = (sep_width if current else 0) + item_width
        if current and (current_width + added_width) > max_width:
            lines.append(current)
            current = [(label, color)]
            current_width = item_width
        else:
            current.append((label, color))
            current_width += added_width
    if current:
        lines.append(current)
    return lines


def colored_line_width(line_items, font_size, avg_char_ratio=0.56):
    """Total rendered width of a wrapped line built by wrap_colored_items."""
    sep_width = estimate_text_width(", ", font_size, avg_char_ratio)
    total = sum(estimate_text_width(label, font_size, avg_char_ratio) for label, _ in line_items)
    total += sep_width * max(len(line_items) - 1, 0)
    return total


PLANET_COLORS = {
    "Mars": "#A63030", "Sun": "#8F5614", "Rahu": "#6F6220", "Mercury": "#26734D",
    "Ketu": "#206F6C", "Uranus": "#276D86", "Moon": "#54667D", "Saturn": "#2E366B",
    "Neptune": "#6651B8", "Jupiter": "#79469B", "Venus": "#A63A7F", "Pluto": "#822B3D",
    "Ascendant": "#1A1A1A",
}
CHART_LINE_COLOR = "#C4A876"   # muted gold/tan
CHART_NUMBER_COLOR = "#C4A876"  # same tone, slightly different weight in use


def _house_text_content(chart_data):
    """Shared between the SVG and PDF chart drawers: for every house,
    returns (sign_number, list of (label, color) for each resident body,
    each label including that body's exact degree). sign_number is the
    body's fixed position in the zodiac (1=Aries...12=Pisces) -- not the
    house number relative to the Ascendant -- so it reads the same way
    for everyone; a universal Key (not a chart-specific one) explains it."""
    asc_sign = chart_data["ascendant"]["sign"]
    asc_index = SIGNS.index(asc_sign)
    house_sign = {h: SIGNS[(asc_index + h - 1) % 12] for h in range(1, 13)}

    by_house = {h: [] for h in range(1, 13)}
    by_house[1].append((f"As {chart_data['ascendant']['degree']:.1f}\u00b0", PLANET_COLORS["Ascendant"]))
    for p in chart_data["planets"]:
        abbr = PLANET_ABBR.get(p["name"], p["name"])
        color = PLANET_COLORS.get(p["name"], "#333333")
        label = f"{abbr} {p['degree']:.1f}\u00b0"
        by_house[p["house"]].append((label, color))

    content = {}
    for house_num in range(1, 13):
        sign_number = SIGNS.index(house_sign[house_num]) + 1
        content[house_num] = (sign_number, by_house[house_num])
    return content


def generate_north_indian_chart_svg(chart_data, canvas_width=1200, canvas_height=1000):
    margin_x = canvas_width * 0.07
    margin_y = canvas_height * 0.10
    width = canvas_width - 2 * margin_x
    height = canvas_height - 2 * margin_y
    x0, y0 = margin_x, margin_y
    houses = build_house_polygons(x0, y0, width, height)
    content = _house_text_content(chart_data)
    center_x, center_y = x0 + width / 2, y0 + height / 2

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_width}" '
        f'height="{canvas_height}" viewBox="0 0 {canvas_width} {canvas_height}">',
        '<defs>',
        '<radialGradient id="vbcCenterGlow" cx="50%" cy="50%" r="50%">',
        '<stop offset="0%" stop-color="#E8ECFB" stop-opacity="0.9"/>',
        '<stop offset="60%" stop-color="#EEF1FC" stop-opacity="0.5"/>',
        '<stop offset="100%" stop-color="#F5F7FD" stop-opacity="0"/>',
        '</radialGradient>',
        '</defs>',
        f'<rect x="0" y="0" width="{canvas_width}" height="{canvas_height}" fill="white"/>',
        f'<text x="{canvas_width/2}" y="{margin_y * 0.5}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="15" letter-spacing="3" fill="#9C9488">'
        f'WHOLE SIGN HOUSES</text>',
    ]

    glow_r = min(width, height) * 0.16
    svg_lines.append(f'<circle cx="{center_x}" cy="{center_y}" r="{glow_r}" fill="url(#vbcCenterGlow)"/>')

    NUMBER_FONT_RATIO = 0.75  # sign number renders a bit smaller than the body text
    CONTENT_CANDIDATES = [40, 36, 32, 28, 26, 24, 22, 20, 18, 16, 14, 12, 10, 8]

    per_house_geometry = {}
    for house_num, pts in houses.items():
        bbox = polygon_bbox(pts)
        bbox_w, bbox_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        wrap_width = bbox_w * 0.80
        safe_height = bbox_h * 0.72
        per_house_geometry[house_num] = (wrap_width, safe_height, polygon_centroid(pts))

    def fits_canvas(centroid_x, text, font_size):
        half = estimate_text_width(text, font_size) / 2.0
        return (centroid_x - half) >= margin_x * 0.4 and (centroid_x + half) <= (canvas_width - margin_x * 0.4)

    def layout_fits(body_font):
        number_font = max(int(body_font * NUMBER_FONT_RATIO), 8)
        number_line_height = number_font * 1.2
        body_line_height = body_font * 1.25
        layout = {}
        for house_num, pts in houses.items():
            wrap_width, safe_height, centroid = per_house_geometry[house_num]
            sign_number, bodies = content[house_num]
            number_text = str(sign_number)

            if not fits_canvas(centroid[0], number_text, number_font):
                return None

            wrapped_lines = wrap_colored_items(bodies, wrap_width, body_font) if bodies else []
            for line_items in wrapped_lines:
                combined = ", ".join(lbl for lbl, _ in line_items)
                if not fits_canvas(centroid[0], combined, body_font):
                    return None

            total_height = number_line_height + (0.3 * body_line_height if wrapped_lines else 0) \
                + len(wrapped_lines) * body_line_height
            if total_height > safe_height:
                return None

            layout[house_num] = (number_text, wrapped_lines)
        return layout, number_font, body_font

    result = None
    for candidate in CONTENT_CANDIDATES:
        result = layout_fits(candidate)
        if result is not None:
            break
    if result is None:
        smallest = CONTENT_CANDIDATES[-1]
        layout = {h: (str(content[h][0]), [[item] for item in content[h][1]]) for h in range(1, 13)}
        result = (layout, max(int(smallest * NUMBER_FONT_RATIO), 8), smallest)
    layout, number_font, body_font = result
    number_line_height = number_font * 1.2
    body_line_height = body_font * 1.25

    for house_num, pts in houses.items():
        points_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        svg_lines.append(
            f'<polygon points="{points_str}" fill="none" '
            f'stroke="{CHART_LINE_COLOR}" stroke-width="1.3"/>'
        )

        _, _, centroid = per_house_geometry[house_num]
        number_text, wrapped_lines = layout[house_num]

        cursor = 0.0
        number_local_y = cursor + number_line_height * 0.8
        cursor += number_line_height
        if wrapped_lines:
            cursor += 0.3 * body_line_height
        line_positions = []
        for line_items in wrapped_lines:
            line_positions.append((line_items, cursor + body_line_height * 0.8))
            cursor += body_line_height

        top_y = centroid[1] - cursor / 2.0

        svg_lines.append(
            f'<text x="{centroid[0]:.1f}" y="{top_y + number_local_y:.1f}" font-size="{number_font}" '
            f'text-anchor="middle" fill="{CHART_NUMBER_COLOR}" '
            f'font-family="Poppins, sans-serif">{number_text}</text>'
        )

        for line_items, local_y in line_positions:
            line_width = colored_line_width(line_items, body_font)
            start_x = centroid[0] - line_width / 2.0
            tspans = []
            cx = start_x
            for i, (label, color) in enumerate(line_items):
                prefix = ", " if i > 0 else ""
                tspans.append(f'<tspan fill="{color}">{prefix}{label}</tspan>')
            svg_lines.append(
                f'<text x="{start_x:.1f}" y="{top_y + local_y:.1f}" font-size="{body_font}" '
                f'text-anchor="start" font-family="Poppins, sans-serif">{"".join(tspans)}</text>'
            )

    svg_lines.append('</svg>')
    return "\n".join(svg_lines)
def draw_chart_on_pdf_canvas(c, chart_data, x0, y0, width, height):
    """Draws the North Indian chart directly onto a reportlab canvas at
    the given position/size (reportlab's own bottom-up coordinate system).
    Matches generate_north_indian_chart_svg's new minimal style: thin gold
    lines, small house numbers offset toward each house's outer corner,
    and colored planet abbreviations stacked at the centroid -- no signs,
    no degrees, no aspects shown inline anymore."""
    houses = build_house_polygons(x0, y0, width, height)
    content = _house_text_content(chart_data)
    center_x, center_y = x0 + width / 2, y0 + height / 2

    def to_pdf_point(pt):
        px, py = pt
        return px, (y0 * 2 + height) - py

    # Soft decorative glow at the center -- reportlab has no native radial
    # gradient, so this approximates one with a few concentric,
    # increasingly transparent circles.
    glow_r = min(width, height) * 0.16
    glow_pdf_center = to_pdf_point((center_x, center_y))
    for frac, alpha in [(1.0, 0.10), (0.7, 0.16), (0.4, 0.22)]:
        c.saveState()
        c.setFillColorRGB(0.91, 0.93, 0.99, alpha=alpha)
        c.circle(glow_pdf_center[0], glow_pdf_center[1], glow_r * frac, stroke=0, fill=1)
        c.restoreState()

    NUMBER_FONT_RATIO = 0.75
    CONTENT_CANDIDATES = [40, 36, 32, 28, 26, 24, 22, 20, 18, 16, 14, 13, 12, 11, 10, 9, 8, 7, 6]

    per_house_geometry = {}
    for house_num, pts in houses.items():
        bbox = polygon_bbox(pts)
        bbox_w, bbox_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        wrap_width = bbox_w * 0.80
        safe_height = bbox_h * 0.72
        per_house_geometry[house_num] = (wrap_width, safe_height, to_pdf_point(polygon_centroid(pts)))

    def fits_canvas_pdf(centroid_x, text, font, size):
        half = c.stringWidth(text, font, size) / 2.0
        buffer = width * 0.015
        return (centroid_x - half) >= (x0 + buffer) and (centroid_x + half) <= (x0 + width - buffer)

    def wrap_colored_pdf(items, max_width, font, size):
        lines, current, current_width = [], [], 0.0
        sep_width = c.stringWidth(", ", font, size)
        for label, color in items:
            item_width = c.stringWidth(label, font, size)
            added = (sep_width if current else 0) + item_width
            if current and (current_width + added) > max_width:
                lines.append(current)
                current, current_width = [(label, color)], item_width
            else:
                current.append((label, color))
                current_width += added
        if current:
            lines.append(current)
        return lines

    def colored_line_width_pdf(line_items, font, size):
        sep_width = c.stringWidth(", ", font, size)
        total = sum(c.stringWidth(lbl, font, size) for lbl, _ in line_items)
        return total + sep_width * max(len(line_items) - 1, 0)

    def layout_fits(body_font):
        number_font = max(int(body_font * NUMBER_FONT_RATIO), 6)
        number_line_height = number_font * 1.2
        body_line_height = body_font * 1.25
        layout = {}
        for house_num, pts in houses.items():
            wrap_width, safe_height, centroid = per_house_geometry[house_num]
            sign_number, bodies = content[house_num]
            number_text = str(sign_number)

            if not fits_canvas_pdf(centroid[0], number_text, CHART_FONT_REGULAR, number_font):
                return None

            wrapped_lines = wrap_colored_pdf(bodies, wrap_width, CHART_FONT_REGULAR, body_font) if bodies else []
            for line_items in wrapped_lines:
                combined = ", ".join(lbl for lbl, _ in line_items)
                if not fits_canvas_pdf(centroid[0], combined, CHART_FONT_REGULAR, body_font):
                    return None

            total_height = number_line_height + (0.3 * body_line_height if wrapped_lines else 0) \
                + len(wrapped_lines) * body_line_height
            if total_height > safe_height:
                return None

            layout[house_num] = (number_text, wrapped_lines)
        return layout, number_font, body_font

    result = None
    for candidate in CONTENT_CANDIDATES:
        result = layout_fits(candidate)
        if result is not None:
            break
    if result is None:
        smallest = CONTENT_CANDIDATES[-1]
        layout = {h: (str(content[h][0]), [[item] for item in content[h][1]]) for h in range(1, 13)}
        result = (layout, max(int(smallest * NUMBER_FONT_RATIO), 6), smallest)
    layout, number_font, body_font = result
    number_line_height = number_font * 1.2
    body_line_height = body_font * 1.25

    c.setLineWidth(1.0)
    c.setStrokeColorRGB(0.769, 0.659, 0.463)  # #C4A876 muted gold
    for house_num, pts in houses.items():
        pdf_pts = [to_pdf_point(p) for p in pts]
        path = c.beginPath()
        path.moveTo(*pdf_pts[0])
        for p in pdf_pts[1:]:
            path.lineTo(*p)
        path.close()
        c.drawPath(path, stroke=1, fill=0)

        _, _, centroid_pdf = per_house_geometry[house_num]
        number_text, wrapped_lines = layout[house_num]

        cursor = 0.0
        number_local_y = cursor + number_line_height * 0.8
        cursor += number_line_height
        if wrapped_lines:
            cursor += 0.3 * body_line_height
        line_positions = []
        for line_items in wrapped_lines:
            line_positions.append((line_items, cursor + body_line_height * 0.8))
            cursor += body_line_height

        top_y = centroid_pdf[1] + cursor / 2.0

        c.setFont(CHART_FONT_REGULAR, number_font)
        c.setFillColorRGB(0.769, 0.659, 0.463)
        c.drawCentredString(centroid_pdf[0], top_y - number_local_y, number_text)

        c.setFont(CHART_FONT_REGULAR, body_font)
        for line_items, local_y in line_positions:
            y = top_y - local_y
            line_width = colored_line_width_pdf(line_items, CHART_FONT_REGULAR, body_font)
            cx = centroid_pdf[0] - line_width / 2.0
            for i, (label, color_hex) in enumerate(line_items):
                text = (", " if i > 0 else "") + label
                r = int(color_hex[1:3], 16) / 255
                g = int(color_hex[3:5], 16) / 255
                b = int(color_hex[5:7], 16) / 255
                c.setFillColorRGB(r, g, b)
                c.drawString(cx, y, text)
                cx += c.stringWidth(text, CHART_FONT_REGULAR, body_font)
    c.setFillColorRGB(0, 0, 0)


def draw_mini_house_diagram(c, pdf_x0, pdf_y0, size):
    """A simplified North Indian chart showing only the house numbers 1-12
    -- no signs, no planets -- for use as a small illustrative reference
    image (e.g. next to the intro page's "Houses" explanation).
    (pdf_x0, pdf_y0) is the BOTTOM-LEFT corner in reportlab's own
    coordinate system (y grows upward) -- kept deliberately simple and
    separate from the top-down/mirrored convention used by the main chart
    drawing function, to avoid confusing the two."""
    local_houses = build_house_polygons(0, 0, size, size)  # top-down, local to this diagram only

    def to_pdf(pt):
        lx, ly = pt
        return pdf_x0 + lx, pdf_y0 + size - ly

    c.setLineWidth(0.8)
    c.setStrokeColorRGB(0, 0, 0)
    font_size = max(int(size * 0.11), 7)
    c.setFont(CHART_FONT_REGULAR, font_size)
    for house_num, pts in local_houses.items():
        pdf_pts = [to_pdf(p) for p in pts]
        path = c.beginPath()
        path.moveTo(*pdf_pts[0])
        for p in pdf_pts[1:]:
            path.lineTo(*p)
        path.close()
        c.drawPath(path, stroke=1, fill=0)
        cx, cy = to_pdf(polygon_centroid(pts))
        c.drawCentredString(cx, cy - font_size * 0.32, str(house_num))


INTRO_SECTIONS = [
    ("What is this chart?",
     "This is your natal (birth) chart, calculated using the sidereal "
     "zodiac with the Lahiri ayanamsa. The system used in traditional "
     "Vedic (Jyotish) astrology. It's a snapshot of exactly where the Sun, "
     "Moon, and every planet were positioned at the moment and place you "
     "were born."),
    ("The Ascendant (marked 'As')",
     "Your Ascendant is the zodiac sign that was rising on the eastern "
     "horizon at your exact birth time. Everything else in the chart is read relative to it. The Ascendant "
     "will always fall in the first house, in every chart."),
    ("Houses",
     "The chart is divided into 12 houses, each representing a different "
     "area of life (self, money, communication, home, and so on). This "
     "chart uses the Whole Sign house system."),
    ("North Node (Rahu) and South Node (Ketu): The Lunar Nodes",
     "Rahu (the North Node) and Ketu (the South Node) aren't physical "
     "planets. They are the two points where the Moon's orbital path "
     "crosses the Sun's."),
    ("Retrograde",
     "A planet marked RETROGRADE appeared to be moving backward through "
     "the zodiac from Earth's point of view at the moment of your birth."),
]


PLANET_MEANINGS = {
    "Moon": "the mind, emotions, and instinctive reactions. how you feel and process life day to day.",
    "Sun": "the core self, willpower, and vitality. how you shine and lead.",
    "Mercury": "communication, intellect, and reasoning. how you think and express ideas.",
    "Venus": "love, beauty, and pleasure. what you're drawn to and how you relate to others.",
    "Mars": "drive, courage, and assertion. how you act and pursue what you want.",
    "Jupiter": "growth, wisdom, and fortune. where life expands and offers meaning.",
    "Saturn": "discipline, limitation, and time. where you mature through effort and restriction.",
    "Rahu": "worldly desire and forward momentum. what you're pulled toward growing into.",
    "Ketu": "detachment and past mastery. what comes naturally but calls for release.",
    "Uranus": "sudden change, originality, and rebellion. where you break from convention.",
    "Neptune": "imagination, spirituality, and illusion. where boundaries dissolve.",
    "Pluto": "deep transformation, power, and rebirth. where old structures break down and remake themselves.",
    "Ascendant": "the outer personality and how you meet the world. the lens the whole chart is read through.",
}

HOUSE_MEANINGS = {
    1: "physical body, overall health and vitality, personality, temperament, appearance, and how you present yourself to the world. It also touches longevity, general well-being, and one's sense of self.",
    2: "accumulated wealth and resources, family and family values, speech and voice, food and dietary habits, and personal values. It reflects self-worth and what one considers valuable enough to hold onto.",
    3: "courage, initiative, and self-effort, younger siblings, communication and short journeys, hobbies and skills, and the hands and arms. It's the house of valor -- the willingness to act.",
    4: "home and property, the mother, emotional foundation and inner comfort, vehicles, early education, and one's roots. It's where a sense of peace and belonging is built.",
    5: "children, creativity and intelligence, romance, past-life merit (purva punya), speculation, and spiritual practice such as mantra. It governs how one creates and what one brings into being.",
    6: "health and disease, daily work and service, obstacles, debts, litigation, competition, and rivals. It's the house of struggle -- where effort is required to overcome resistance.",
    7: "marriage and partnerships, the spouse, business relationships, public dealings, open enemies, and contracts. It's the house of the 'other' -- anyone met as an equal.",
    8: "transformation, longevity, shared resources such as inheritance or insurance, hidden or occult knowledge, sudden events, and in-laws. It marks the threshold between what's known and unknown.",
    9: "higher learning, philosophy and religion, the father, one's guru or teachers, fortune and luck, and long-distance or foreign travel. It's the house of dharma -- one's higher purpose.",
    10: "career and profession, public standing and reputation, authority, and actions taken in the world. It reflects how one rises, achieves, and is recognized.",
    11: "gains and income, hopes and wishes, elder siblings, friendships, and social networks. It's the house of fulfillment -- where desires find their outlet.",
    12: "loss and expenditure, solitude, foreign lands, spirituality and liberation (moksha), sleep, and letting go. It's the house of release -- where the material world falls away.",
}

# Section intros for the PDF -- kept here as named constants (rather than
# inline strings) so they're easy to find and tweak later, matching the
# pattern used by INTRO_SECTIONS / PLANET_MEANINGS / HOUSE_MEANINGS above.
intro_note = (
    "The gold numbers mark each house's zodiac sign (1 = Aries through "
    "12 = Pisces). Colored abbreviations show which planets sit in that "
    "house, each with its exact degree."
)
placements_intro = (
    "This section walks through where each planet landed in your chart "
    "-- which sign, at what degree, and in which house -- starting with "
    "the Moon, since it sets the tone for how everything else is "
    "experienced."
)


def generate_full_chart_pdf(chart_data, chart_title="Your Vedic Birth Chart"):
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.units import inch
    from reportlab.lib.pagesizes import letter

    buf = io.BytesIO()
    page_w, page_h = letter
    c = pdfcanvas.Canvas(buf, pagesize=letter)

    # -- Page 1: intro / how to read this chart --
    c.setFont("Times-Bold", 20)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "Read This Before You Try to Interpret")
    c.setFont("Times-Roman", 10)
    c.drawString(0.75 * inch, page_h - 1.15 * inch,
                 f"{chart_title} -- {chart_data['birth']['resolved_address']}")

    y = page_h - 1.6 * inch
    for heading, body in INTRO_SECTIONS:
        c.setFont("Times-Bold", 12)
        c.drawString(0.75 * inch, y, heading)
        y -= 0.22 * inch
        c.setFont("Times-Roman", 10)

        # The "Houses" section gets a small illustrative diagram to its
        # right (just house numbers, no signs/planets) -- its text column
        # narrows to make room, and the row's total height accounts for
        # whichever is taller: the wrapped text, or the diagram itself.
        is_houses_section = (heading == "Houses")
        diagram_size = 1.1 * inch
        right_reserve = (diagram_size + 0.25 * inch) if is_houses_section else 0
        max_width = page_w - 1.5 * inch - right_reserve

        section_top_y = y
        # simple word-wrap to fit the (possibly narrowed) width
        words = body.split()
        line = ""
        for word in words:
            trial = (line + " " + word).strip()
            if c.stringWidth(trial, "Times-Roman", 10) > max_width:
                c.drawString(0.75 * inch, y, line)
                y -= 0.18 * inch
                line = word
            else:
                line = trial
        if line:
            c.drawString(0.75 * inch, y, line)
            y -= 0.18 * inch

        if is_houses_section:
            diagram_x = page_w - 0.75 * inch - diagram_size
            diagram_y = section_top_y - diagram_size + 0.08 * inch
            draw_mini_house_diagram(c, diagram_x, diagram_y, diagram_size)
            # make sure the next section starts below the diagram too,
            # not just below the (possibly shorter) wrapped text
            y = min(y, diagram_y - 0.15 * inch)

        y -= 0.18 * inch
        if y < 1 * inch:
            c.showPage()
            y = page_h - 0.9 * inch

    # -- Page 2: the chart itself --
    c.showPage()
    c.setFont("Times-Bold", 16)
    c.drawCentredString(page_w / 2, page_h - 0.7 * inch, chart_title)
    c.setFont("Times-Roman", 9)
    c.drawCentredString(page_w / 2, page_h - 0.9 * inch,
                         f"{chart_data['birth']['local_datetime']} ({chart_data['birth']['timezone']}) "
                         f"-- {chart_data['birth']['resolved_address']}")

    # -- Key/legend, explaining how to read the diagram --
    c.setFont("Times-Bold", 16)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(0.75 * inch, page_h - 1.15 * inch, "Key to the Diagram Above")

    c.setFont("Times-Roman", 9)
    c.setFillColorRGB(0.769, 0.659, 0.463)
    c.drawString(0.75 * inch, page_h - 1.4 * inch, "\u25cf")
    c.setFillColorRGB(0, 0, 0)
   
    def _wrap_simple(text, max_width, size):
        words, lines, line = text.split(), [], ""
        for word in words:
            trial = (line + " " + word).strip()
            if c.stringWidth(trial, "Times-Roman", size) > max_width and line:
                lines.append(line)
                line = word
            else:
                line = trial
        if line:
            lines.append(line)
        return lines

    for i, ln in enumerate(_wrap_simple(intro_note, page_w - 1.9 * inch, 9)):
        c.drawString(0.95 * inch, page_h - 1.4 * inch - i * 0.16 * inch, ln)

    legend_bodies = [
        ("As", "Ascendant"), ("Su", "Sun"), ("Mo", "Moon"), ("Me", "Mercury"), ("Ve", "Venus"),
        ("Ma", "Mars"), ("Ju", "Jupiter"), ("Sa", "Saturn"), ("Ra", "North Node (Rahu)"), ("Ke", "South Node (Ketu)"),
        ("Ur", "Uranus"), ("Ne", "Neptune"), ("Pl", "Pluto"),
    ]
    body_key_map = {"As": "Ascendant", "Su": "Sun", "Mo": "Moon", "Me": "Mercury", "Ve": "Venus",
                     "Ma": "Mars", "Ju": "Jupiter", "Sa": "Saturn", "Ra": "Rahu", "Ke": "Ketu",
                     "Ur": "Uranus", "Ne": "Neptune", "Pl": "Pluto"}
    legend_y0 = page_h - 1.85 * inch
    col_x = [0.95 * inch, 3.0 * inch, 5.05 * inch]
    for i, (abbr, name) in enumerate(legend_bodies):
        col = i % 3
        row = i // 3
        x = col_x[col]
        y = legend_y0 - row * 0.22 * inch
        color_hex = PLANET_COLORS.get(body_key_map[abbr], "#333333")
        r, g, b = int(color_hex[1:3], 16) / 255, int(color_hex[3:5], 16) / 255, int(color_hex[5:7], 16) / 255
        c.setFont("Times-Bold", 9)
        c.setFillColorRGB(r, g, b)
        c.drawString(x, y, abbr)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Times-Roman", 9)
        c.drawString(x + 0.32 * inch, y, name)

    zodiac_key_y = legend_y0 - 5 * 0.22 * inch - 0.2 * inch
    c.setFont("Times-Bold", 10)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(0.75 * inch, zodiac_key_y, "Zodiac Key")
    zk_y = zodiac_key_y - 0.2 * inch
    # 6 signs per row, 2 rows
    zk_col_positions = [0.75 * inch + j * ((page_w - 1.5 * inch) / 6) for j in range(6)]
    for i, sign in enumerate(SIGNS):
        row = i // 6
        col = i % 6
        c.setFont("Times-Roman", 9)
        c.drawString(zk_col_positions[col], zk_y - row * 0.2 * inch, f"{i + 1} = {sign}")

    chart_width = 7.5 * inch
    chart_height = 5.0 * inch
    chart_x0 = (page_w - chart_width) / 2
    chart_y0 = 0.95 * inch
    draw_chart_on_pdf_canvas(c, chart_data, chart_x0, chart_y0, chart_width, chart_height)

    def _wrap(text, max_width, size, font="Times-Roman"):
        words, lines, line = text.split(), [], ""
        for word in words:
            trial = (line + " " + word).strip()
            if c.stringWidth(trial, font, size) > max_width and line:
                lines.append(line)
                line = word
            else:
                line = trial
        if line:
            lines.append(line)
        return lines or [""]

    # -- Page 3: House & Sign Key -- decodes the gold numbers in the diagram --
    c.showPage()
    c.setFont("Times-Bold", 16)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "House & Sign Key")
    c.setFont("Times-Roman", 9)
    note = ("Every chart's Ascendant defines House 1 -- each sign listed below is simply "
             "the next sign in zodiac order for each house that follows. \"Sign #\" is the same "
             "number shown in gold on the diagram for that sign.")
    ny = page_h - 1.15 * inch
    for ln in _wrap(note, page_w - 1.5 * inch, 9):
        c.drawString(0.75 * inch, ny, ln)
        ny -= 0.16 * inch

    y = ny - 0.25 * inch
    c.setFont("Times-Bold", 10)
    c.drawString(0.75 * inch, y, "House")
    c.drawString(2.0 * inch, y, "Sign")
    c.drawString(3.5 * inch, y, "Sign #")
    y -= 0.24 * inch
    c.setFont("Times-Roman", 10)
    for house_data in chart_data["houses"]:
        label = f"House {house_data['house']}"
        if house_data["house"] == 1:
            label += "  (Ascendant)"
        c.drawString(0.75 * inch, y, label)
        c.drawString(2.0 * inch, y, house_data["sign"])
        c.drawString(3.5 * inch, y, str(SIGNS.index(house_data["sign"]) + 1))
        y -= 0.24 * inch

    # -- Page 3b: Vimshottari Dasha -- previous, current, and upcoming
    # Mahadasha/Antardasha periods --
    c.showPage()
    c.setFont(CHART_FONT_BOLD, 16)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "Vimshottari Dasha")
    c.setFont(CHART_FONT_REGULAR, 9)
    dasha_note = ("The Vimshottari Dasha system marks out major life periods (Mahadasha) and their "
                   "sub-periods (Antardasha), based on the Moon's position at birth. Shown below: the "
                   "period just before now, the one currently active, and the one coming up next.")
    dy = page_h - 1.15 * inch
    for ln in _wrap(dasha_note, page_w - 1.5 * inch, 9, font=CHART_FONT_REGULAR):
        c.drawString(0.75 * inch, dy, ln)
        dy -= 0.16 * inch

    y = dy - 0.3 * inch

    def _describe_rules(house_list):
        if not house_list:
            return "rules no houses in this chart"
        if len(house_list) == 1:
            return f"rules House {house_list[0]}"
        return "rules Houses " + ", ".join(str(h) for h in house_list)

    for label, key in [("Previous", "previous"), ("Current", "current"), ("Upcoming", "next")]:
        p = chart_data["dashas"].get(key)
        heading = f"{label}: {p['mahadasha_display_name']} Mahadasha / {p['antardasha_display_name']} Antardasha" if p \
            else f"{label}: (outside calculated range)"
        dates = f"{p['start']} to {p['end']}" if p else ""

        paragraph = ""
        if p:
            paragraph = (
                f"{p['mahadasha_display_name']} is placed in House {p['mahadasha_house']} and "
                f"{_describe_rules(p['mahadasha_rules_houses'])}. "
                f"{p['antardasha_display_name']} is placed in House {p['antardasha_house']} and "
                f"{_describe_rules(p['antardasha_rules_houses'])}. "
                f"The two planets are in {p['angular_relationship']} to each other."
            )
        body_lines = _wrap(paragraph, page_w - 1.5 * inch, 9.5, font=CHART_FONT_REGULAR) if paragraph else []
        block_height = 0.22 * inch + 0.18 * inch + len(body_lines) * 0.16 * inch + 0.2 * inch

        if y - block_height < 0.75 * inch:
            c.showPage()
            y = page_h - 0.9 * inch

        c.setFont(CHART_FONT_BOLD, 11)
        c.setFillColorRGB(0.769, 0.659, 0.463) if key == "current" else c.setFillColorRGB(0, 0, 0)
        c.drawString(0.75 * inch, y, heading)
        c.setFillColorRGB(0, 0, 0)
        y -= 0.2 * inch
        if dates:
            c.setFont(CHART_FONT_REGULAR, 9)
            c.drawString(0.75 * inch, y, dates)
            y -= 0.18 * inch
        c.setFont(CHART_FONT_REGULAR, 9.5)
        for ln in body_lines:
            c.drawString(0.75 * inch, y, ln)
            y -= 0.16 * inch
        y -= 0.2 * inch

    # -- Page 4: Placements -- Moon-first, narrative read-along format,
    # matching the Houses section's style --
    c.showPage()
    c.setFont("Times-Bold", 16)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "Placements")
    c.setFont("Times-Roman", 9)
    y = page_h - 1.15 * inch
    for ln in _wrap(placements_intro, page_w - 1.5 * inch, 9):
        c.drawString(0.75 * inch, y, ln)
        y -= 0.16 * inch
    y -= 0.15 * inch

    placements_by_name = {"Ascendant": {"display_name": "Ascendant", "abbr": "As",
                                          "sign": chart_data["ascendant"]["sign"],
                                          "degree": chart_data["ascendant"]["degree"],
                                          "house": 1, "retrograde": False}}
    for p in chart_data["planets"]:
        placements_by_name[p["name"]] = p

    MOON_FIRST_ORDER = ["Ascendant", "Moon", "Sun", "Mercury", "Venus", "Mars",
                        "Jupiter", "Saturn", "Rahu", "Ketu", "Uranus", "Neptune", "Pluto"]

    for name in MOON_FIRST_ORDER:
        p = placements_by_name.get(name)
        if p is None:
            continue
        heading = f"{p['display_name']} ({p['abbr']})" + (" -- Retrograde" if p.get("retrograde") else "")
        meaning = PLANET_MEANINGS.get(name, "")
        ruled_houses = [h["house"] for h in chart_data["houses"] if h.get("ruler") == name]
        if len(ruled_houses) == 1:
            rules_prefix = f"Ruler of House {ruled_houses[0]}. "
        elif len(ruled_houses) > 1:
            rules_prefix = "Ruler of Houses " + " and ".join(str(h) for h in ruled_houses) + ". "
        else:
            rules_prefix = ""
        paragraph = (f"{rules_prefix}{meaning} Placed in {p['sign']} at {p['degree']:.1f}\u00b0, in House {p['house']}.")

        body_lines = _wrap(paragraph, page_w - 1.5 * inch, 9.5)
        block_height = 0.22 * inch + len(body_lines) * 0.16 * inch + 0.18 * inch

        if y - block_height < 0.75 * inch:
            c.showPage()
            y = page_h - 0.9 * inch

        c.setFont("Times-Bold", 11)
        c.drawString(0.75 * inch, y, heading)
        y -= 0.22 * inch
        c.setFont("Times-Roman", 9.5)
        for ln in body_lines:
            c.drawString(0.75 * inch, y, ln)
            y -= 0.16 * inch
        y -= 0.18 * inch

    # -- Page 5: Houses -- narrative, read-along format, Braha-style compact meanings --
    c.showPage()
    c.setFont("Times-Bold", 16)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "Houses")
    c.setFont("Times-Roman", 9)
    intro2 = ("This section walks through each house in order, so you can follow along house by "
              "house. "
              "Each house also lists which planets aspect it. An aspect is when a planet, sitting "
              "elsewhere in the chart, still casts its influence onto that house, in addition to "
              "whatever planet is physically placed there.")
    y = page_h - 1.15 * inch
    for ln in _wrap(intro2, page_w - 1.5 * inch, 9):
        c.drawString(0.75 * inch, y, ln)
        y -= 0.16 * inch
    y -= 0.15 * inch

    for house_data in chart_data["houses"]:
        h = house_data["house"]
        heading = f"House {h} ({house_data['sign']}, ruled by {house_data['ruler_display_name']})"
        bodies_text = ", ".join(
            f"{b['display_name']} ({b['abbr']}) {b['degree']:.1f}\u00b0" for b in house_data["bodies"]
        ) or "no placements"
        aspects_text = ", ".join(
            f"{a['display_name']} ({a['abbr']})" for a in house_data["aspects"]
        ) or "none"
        paragraph = (f"This house rules {HOUSE_MEANINGS.get(h, '')} "
                     f"Placed here: {bodies_text}. Aspected by: {aspects_text}.")

        body_lines = _wrap(paragraph, page_w - 1.5 * inch, 9.5)
        block_height = 0.22 * inch + len(body_lines) * 0.16 * inch + 0.18 * inch

        if y - block_height < 0.75 * inch:
            c.showPage()
            y = page_h - 0.9 * inch

        c.setFont("Times-Bold", 11)
        c.drawString(0.75 * inch, y, heading)
        y -= 0.22 * inch
        c.setFont("Times-Roman", 9.5)
        for ln in body_lines:
            c.drawString(0.75 * inch, y, ln)
            y -= 0.16 * inch
        y -= 0.18 * inch


    # -- Page 5: Planetary Angles (conjunction/sextile/square/trine/opposition) --
    c.showPage()
    c.setFont("Times-Bold", 16)
    c.drawString(0.75 * inch, page_h - 0.9 * inch, "Planetary Angles")

    ASPECT_MEANINGS = [
        ("Conjunction (0\u00b0)", "The two bodies sit at nearly the same degree -- "
         "their energies blend together and intensify each other."),
        ("Sextile (60\u00b0)", "An easy, supportive connection that offers "
         "opportunity, though it usually takes a bit of effort to make use of."),
        ("Square (90\u00b0)", "A tense, dynamic aspect that creates friction and "
         "challenge -- often the pressure that pushes real growth."),
        ("Trine (120\u00b0)", "A harmonious, flowing aspect where the two "
         "energies support each other with ease."),
        ("Opposition (180\u00b0)", "A polarizing aspect, pulling two areas in "
         "opposite directions and calling for balance between them."),
    ]

    key_y = page_h - 1.25 * inch
    line_gap = 0.24 * inch
    for i, (label, meaning) in enumerate(ASPECT_MEANINGS):
        c.setFont("Times-Bold", 10)
        c.drawString(0.75 * inch, key_y - i * line_gap, label)
        c.setFont("Times-Roman", 9)
        wrapped = _wrap(meaning, page_w - 3.0 * inch, 9)
        c.drawString(2.3 * inch, key_y - i * line_gap, wrapped[0])
        if len(wrapped) > 1:
            # rare, only if a meaning is unusually long -- keep it simple
            # by continuing directly beneath rather than reflowing the grid
            c.drawString(2.3 * inch, key_y - i * line_gap - 0.14 * inch, wrapped[1])

    y = key_y - len(ASPECT_MEANINGS) * line_gap - 0.3 * inch
    c.setFont("Times-Roman", 9)
    c.drawString(0.75 * inch, y, f"Orb used: aspects within {ASPECT_ANGLE_ORB:.0f}\u00b0 of exact are shown below.")
    y -= 0.35 * inch

    col_x3 = [0.75 * inch, 2.6 * inch, 4.45 * inch, 5.7 * inch]
    headers3 = ["Body 1", "Body 2", "Aspect", "Orb"]
    c.setFont("Times-Bold", 9)
    for cx, h in zip(col_x3, headers3):
        c.drawString(cx, y, h)
    y -= 0.22 * inch
    c.setFont("Times-Roman", 8)

    if not chart_data["planetary_aspects"]:
        c.drawString(0.75 * inch, y, "No planetary aspects fall within orb for this chart.")
    else:
        for a in chart_data["planetary_aspects"]:
            if y < 0.75 * inch:
                c.showPage()
                y = page_h - 0.9 * inch
                c.setFont("Times-Roman", 8)
            c.drawString(col_x3[0], y, f"{a['body1']} ({a['abbr1']})")
            c.drawString(col_x3[1], y, f"{a['body2']} ({a['abbr2']})")
            c.drawString(col_x3[2], y, a['aspect'])
            c.drawString(col_x3[3], y, f"{a['orb']:.2f}\u00b0")
            y -= 0.18 * inch

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
    """Shared argument parsing/validation for the birth-chart endpoints."""
    required = ["year", "month", "day", "hour", "minute", "city"]
    missing = [r for r in required if r not in args]
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}")
    return (
        int(args["year"]), int(args["month"]), int(args["day"]),
        int(args["hour"]), int(args["minute"]), args["city"],
    )


def parse_coords_args(args):
    """Shared argument parsing/validation for the "chart for now" endpoints,
    which take coordinates directly (from the browser's own geolocation)
    rather than a city name to geocode."""
    required = ["lat", "lon"]
    missing = [r for r in required if r not in args]
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}")
    try:
        lat = float(args["lat"])
        lon = float(args["lon"])
    except ValueError:
        raise ValueError("lat/lon must be numbers")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of valid range")
    return lat, lon


def compute_chart_for_now(lat, lon):
    """Chart for the current moment at the given coordinates. Tries to
    resolve a human-readable place name via reverse geocoding for display
    purposes; falls back to showing the raw coordinates if that lookup
    fails or is unavailable, rather than failing the whole request over
    what's just a cosmetic label."""
    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=lat, lng=lon)
    if tz_name is None:
        raise ValueError(f"Could not determine a timezone for ({lat}, {lon})")

    try:
        geolocator = Nominatim(user_agent="natal_chart_api")
        reverse = geolocator.reverse((lat, lon), language="en")
        location_label = reverse.address if reverse else f"{lat:.4f}, {lon:.4f}"
    except Exception:
        location_label = f"{lat:.4f}, {lon:.4f}"

    now_local = dt.datetime.now(ZoneInfo(tz_name))
    return compute_chart_from_coords(
        now_local.year, now_local.month, now_local.day,
        now_local.hour, now_local.minute,
        lat, lon, location_label,
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


@app.route("/api/chart-now")
def api_chart_now():
    try:
        lat, lon = parse_coords_args(request.args)
        chart_data = compute_chart_for_now(lat, lon)
        return jsonify(chart_data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.route("/api/chart-now-svg")
def api_chart_now_svg():
    try:
        lat, lon = parse_coords_args(request.args)
        chart_data = compute_chart_for_now(lat, lon)
        svg = generate_north_indian_chart_svg(chart_data)
        return Response(svg, mimetype="image/svg+xml")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.route("/api/chart-now-pdf")
def api_chart_now_pdf():
    try:
        lat, lon = parse_coords_args(request.args)
        chart_data = compute_chart_for_now(lat, lon)
        pdf_buffer = generate_full_chart_pdf(chart_data, chart_title="The Sky Right Now")
        return Response(
            pdf_buffer.read(),
            mimetype="application/pdf",
            headers={"Content-Disposition": "attachment; filename=sky_right_now.pdf"},
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
