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
  GET  /api/chart   -- returns chart data as JSON (positions, houses, etc.)
  GET  /api/chart.svg -- returns the North Indian chart as an SVG image
  GET  /health      -- simple check that the server is running

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
DISPLAY_NAME = {"Rahu": "North Node (Rahu)", "Ketu": "South Node (Ketu)"}

INNER_VERTEX_INDEX = {1: 2, 2: 2, 3: 1, 4: 2, 5: 2, 6: 1,
                       7: 2, 8: 2, 9: 1, 10: 2, 11: 2, 12: 1}


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


def fit_font_size(text, max_width, candidates=(15, 14, 13, 12, 11, 10, 9, 8, 7)):
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
    for p in chart_data["planets"]:
        by_house[p["house"]].append((p["name"], p["degree"]))

    asc_index = SIGNS.index(asc_sign)
    house_sign = {h: SIGNS[(asc_index + h - 1) % 12] for h in range(1, 13)}

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_size}" '
        f'height="{canvas_size}" viewBox="0 0 {canvas_size} {canvas_size}">',
        f'<rect x="0" y="0" width="{canvas_size}" height="{canvas_size}" fill="white"/>',
    ]

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

        sign_name = house_sign[house_num]
        occupants = by_house[house_num]
        planet_labels = [f"{PLANET_ABBR.get(n, n)} {d:.1f}\u00b0" for n, d in occupants]
        all_lines = [sign_name] + planet_labels

        block_font = min(fit_font_size(ln, safe_width) for ln in all_lines)
        line_height = block_font * 1.2
        start_y = centroid[1] - (len(all_lines) - 1) * line_height / 2
        for i, ln in enumerate(all_lines):
            is_sign_line = (i == 0)
            style_attr = 'fill="#555"' if is_sign_line else 'font-weight="bold" fill="black"'
            svg_lines.append(
                f'<text x="{centroid[0]:.1f}" y="{start_y + i * line_height:.1f}" '
                f'font-size="{block_font}" text-anchor="middle" '
                f'{style_attr} '
                f'font-family="sans-serif">{ln}</text>'
            )

    svg_lines.append('</svg>')
    return "\n".join(svg_lines)


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


@app.route("/api/chart.svg")
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
