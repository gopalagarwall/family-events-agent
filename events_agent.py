#!/usr/bin/env python3
"""
Daily family events agent for the Isle of Dogs / E14 and wider London.

1. Pulls a 7-day weather forecast for E14 (Open-Meteo, no key needed).
2. Asks Claude (with web search) for toddler-friendly, pram-friendly events.
3. Drops excluded events (e.g. the regular Idea Store Canary Wharf story time)
   and anything already emailed before.
4. Emails a digest to Gmail with an "Add to Google Calendar" button per event
   and an .ics invite attached for each.

Run locally without sending anything:
    python events_agent.py --mock --dry-run
"""

import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

# ----------------------------- Settings ---------------------------------
MODEL = "claude-sonnet-5-5"
CHILD_NAME = "Pari"
CHILD_BIRTH = date(2024, 8, 1)
HOME_LAT, HOME_LON = 51.4975, -0.0175          # Isle of Dogs, E14
DAYS_AHEAD = 14                                # how far ahead to look
MAX_EVENTS = 10                                # per run
MAX_SEARCHES = 10                              # web searches per run (cost cap)
SEEN_KEEP_DAYS = 60

# Both patterns must match (title + venue + address) for an event to be dropped.
EXCLUDE_RULES = [
    (r"idea\s*store\s*canary\s*wharf", r"story|rhyme|play"),
]

LONDON = ZoneInfo("Europe/London")
SEEN_FILE = Path(__file__).with_name("seen_events.json")


# ----------------------------- Weather ----------------------------------
def get_weather():
    params = {
        "latitude": HOME_LAT,
        "longitude": HOME_LON,
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,wind_gusts_10m_max",
        "timezone": "Europe/London",
        "forecast_days": 7,
    }
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20)
        r.raise_for_status()
        d = r.json()["daily"]
        return [
            {
                "date": d["time"][i],
                "max": round(d["temperature_2m_max"][i]),
                "min": round(d["temperature_2m_min"][i]),
                "rain": d["precipitation_probability_max"][i] or 0,
                "gust": round(d["wind_gusts_10m_max"][i] or 0),
            }
            for i in range(len(d["time"]))
        ]
    except Exception as exc:  # weather is nice-to-have, never fatal
        print(f"Weather unavailable: {exc}", file=sys.stderr)
        return []


# ----------------------------- Claude search ----------------------------
def build_prompt(today, weather):
    months = (today.year - CHILD_BIRTH.year) * 12 + today.month - CHILD_BIRTH.month
    end = today + timedelta(days=DAYS_AHEAD)
    wx = "\n".join(
        f"- {w['date']}: {w['min']}-{w['max']}°C, rain chance {w['rain']}%, gusts {w['gust']} km/h"
        for w in weather
    ) or "- (forecast unavailable)"

    return f"""Today is {today:%A %d %B %Y}. Find family events from {today:%d %b} to {end:%d %b %Y}.

Who it's for: a toddler aged {months // 12} years {months % 12} months with parents, travelling by pram from the Isle of Dogs, London E14.

Where:
1. First priority: walkable by pram in and around E14 - Isle of Dogs, Canary Wharf, Millwall, Cubitt Town, Poplar, Limehouse, and Greenwich via the foot tunnel.
2. Also: a few standout events elsewhere in London that are easy to reach step-free (DLR, Jubilee or Elizabeth line).

Good fits: stay-and-play, rhyme/story/music sessions, toddler soft play, farm and park events, museum family days, festivals and cultural celebrations (e.g. Diwali, Christmas), free or low-cost events.

Rules:
- Only include events you found evidence for in search results for these dates. Never guess dates or times.
- Do NOT include the regular Idea Store Canary Wharf story time, rhyme time or play sessions - we already know about them.
- Skip events only for older children, adult-only events, and anything ending after 7pm unless it's a major one-off.
- Use the weather below: on wet or windy days prefer indoor options and say so.
- Up to {MAX_EVENTS} events, best first.

Weather forecast (E14):
{wx}

After your research, finish with ONLY this block:
<events_json>
[{{"title": "...", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM or null", "all_day": false,
  "venue": "...", "address": "...", "area": "E14 or London", "indoor": true,
  "cost": "Free / £x", "booking": "Drop-in / Book ahead", "url": "https://...",
  "why": "one line: why it suits a toddler, plus any pram or weather note"}}]
</events_json>
Times are London local time. If only the date is known, set all_day to true and use T00:00."""


def find_events(prompt):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": MAX_SEARCHES,
        "user_location": {"type": "approximate", "city": "London", "region": "England",
                          "country": "GB", "timezone": "Europe/London"},
    }]
    messages = [{"role": "user", "content": prompt}]
    text = ""
    for _ in range(6):  # long searches can pause; continue until done
        resp = client.messages.create(model=MODEL, max_tokens=8000, messages=messages, tools=tools)
        text += "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]

    match = re.search(r"<events_json>(.*?)</events_json>", text, re.S)
    if not match:
        raise RuntimeError("No <events_json> block in Claude's reply:\n" + text[-2000:])
    raw = re.sub(r"^```(?:json)?|```$", "", match.group(1).strip(), flags=re.M).strip()
    return json.loads(raw)


def mock_events(today):
    d1, d2 = today + timedelta(days=2), today + timedelta(days=5)
    return [
        {"title": "Diwali family day", "start": f"{d2}T10:30", "end": f"{d2}T17:00", "all_day": False,
         "venue": "National Maritime Museum", "address": "Park Row, Greenwich SE10 9NF", "area": "London",
         "indoor": True, "cost": "Free", "booking": "Drop-in", "url": "https://www.rmg.co.uk",
         "why": "Lantern parade and rangoli; indoors, step-free via the foot tunnel lifts."},
        {"title": "Story Time for under 5s", "start": f"{d1}T10:30", "end": f"{d1}T11:00", "all_day": False,
         "venue": "Idea Store Canary Wharf", "address": "Churchill Place, E14", "area": "E14",
         "indoor": True, "cost": "Free", "booking": "Drop-in", "url": "", "why": "Should be excluded."},
    ]


# ----------------------------- Cleaning ---------------------------------
def parse_dt(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=LONDON) if dt.tzinfo is None else dt.astimezone(LONDON)


def clean(events, today):
    out = []
    for e in events:
        try:
            start = parse_dt(e["start"])
        except Exception:
            continue
        if not today <= start.date() <= today + timedelta(days=DAYS_AHEAD):
            continue

        text = " ".join(str(e.get(k) or "") for k in ("title", "venue", "address")).lower()
        if any(re.search(a, text) and re.search(b, text) for a, b in EXCLUDE_RULES):
            continue

        all_day = bool(e.get("all_day"))
        end = None
        if e.get("end"):
            try:
                end = parse_dt(e["end"])
            except Exception:
                end = None
        if not all_day and (end is None or end <= start):
            end = start + timedelta(hours=1)

        key_src = f"{(e.get('title') or '').lower().strip()}|{start.date()}"
        e.update(_start=start, _end=end, _all_day=all_day,
                 _key=hashlib.sha1(key_src.encode()).hexdigest()[:16])
        out.append(e)
    return sorted(out, key=lambda x: x["_start"])


def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen, today):
    cutoff = (today - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True) + "\n")


# ----------------------------- Calendar ---------------------------------
def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ics_escape(value):
    return (str(value).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def fold(line):
    if len(line) <= 73:
        return line
    chunks = [line[:73]] + [line[i:i + 72] for i in range(73, len(line), 72)]
    return "\r\n ".join(chunks)


def location(e):
    return ", ".join(x for x in (e.get("venue"), e.get("address")) if x)


def details(e):
    bits = [e.get("why"), f"Cost: {e.get('cost') or '?'} | {e.get('booking') or ''}", e.get("url")]
    return "\n".join(b for b in bits if b)


def make_ics(e):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//family-events-agent//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:{e['_key']}@family-events-agent",
        f"DTSTAMP:{utc(datetime.now(timezone.utc))}",
    ]
    if e["_all_day"]:
        d = e["_start"].date()
        lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
    else:
        lines += [f"DTSTART:{utc(e['_start'])}", f"DTEND:{utc(e['_end'])}"]
    lines += [
        f"SUMMARY:{ics_escape(e.get('title') or 'Family event')}",
        f"LOCATION:{ics_escape(location(e))}",
        f"DESCRIPTION:{ics_escape(details(e))}",
    ]
    if e.get("url"):
        lines.append(f"URL:{e['url']}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def gcal_link(e):
    if e["_all_day"]:
        d = e["_start"].date()
        dates = f"{d:%Y%m%d}/{d + timedelta(days=1):%Y%m%d}"
    else:
        dates = f"{utc(e['_start'])}/{utc(e['_end'])}"
    params = {"action": "TEMPLATE", "text": e.get("title") or "Family event", "dates": dates,
              "location": location(e), "details": details(e), "ctz": "Europe/London"}
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def when(e):
    s = e["_start"]
    if e["_all_day"]:
        return s.strftime("%a %-d %b (all day)")
    return f"{s:%a %-d %b}, {s:%H:%M}-{e['_end']:%H:%M}"


def slug(e):
    name = re.sub(r"[^a-z0-9]+", "-", (e.get("title") or "event").lower()).strip("-")[:40]
    return f"{e['_start']:%Y-%m-%d}-{name}"


# ----------------------------- Email ------------------------------------
def render_email(events, weather):
    wx_cells = "".join(
        f"<td style='padding:6px 8px;text-align:center;font-size:12px;"
        f"background:{'#fdecea' if w['rain'] >= 50 or w['gust'] >= 45 else '#eef6ee'}'>"
        f"<b>{datetime.fromisoformat(w['date']):%a}</b><br>{w['max']}°<br>{w['rain']}% rain</td>"
        for w in weather
    )
    wx_html = (f"<table style='border-collapse:separate;border-spacing:3px;margin-bottom:16px'>"
               f"<tr>{wx_cells}</tr></table>") if weather else ""

    cards, plain = [], []
    for e in events:
        tag = "Indoor" if e.get("indoor") else "Outdoor"
        link = (f" &middot; <a href='{escape(e['url'])}'>Details</a>" if e.get("url") else "")
        cards.append(f"""
<div style="border:1px solid #ddd;border-radius:10px;padding:14px;margin-bottom:12px">
  <div style="font-size:16px;font-weight:bold">{escape(e.get('title') or '')}</div>
  <div style="color:#555;margin:4px 0">{escape(when(e))} &middot; {escape(e.get('area') or '')} &middot; {tag}</div>
  <div>{escape(location(e))}</div>
  <div style="margin:6px 0">{escape(e.get('cost') or '')} &middot; {escape(e.get('booking') or '')}</div>
  <div style="color:#333;margin-bottom:10px">{escape(e.get('why') or '')}</div>
  <a href="{escape(gcal_link(e))}" style="background:#1a73e8;color:#fff;padding:8px 12px;border-radius:6px;text-decoration:none">Add to Google Calendar</a>{link}
</div>""")
        plain.append(f"- {e.get('title')} | {when(e)} | {location(e)} | {e.get('cost')} | {e.get('url') or ''}")

    html = f"""<div style="font-family:Arial,sans-serif;max-width:600px">
<h2 style="margin:0 0 8px">New events for {CHILD_NAME}</h2>
{wx_html}{''.join(cards)}
<p style="color:#888;font-size:12px">Each event is also attached as an .ics file. Always check the venue's page before heading out.</p>
</div>"""
    return html, "New events:\n" + "\n".join(plain)


def send_email(subject, html, plain, attachments):
    sender = os.environ["GMAIL_ADDRESS"]
    to = os.environ.get("TO_ADDRESS") or sender
    msg = MIMEMultipart("mixed")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(plain, "plain", "utf-8"))
    body.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(body)

    for name, content in attachments:
        part = MIMEText(content, "calendar", "utf-8")
        part.set_param("method", "PUBLISH")
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)


# ----------------------------- Main -------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv
    today = datetime.now(LONDON).date()
    weather = get_weather()

    raw = mock_events(today) if "--mock" in sys.argv else find_events(build_prompt(today, weather))
    events = clean(raw, today)
    seen = load_seen()
    new = [e for e in events if e["_key"] not in seen]
    print(f"Found {len(events)} events after filtering, {len(new)} new.")

    if not new:
        save_seen(seen, today)
        return

    subject = f"{len(new)} new event{'s' if len(new) != 1 else ''} for {CHILD_NAME} - {today:%a %-d %b}"
    html, plain = render_email(new, weather)
    attachments = [(f"{slug(e)}.ics", make_ics(e)) for e in new]

    if dry_run:
        out = Path("out")
        out.mkdir(exist_ok=True)
        (out / "email.html").write_text(html)
        for name, content in attachments:
            (out / name).write_text(content)
        print(f"Dry run: wrote email and {len(attachments)} .ics file(s) to ./out")
        return

    send_email(subject, html, plain, attachments)
    for e in new:
        seen[e["_key"]] = today.isoformat()
    save_seen(seen, today)
    print(f"Emailed {len(new)} event(s).")


if __name__ == "__main__":
    main()
