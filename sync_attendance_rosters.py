#!/usr/bin/env python3
"""
Sync ONtraport contacts → edu.imageft.ie attendance rosters.

Pulls contacts LIVE from the ONtraport API (same framework as the finance
dashboard — ontraport_sync.py's ONtraportClient + FIELD_MAP + OPT decoder)
and pushes each contact into the right per-course attendance register on
edu.imageft.ie.

Students are matched to a course by (location × start date × category) —
identical to the join Adam's finance dashboard uses. So when a sale lands
in ONtraport, or a student switches course, the change flows through to
attendance here on the next run.

Runs once. Idempotent — re-running just re-asserts current state.

Usage:
    python3 sync_attendance_rosters.py [--dry-run] [--course cork-mat-260606,kerry-ref-260606]
    python3 sync_attendance_rosters.py --use-csv   # fallback to stale CSV if API down

Env vars:
    OP_APP_ID, OP_API_KEY  — ONtraport credentials (defaults in ontraport_sync.py)
    APPS_SCRIPT_URL        — live /exec URL of the Apps Script web app

Wire-up:
    Append to run_weekly_sync.sh:
        python3 sync_attendance_rosters.py
"""

import argparse
import csv
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'v2'))

# Pull straight from the v2 finance dashboard's ONtraport layer — same
# discover + batch-fetch logic that produces the 17-student Cork Mat list
# on edu.imageft.ie/admin/finance. Single source of truth.
#
# Why v2 not v1:
#   v1 ontraport_sync.py uses a frozen ID snapshot (.op_live_s26.csv from
#   May 5) — it can't see students who enrolled after that. v2 instead
#   server-side-filters ONtraport by Course Year=2026 (dropdown), which
#   finds every current 2026 student in ~3 API calls.
from app.ontraport import (  # noqa: E402
    discover_s26_contact_ids,
    fetch_contacts_full,
)


# ─── Dynamic course discovery ─────────────────────────────────────────────
# Course list is pulled live from edu.imageft.ie's /api/attendance-courses
# endpoint (which sources from lib/data.js — single source of truth). The
# sync iterates over EVERY PT / Pilates / Reformer course in S26+ and
# pushes a roster for each, so a new sale on any intake auto-creates the
# attendance register without any manual config here.

COURSES_API = 'https://edu.imageft.ie/api/attendance-courses'

# Per-course filter accepts location-label variants to absorb ONtraport
# label drift (e.g. 'Cork' vs 'Cork City', 'Killarney' vs 'Killarney, Kerry').
LOCATION_ALIASES = {
    'Cork City':         ['Cork', 'Cork City'],
    'Killarney, Kerry':  ['Kerry', 'Killarney', 'Killarney, Kerry'],
    'Dublin - Swords':   ['Dublin', 'Swords', 'Dublin - Swords'],
    'Dublin - Tallaght': ['Dublin', 'Tallaght', 'Dublin - Tallaght'],
    'Galway':            ['Galway'],
    'Limerick':          ['Limerick'],
    'Wexford':           ['Wexford'],
    'Clare':             ['Clare'],
    "Derry / L'Derry":   ['Derry', "L'Derry", "Derry / L'Derry"],
    'Online':            ['Online'],
}

# ONtraport stores course start dates as 'D-M-YYYY' or 'DD-MM-YYYY' strings
# (with the dropdown's display label). Accept either format.
def _date_variants(iso):
    if not iso: return []
    try:
        y, m, d = iso.split('-')
        m_int, d_int = int(m), int(d)
        return [
            f'{d_int}-{m_int}-{y}',          # 6-6-2026
            f'{d_int:02d}-{m_int:02d}-{y}',  # 06-06-2026
            iso,                              # 2026-06-06
        ]
    except (ValueError, AttributeError):
        return [iso]


# 2026-07-28 — tolerant matching. The old match() required the ONtraport date to
# be a byte-exact member of the 3 hard-coded dash formats and the location to be
# a case-exact alias; any drift (slashes, leading zeros, 2-digit year, casing)
# silently produced an EMPTY roster. These canonicalize BOTH sides so the same
# real date/location matches regardless of format — WITHOUT loosening semantics
# (a different day or place still won't match, so no contact is mis-assigned).
def _norm_date(s):
    """Canonicalize a date string to 'YYYY-M-D', or None if unparseable.
    Accepts -, /, . separators; D-M-Y or Y-M-D order; 2- or 4-digit years."""
    if not s:
        return None
    s = str(s).strip().replace('/', '-').replace('.', '-')
    parts = [p for p in s.split('-') if p != '']
    if len(parts) != 3:
        return None
    try:
        a, b, c = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if len(parts[0]) == 4 or a > 31:      # first part is the year → Y-M-D
        y, m, d = a, b, c
    else:                                  # D-M-Y
        d, m, y = a, b, c
        if y < 100:
            y += 2000
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f'{y}-{m}-{d}'


def _norm_loc(s):
    return (s or '').strip().lower()


def fetch_rosters_from_api():
    """Build ROSTERS dict live from edu.imageft.ie. One entry per active
    or future PT / Pilates / Reformer intake. Past courses are skipped by
    default (use --include-past to backfill historic registers)."""
    import urllib.request as _u
    rosters = {}
    legacy = {}
    try:
        with _u.urlopen(COURSES_API, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'  WARNING: courses API unreachable ({e}); falling back to static list')
        return _static_fallback_rosters()
    for c in data.get('courses', []):
        slug = c['slug']
        loc_name = c['location']
        opts = c.get('ontraport') or {}
        rosters[slug] = {
            'label': f'{loc_name} {c["course"]} · {c["start"]}',
            'location_col': opts.get('location'),
            'start_col': opts.get('startDate'),
            'location': LOCATION_ALIASES.get(loc_name, [loc_name]),
            'start_date_iso': c['start'],
            'start_date_formats': _date_variants(c['start']),
            'status': c.get('status', 'unknown'),
            'season': c.get('season', 'unknown'),
        }
        if c.get('id'):
            legacy[c['id']] = slug
    return rosters, legacy


def _static_fallback_rosters():
    """Used only when the courses API is unreachable. Two minimum intakes
    so the cron job never stalls completely."""
    rosters = {
        'cork-mat-260606': {
            'label': 'Cork Pilates · Bi-Weekly Saturdays · 6 Jun 2026',
            'location_col': 'Pilates Course Location',
            'start_col': 'Pilates Course Start Date',
            'location': ['Cork', 'Cork City'],
            'start_date_iso': '2026-06-06',
            'start_date_formats': ['6-6-2026', '06-06-2026', '2026-06-06'],
            'status': 'unknown', 'season': 'S26',
        },
        'kerry-ref-260606': {
            'label': 'Kerry Reformer · 6 Jun 2026',
            'location_col': 'Reformer Course Location',
            'start_col': 'Reformer Course Start Date',
            'location': ['Kerry', 'Killarney', 'Killarney, Kerry'],
            'start_date_iso': '2026-06-06',
            'start_date_formats': ['6-6-2026', '06-06-2026', '2026-06-06'],
            'status': 'unknown', 'season': 'S26',
        },
    }
    legacy = {'c029': 'cork-mat-260606', 'c061': 'kerry-ref-260606'}
    return rosters, legacy


# Populated at startup by main() — kept module-global for back-compat with
# any callers that still poke at ROSTERS / LEGACY_ALIASES directly.
ROSTERS = {}
LEGACY_ALIASES = {}


DEFAULT_APPS_SCRIPT_URL = os.environ.get(
    'APPS_SCRIPT_URL',
    'https://script.google.com/macros/s/AKfycbwyOTtqMqLTIKG4PZuCDL-3gOiaTgPgS_X-0-B8ldqHtYyh5z329hKAOX62RMMogaDN/exec',
)


# ─── Live ONtraport pull ──────────────────────────────────────────────────

def fetch_live_contacts():
    """Live pull straight through the v2 finance-dashboard layer.

    Two steps, both server-side:
      1. discover_s26_contact_ids() — condition search on the 2026 year
         dropdown for PT, Pilates, and Reformer. Three queries.
      2. fetch_contacts_full(ids) — batch (50/req) and DECODE so dropdowns
         + dates come back as human strings ('Cork', '6-6-2026').

    Rows returned use the prettified column names ('Pilates Course
    Location', 'Pilates Course Start Date', etc.) — same shape as the
    .op_live_s26.csv snapshot, so match() can work on them directly.
    """
    print('  Discovering all 2026 contact IDs (server-side filter)…')
    ids = discover_s26_contact_ids()
    print(f'  Found {len(ids)} contact IDs.')
    print(f'  Fetching + decoding {len(ids)} full contact records…')
    rows = fetch_contacts_full(ids)
    print(f'  {len(rows)} contacts pulled (live, decoded).')
    return rows


def fetch_csv_contacts():
    """Fallback: read the .op_live_s26.csv snapshot (may be stale)."""
    candidates = [
        HERE / 'v2' / 'app' / '.op_live_s26.csv',
        HERE / '.op_live_s26.csv',
        *sorted(HERE.glob('S26_Finance_Report_DATA_as_of_*.csv'), reverse=True),
    ]
    for p in candidates:
        if p.exists():
            print(f'  Reading {p.name} (CSV fallback)')
            with open(p, encoding='utf-8-sig') as f:
                return list(csv.DictReader(f))
    return []


# ─── Match logic ──────────────────────────────────────────────────────────

def match(row, spec):
    """Does this contact belong in this course's register?

    Works on the DECODED row shape — already strings like 'Cork' and
    '6-6-2026', so it's just a plain text join.
    """
    loc = _norm_loc(row.get(spec['location_col']))
    if loc not in {_norm_loc(x) for x in spec['location']}:
        return False
    start = _norm_date(row.get(spec['start_col']))
    if start is None:
        return False
    want = {_norm_date(x) for x in spec['start_date_formats']}
    want.add(_norm_date(spec.get('start_date_iso')))
    return start in want


def full_name(row):
    name = (row.get('Name') or '').strip()
    if name:
        return name
    first = (row.get('firstname') or '').strip()
    last = (row.get('lastname') or '').strip()
    return f'{first} {last}'.strip()


def email_of(row):
    return (row.get('Email') or row.get('email') or '').strip().lower()


def phone_of(row):
    return (row.get('SMS Number') or row.get('sms_number') or '').strip()


# ─── Push to Apps Script ──────────────────────────────────────────────────

def post_attendance_save(apps_script_url, course_id, course_label, extras_rows, roster_seed_date=None):
    seed_rows = []
    if roster_seed_date:
        for e in extras_rows:
            seed_rows.append({
                'sessionId': '__roster_seed__',
                'sessionDate': roster_seed_date,
                'studentName': e.get('studentName', ''),
                'studentEmail': e.get('studentEmail', ''),
                'studentPhone': e.get('studentPhone', ''),
                'status': '',
                'notes': 'roster seed from ONtraport sync',
            })
    payload = {
        'op': 'attendanceSave',
        'courseId': course_id,
        'courseLabel': course_label,
        'tutorName': 'ONtraport Sync',
        'rows': seed_rows,
        'extras': extras_rows,
    }
    req = urllib.request.Request(
        apps_script_url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    # Apps Script cold-starts + larger rosters occasionally push past 30s.
    # Retry with backoff so a single slow call doesn't kill the whole sync.
    import time as _time
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return True, resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            return False, f'HTTP {e.code}: {e.read().decode("utf-8", errors="ignore")}'
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < 2:
                _time.sleep(5 * (attempt + 1))
                continue
    return False, f'URL error after retries: {last_err}'


# ─── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Sync ONtraport contacts → attendance rosters.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print matches without posting to Apps Script.')
    parser.add_argument('--course',
                        help='Comma-separated course ids/slugs to sync (default: all in ROSTERS).')
    parser.add_argument('--use-csv', action='store_true',
                        help='Read stale CSV snapshot instead of live ONtraport API.')
    parser.add_argument('--apps-script-url', default=DEFAULT_APPS_SCRIPT_URL,
                        help='Apps Script /exec URL (override per environment).')
    parser.add_argument('--include-past', action='store_true',
                        help='Also sync courses already ended (for historical backfill).')
    parser.add_argument('--only-status', choices=['active', 'future', 'past'],
                        help='Restrict sync to courses in this lifecycle state.')
    args = parser.parse_args()

    # Live course list from edu.imageft.ie/api/attendance-courses (single
    # source of truth = lib/data.js). Populates ROSTERS dynamically.
    global ROSTERS, LEGACY_ALIASES
    print('  Pulling course list from edu.imageft.ie/api/attendance-courses …')
    ROSTERS, LEGACY_ALIASES = fetch_rosters_from_api()
    print(f'  {len(ROSTERS)} courses in catalog')

    # ── Pull contacts (live or CSV fallback) ─────────────────────────────
    if args.use_csv:
        contacts = fetch_csv_contacts()
        if not contacts:
            print('ERROR: --use-csv set but no CSV found.', file=sys.stderr)
            sys.exit(2)
    else:
        try:
            contacts = fetch_live_contacts()
        except Exception as e:
            print(f'  Live fetch failed: {e}\n  Falling back to CSV…')
            contacts = fetch_csv_contacts()
            if not contacts:
                print('ERROR: live fetch failed and no CSV fallback available.', file=sys.stderr)
                sys.exit(2)

    # ── Resolve which courses to sync (slug or legacy id accepted) ───────
    if args.course:
        raw = [c.strip() for c in args.course.split(',')]
        courses_to_sync = [LEGACY_ALIASES.get(c, c) for c in raw]
    else:
        # Default: sync active + future intakes only. --include-past adds
        # historic ones (slow, lots of API calls — only for backfill).
        courses_to_sync = []
        for slug, spec in ROSTERS.items():
            st = spec.get('status', 'unknown')
            if args.only_status and st != args.only_status:
                continue
            if not args.include_past and st == 'past':
                continue
            courses_to_sync.append(slug)
        print(f'  {len(courses_to_sync)} courses will sync '
              f'(status filter: {args.only_status or "active+future"})')

    grand_total = 0
    for cid in courses_to_sync:
        spec = ROSTERS.get(cid)
        if not spec:
            print(f'  ✗ Unknown course id {cid}, skipping.')
            continue
        matches = [c for c in contacts if match(c, spec)]
        print(f'\n=== {spec["label"]} ({cid}) ===')
        print(f'  {len(matches)} matching contacts')
        # 2026-07-28 — explain an empty roster instead of skipping silently:
        # of the contacts at the RIGHT location, show the distinct raw date
        # strings they carry vs what this course expects, so date-format drift
        # is immediately visible (the usual cause of a 0-match).
        if not matches:
            want_locs = {_norm_loc(x) for x in spec['location']}
            same_loc = [c for c in contacts if _norm_loc(c.get(spec['location_col'])) in want_locs]
            raw_dates = sorted({(c.get(spec['start_col']) or '').strip() for c in same_loc if (c.get(spec['start_col']) or '').strip()})
            print(f'  ⚠ 0 matches. {len(same_loc)} contacts at this location; their raw start-dates seen: {raw_dates or "(none)"}')
            print(f'    expected date (any format): {spec.get("start_date_iso")}  ·  location aliases: {spec["location"]}')

        extras_rows = []
        for c in matches:
            name = full_name(c)
            email = email_of(c)
            phone = phone_of(c)
            if not email:
                print(f'    ⚠ skip "{name}" — no email')
                continue
            print(f'    + {name:32}  {email:40}  {phone}')
            extras_rows.append({
                'studentName': name,
                'studentEmail': email,
                'studentPhone': phone,
                'extras': {},
            })

        if args.dry_run:
            print(f'  (dry-run — not posting)')
            continue
        if not extras_rows:
            print(f'  Nothing to post.')
            continue
        ok, body = post_attendance_save(
            args.apps_script_url, cid, spec['label'], extras_rows,
            roster_seed_date=spec.get('start_date_iso'),
        )
        if ok:
            print(f'  ✓ posted {len(extras_rows)} → {body[:200]}')
            grand_total += len(extras_rows)
        else:
            print(f'  ✗ POST failed: {body[:200]}')

    print(f'\nTOTAL synced across {len(courses_to_sync)} course(s): {grand_total} contacts')


if __name__ == '__main__':
    main()
