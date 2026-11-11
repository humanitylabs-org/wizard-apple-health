"""Apple Health: a receiver for the Health Auto Export iOS app (contract wizard.health-data/1).

Apple Health (HealthKit) lives only on the owner's iPhone and Apple Watch; Apple has no cloud API. The third-party
iOS app Health Auto Export (Premium tier) can POST Apple Health data as JSON to a URL on a schedule ("REST API"
automation). This app is that URL: the owner's iPhone, on the same Tailscale network as the box, posts to
https://<box>.ts.net:<shell port>/wizard-apple-health/api/push with a per-box secret header the app generates and
shows on its own screen. Nothing is public: the box is tailnet-only and every push must carry the token.

Each push is size-limited, parsed, and merged into a local SQLite copy idempotently: a record is keyed by its metric
and period (or workout id), so a re-sent day replaces the earlier value instead of adding to it, and a record's
change sequence moves only when its data really changed. Other apps on the box (the Health app) read the copy
through the box-only health-data contract: GET /api/status and GET /api/days (docs/health-data-contract.md).

Stdlib only. The token lives in DATA_DIR (0600 files, 0700 folder); it is returned only to this app's own screen.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-apple-health'
NAME = 'Apple Health'
VERSION = '0.1.3'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8844')
HEADER = 'X-Wizard-Token'                       # the header name the owner enters in Health Auto Export
MAX_PUSH_BYTES = int(os.environ.get('APPLE_MAX_PUSH_BYTES', str(16 * 1024 * 1024)))
MAX_ITEMS = int(os.environ.get('APPLE_MAX_ITEMS', '200000'))
# An oversize body up to this size is read and dropped before the 413, so a proxy in front (Tailscale Serve) passes the
# 413 on instead of turning a half-read request into a 502.
DRAIN_BYTES = int(os.environ.get('APPLE_DRAIN_BYTES', str(256 * 1024 * 1024)))   # data points + workouts in one push
QUIET_HOURS = float(os.environ.get('APPLE_QUIET_HOURS', '48'))  # no push for this long: say so in status.error
SHELL_PORT = os.environ.get('APPLE_SHELL_PORT', '8444')
HAE_URL = 'https://apps.apple.com/us/app/health-auto-export-json-csv/id1115567069'
HAE_DOCS = 'https://help.healthyapps.dev/en/health-auto-export/automations/rest-api'
TAILSCALE_URL = 'https://apps.apple.com/us/app/tailscale/id1470499037'
PRICES = 'Premium is $0.99 a month, $5.99 a year or $24.99 once'
AUTOMATION_NAME = 'Wizard Health'
# Health Auto Export's own names (its metric picker), the snake_case name it sends, and what the app does with it.
METRICS = [
    ('Sleep Analysis', 'sleep_analysis', True), ('Heart Rate Variability', 'heart_rate_variability', True),
    ('Resting Heart Rate', 'resting_heart_rate', True), ('Active Energy', 'active_energy', True),
    ('Apple Exercise Time', 'apple_exercise_time', True), ('Step Count', 'step_count', True),
    ('Blood Oxygen Saturation', 'blood_oxygen_saturation', True), ('Respiratory Rate', 'respiratory_rate', False),
    ('Basal Energy Burned', 'basal_energy_burned', False), ('Heart Rate', 'heart_rate', False),
    ('Apple Sleeping Wrist Temperature', 'apple_sleeping_wrist_temperature', False),
    ('Walking + Running Distance', 'walking_running_distance', False),
]
KNOWN = {m[1] for m in METRICS}
SUM = {'active_energy', 'basal_energy_burned', 'apple_exercise_time', 'step_count', 'walking_running_distance'}
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
LOCK = threading.RLock()


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------- private storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def _write_secret(name, text):
    p = _private() / name
    tmp = p.with_name('.' + name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _shred(name):
    p = DATA / name
    try:
        size = p.stat().st_size
        with open(p, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
        p.unlink()
    except FileNotFoundError:
        pass


def _state():
    return _read('state.json')


def _update_state(**kv):
    with LOCK:
        s = _state()
        for k, v in kv.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        _write_secret('state.json', json.dumps(s))
        return s


def secure_files():
    """Runtipi loosens app-data permissions; tighten them again on every start."""
    db().close()
    for p in DATA.iterdir():
        if p.is_file():
            os.chmod(p, 0o600)


def db():
    path = _private() / 'apple-health.sqlite3'
    fresh = not path.exists()
    c = sqlite3.connect(path, timeout=15)
    if fresh:
        os.chmod(path, 0o600)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, day TEXT, start TEXT, data TEXT NOT NULL,
        grouping TEXT, push_seq INTEGER NOT NULL DEFAULT 0, changed_seq INTEGER NOT NULL DEFAULT 0, changed_at TEXT,
        PRIMARY KEY (kind, id));
      CREATE INDEX IF NOT EXISTS records_day ON records(day);
      CREATE INDEX IF NOT EXISTS records_changed ON records(changed_seq);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
    ''')
    return c


def _next_seq(c):
    n = int(json.loads((c.execute("SELECT data FROM meta WHERE key='seq'").fetchone() or ['0'])[0])) + 1
    c.execute("INSERT OR REPLACE INTO meta (key,data) VALUES ('seq',?)", (str(n),))
    return n


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _parse(s):
    """Health Auto Export dates are `yyyy-MM-dd HH:mm:ss Z` (2024-02-06 14:30:00 -0800); ISO 8601 also works."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S %z', '%Y-%m-%d %H:%M %z'):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        t = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _local_day(s):
    """The owner's local calendar day of a Health Auto Export date: its own first ten characters (the string carries
    the iPhone's local time and offset), never converted to UTC."""
    s = str(s or '').strip()[:10]
    return s if DATE_RE.match(s) else None


def _ago(iso):
    t = _parse(iso)
    return (_now() - t).total_seconds() if t else 1e12


# ---------------- the push token ----------------

def _token():
    with LOCK:
        t = _read('token.json')
        if not t.get('token'):
            t = {'token': 'whp_' + secrets.token_urlsafe(32), 'created_at': _iso(_now())}
            _write_secret('token.json', json.dumps(t))
        return t


def rotate_token():
    with LOCK:
        _shred('token.json')
        _update_state(rotated_at=_iso(_now()), last_rejected=None)
    _token()
    return setup_info()


def _token_ok(headers):
    want = _read('token.json').get('token')
    if not want:
        return False
    got = (headers.get(HEADER) or '').strip()
    if not got:
        auth = (headers.get('Authorization') or '').strip()
        got = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    return bool(got) and secrets.compare_digest(got.encode(), want.encode())


def _mask(v):
    v = str(v or '')
    return ('•' * 6 + v[-4:]) if len(v) > 8 else '•' * 6


def _push_url_hint():
    """The tailnet address the iPhone posts to, from the box's trusted host names (the screen prefers its own
    https address when it is opened through the tailnet)."""
    names = sorted({h.split(':')[0] for h in TRUSTED_HOSTS if h.split(':')[0].endswith('.ts.net')})
    return f'https://{names[0]}:{SHELL_PORT}{PREFIX}/api/push' if names else None


def setup_info():
    t = _token()
    return {'header_name': HEADER, 'header_value': t['token'], 'token_hint': _mask(t['token']), 'token_created_at': t['created_at'],
            'push_path': PREFIX + '/api/push', 'push_url_hint': _push_url_hint(), 'ping_path': PREFIX + '/ping',
            'hae_url': HAE_URL, 'hae_docs': HAE_DOCS, 'tailscale_url': TAILSCALE_URL, 'prices': PRICES,
            'automation_name': AUTOMATION_NAME, 'max_push_mb': round(MAX_PUSH_BYTES / 1048576),
            'metrics': [{'label': a, 'name': b, 'needed': c} for a, b, c in METRICS],
            'metrics_text': ', '.join(a for a, _, _ in METRICS),
            'settings': [['Automation type', 'REST API'], ['Data type', 'Health Metrics'], ['Export format', 'JSON'],
                         ['Export version', 'Version 2'], ['Summarize data', 'On'], ['Time grouping', 'Day'],
                         ['Date range', 'Since Last Sync'], ['Batch requests', 'On'], ['Sync cadence', 'Every 1 hour']],
            'workout_settings': [['Data type', 'Workouts'], ['Export version', 'Version 2'], ['Include route data', 'Off'],
                                 ['Include workout metrics', 'Off'], ['Date range', 'Since Last Sync'], ['Sync cadence', 'Every 1 hour']],
            'received': bool(_state().get('last_push'))}


# ---------------- parsing a push ----------------

def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) != float('inf') else None
    try:
        f = float(str(v).strip())
        return f if f == f and abs(f) != float('inf') else None
    except ValueError:
        return None


def _qty(o):
    """A Health Auto Export quantity: a bare number or {"qty": n, "units": u}."""
    if isinstance(o, dict):
        return _num(o.get('qty')), str(o.get('units') or '')
    return _num(o), ''


def _kcal(v, unit):
    if v is None:
        return None
    return v / 4.184 if unit.lower() in ('kj', 'kilojoule', 'kilojoules') else v


def _metres(v, unit):
    if v is None:
        return None
    u = unit.lower()
    return v * {'mi': 1609.344, 'km': 1000.0, 'yd': 0.9144, 'ft': 0.3048, 'm': 1.0}.get(u, 1.0)


def _celsius(v, unit):
    if v is None:
        return None
    return (v - 32) / 1.8 if unit.lower() in ('degf', '°f', 'f', 'fahrenheit') else v


def _hash(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:20]


def parse_push(body, grouping=None):
    """Health Auto Export JSON -> rows (kind, id, day, start, data). Raises Problem(400) on a payload that is not
    Health Auto Export JSON. Unknown metrics are counted and skipped."""
    if not isinstance(body, dict):
        raise Problem(400, 'Expected a JSON object from Health Auto Export.')
    d = body.get('data') if isinstance(body.get('data'), dict) else body
    metrics, workouts = d.get('metrics'), d.get('workouts')
    if metrics is None and workouts is None:
        raise Problem(400, 'This is not a Health Auto Export JSON export (no data.metrics or data.workouts).')
    if not isinstance(metrics or [], list) or not isinstance(workouts or [], list):
        raise Problem(400, 'data.metrics and data.workouts must be lists.')
    rows, ignored, n = [], set(), 0
    for m in metrics or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get('name') or '').strip().lower().replace(' ', '_')
        units = str(m.get('units') or '')
        pts = m.get('data') if isinstance(m.get('data'), list) else []
        n += len(pts)
        if n > MAX_ITEMS:
            raise Problem(413, f'Too many data points in one request (over {MAX_ITEMS}). Turn on Batch Requests in Health Auto Export.')
        if name not in KNOWN:
            if name:
                ignored.add(name)
            continue
        for p in pts:
            if not isinstance(p, dict):
                continue
            if name == 'sleep_analysis':
                r = _sleep_row(p, units)
            else:
                r = _metric_row(name, units, p)
            if r:
                rows.append(r)
    for w in workouts or []:
        n += 1
        if n > MAX_ITEMS:
            raise Problem(413, f'Too many records in one request (over {MAX_ITEMS}). Turn on Batch Requests in Health Auto Export.')
        r = _workout_row(w) if isinstance(w, dict) else None
        if r:
            rows.append(r)
    return rows, sorted(ignored)


def _metric_row(name, units, p):
    ts = p.get('date') or p.get('startDate')
    day = _local_day(ts)
    if not day:
        return None
    if name == 'heart_rate':
        v = {'min': _num(p.get('Min', p.get('min'))), 'avg': _num(p.get('Avg', p.get('avg'))), 'max': _num(p.get('Max', p.get('max')))}
        if v['avg'] is None and v['max'] is None:
            return None
    else:
        v = _num(p.get('qty'))
        if v is None:
            return None
    data = {'v': v, 'u': str(p.get('units') or units), 'src': str(p.get('source') or '')[:200] or None}
    return ('m:' + name, str(ts).strip(), day, _iso(_parse(ts)) if _parse(ts) else None, data)


def _sleep_row(p, units):
    """Aggregated sleep (one summary per night: totalSleep/core/deep/rem/inBed/sleepStart/sleepEnd) or one
    unaggregated segment (startDate/endDate/qty/value). A night belongs to the day it ended on (the day woken up)."""
    u = (str(units or p.get('units') or 'hr')).lower()
    scale = 1 / 60 if u in ('min', 'minutes') else 1 / 3600 if u in ('s', 'sec', 'seconds') else 1.0
    if p.get('value') is not None and (p.get('startDate') or p.get('endDate')):
        a, b = p.get('startDate'), p.get('endDate')
        day = _local_day(p.get('sleepEnd') or p.get('inBedEnd') or b)
        if not day or not _parse(a) or not _parse(b):
            return None
        hours = _num(p.get('qty'))
        if hours is None:
            hours = (_parse(b) - _parse(a)).total_seconds() / 3600
        else:
            hours *= scale
        data = {'stage': str(p.get('value')), 'h': round(hours, 4), 'start': _iso(_parse(a)), 'end': _iso(_parse(b)),
                'src': str(p.get('source') or '')[:200] or None}
        return ('s:sleep', f"{str(a).strip()}|{data['stage']}", day, data['start'], data)
    day = _local_day(p.get('sleepEnd') or p.get('inBedEnd') or p.get('date'))
    if not day:
        return None
    data = {k: (None if _num(p.get(k)) is None else round(_num(p.get(k)) * scale, 4))
            for k in ('totalSleep', 'asleep', 'core', 'deep', 'rem', 'awake', 'inBed')}
    if not any(v for v in data.values()):
        return None
    for k in ('sleepStart', 'sleepEnd', 'inBedStart', 'inBedEnd'):
        t = _parse(p.get(k))
        data[k] = _iso(t) if t else None
    data['src'] = str(p.get('source') or '')[:200] or None
    return ('m:sleep_analysis', day, day, data.get('sleepStart') or data.get('inBedStart'), data)


def _first(w, *names):
    for n in names:
        if w.get(n) is not None:
            return w[n]
    return None


def _workout_row(w):
    start, end = w.get('start'), w.get('end')
    ts = _parse(start)
    day = _local_day(start)
    if not ts or not day:
        return None
    te = _parse(end)
    dur = _num(w.get('duration'))
    if dur is None and te:
        dur = (te - ts).total_seconds()
    hr = w.get('heartRate') if isinstance(w.get('heartRate'), dict) else {}
    avg, _ = _qty(hr.get('avg') if hr else None)
    mx, _ = _qty(hr.get('max') if hr else None)
    if avg is None:
        avg, _ = _qty(_first(w, 'avgHeartRate', 'averageHeartRate'))
    if mx is None:
        mx, _ = _qty(w.get('maxHeartRate'))
    ae, aeu = _qty(_first(w, 'activeEnergyBurned', 'activeEnergy'))
    if isinstance(_first(w, 'activeEnergyBurned', 'activeEnergy'), list):   # v1 time series: add it up
        ae, aeu = sum(_num(x.get('qty')) or 0 for x in w['activeEnergy'] if isinstance(x, dict)), 'kcal'
    te_kcal, teu = _qty(w.get('totalEnergy'))
    dist, du = _qty(w.get('distance'))
    wid = str(w.get('id') or '').strip()[:80] or _hash(w.get('name'), start)
    data = {'sport': str(w.get('name') or 'Workout')[:80], 'start': _iso(ts), 'end': _iso(te) if te else None,
            'seconds': dur, 'active_kcal': _kcal(ae, aeu), 'total_kcal': _kcal(te_kcal, teu),
            'distance_m': _metres(dist, du), 'avg_hr': avg, 'max_hr': mx,
            'location': w.get('location'), 'intensity_met': _qty(w.get('intensity'))[0]}
    return ('w', wid, day, data['start'], data)


def _save(rows, grouping):
    """Upsert parsed rows. The key is (metric, period) or the workout id, so a re-sent day replaces the earlier
    value. changed_seq moves only when the stored data really changes."""
    days, now = set(), _iso(_now())
    with LOCK:
        c = db()
        with c:
            seq = _next_seq(c)
            for kind, rid, day, start, data in rows:
                c.execute('''INSERT INTO records (kind,id,day,start,data,grouping,push_seq,changed_seq,changed_at) VALUES (?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(kind,id) DO UPDATE SET day=excluded.day, start=excluded.start,
                               grouping=excluded.grouping, push_seq=excluded.push_seq,
                               changed_at=CASE WHEN records.data IS excluded.data AND records.day IS excluded.day
                                 THEN records.changed_at ELSE excluded.changed_at END,
                               changed_seq=CASE WHEN records.data IS excluded.data AND records.day IS excluded.day
                                 THEN records.changed_seq ELSE excluded.changed_seq END,
                               data=excluded.data''',
                          (kind, rid, day, start, json.dumps(data, sort_keys=True), grouping, seq, seq, now))
                days.add(day)
        c.close()
    return days


def receive(body, headers):
    """One authenticated push from Health Auto Export."""
    grouping = str(headers.get('automation-aggregation') or '')[:40] or None
    rows, ignored = parse_push(body, grouping)
    days = _save(rows, grouping)
    kinds = {}
    for r in rows:
        k = 'workouts' if r[0] == 'w' else 'sleep' if r[0] in ('m:sleep_analysis', 's:sleep') else 'points'
        kinds[k] = kinds.get(k, 0) + 1
    info = {'at': _iso(_now()), 'records': len(rows), 'points': kinds.get('points', 0), 'sleep': kinds.get('sleep', 0),
            'workouts': kinds.get('workouts', 0), 'days': len(days), 'first_day': min(days) if days else None,
            'last_day': max(days) if days else None, 'ignored': ignored[:20],
            'automation': str(headers.get('automation-name') or '')[:80] or None,
            'period': str(headers.get('automation-period') or '')[:40] or None, 'grouping': grouping}
    st = _state()
    _update_state(last_push=info, first_push_at=st.get('first_push_at') or info['at'], pushes=int(st.get('pushes') or 0) + 1,
                  last_rejected=None)
    return {'ok': True, 'received': {k: info[k] for k in ('records', 'points', 'sleep', 'workouts', 'days', 'ignored')}}


def reject(reason):
    _update_state(last_rejected={'at': _iso(_now()), 'reason': reason})


# ---------------- the health-data contract (wizard.health-data/1, see docs/health-data-contract.md) ----------------

CONTRACT = 'wizard.health-data/1'
SOURCE = {'id': APP_ID, 'provider': 'apple-health', 'name': NAME, 'app_path': PREFIX + '/'}
# Activity load = Apple Exercise minutes (the green Exercise ring): open-ended, so `max` is null and `scale_max` is the
# top of the drawing scale. Targets per readiness band anchored on Apple's default 30-minute Exercise goal, in the
# same shape as WHOOP's strain guidance (Fitbit's AZM uses the same shape): Ready = meet the goal up to double,
# So-so = a light day up to the goal, Run down = under 15, "too much" at more than 20 minutes above the band's top.
# Apple gives no readiness, so Health judges activity only when another source supplies readiness for that day.
EXERCISE_GOAL = 30
LOAD_SCALE_MAX = 60
LOAD_TARGETS = {'high': [30, 60], 'medium': [15, 30], 'low': [0, 15]}
LOAD_OVER = 20
LOAD_LABEL = 'Exercise minutes'
HRV_NOTE = ('Apple Watch measures HRV as SDNN. WHOOP, Oura, Polar and Fitbit report RMSSD, which reads differently, '
            'so compare Apple HRV only with itself.')
SCALES = {'readiness': {'label': 'Readiness', 'min': 0, 'max': 100, 'available': False,
                        'note': 'Apple Health has no readiness or recovery score; this source sends none.'},
          'sleep_score': {'label': 'Sleep score', 'min': 0, 'max': 100, 'available': False},
          'hrv': {'label': 'HRV (SDNN)', 'method': 'SDNN', 'unit': 'ms', 'note': HRV_NOTE},
          'load': {'label': LOAD_LABEL, 'short_label': 'Exercise', 'unit': 'min', 'min': 0, 'max': None,
                   'scale_max': LOAD_SCALE_MAX, 'daily_goal': EXERCISE_GOAL, 'targets': LOAD_TARGETS, 'over_margin': LOAD_OVER}}


def _r(v, n=1):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return int(round(v)) if n == 0 else round(v, n)


def _dataset():
    st = _state()
    if not st.get('dataset'):
        st = _update_state(dataset=secrets.token_hex(12))
    return st['dataset']


def _latest_group(rows):
    """Keep only the rows from the time grouping of the most recent push for this metric and day, so switching
    Health Auto Export from hours to days never counts a day twice."""
    if not rows:
        return rows
    top = max(rows, key=lambda r: r['push_seq'])
    return [r for r in rows if r['grouping'] == top['grouping']]


def _agg(name, rows):
    rows = _latest_group(rows)
    vals = [json.loads(r['data']) for r in rows]
    if name == 'heart_rate':
        avgs = [v['v']['avg'] for v in vals if v['v'].get('avg') is not None]
        mxs = [v['v']['max'] for v in vals if v['v'].get('max') is not None]
        return {'avg': sum(avgs) / len(avgs) if avgs else None, 'max': max(mxs) if mxs else None}
    nums = [(v['v'], v.get('u') or '') for v in vals if isinstance(v.get('v'), (int, float))]
    if not nums:
        return None
    if name in ('active_energy', 'basal_energy_burned'):
        return sum(_kcal(x, u) for x, u in nums), any(u.lower() == 'kj' for _, u in nums)
    if name == 'walking_running_distance':
        return sum(_metres(x, u or 'km') for x, u in nums)
    if name == 'apple_sleeping_wrist_temperature':
        xs = [_celsius(x, u) for x, u in nums]
        return sum(xs) / len(xs)
    if name == 'blood_oxygen_saturation':
        xs = [x * 100 if x <= 1.0 else x for x, _ in nums]
        return sum(xs) / len(xs)
    if name in SUM:
        return sum(x for x, _ in nums)
    return sum(x for x, _ in nums) / len(nums)


def _sessions(segs):
    """Unaggregated sleep segments -> nights: segments closer than 2 hours belong to the same sleep."""
    segs = sorted(segs, key=lambda s: s['start'])
    out, cur = [], []
    for s in segs:
        if cur and (_parse(s['start']) - max(_parse(x['end']) for x in cur)).total_seconds() > 7200:
            out.append(cur)
            cur = []
        cur.append(s)
    if cur:
        out.append(cur)
    nights = []
    for ss in out:
        by = {}
        for s in ss:
            by[s['stage'].lower()] = by.get(s['stage'].lower(), 0) + s['h']
        core, deep, rem = by.get('core'), by.get('deep'), by.get('rem')
        asleep = sum(x for x in (core, deep, rem, by.get('asleep'), by.get('unspecified')) if x)
        a, b = min(s['start'] for s in ss), max(s['end'] for s in ss)
        span = (_parse(b) - _parse(a)).total_seconds() / 3600
        nights.append({'totalSleep': asleep or None, 'core': core, 'deep': deep, 'rem': rem, 'awake': by.get('awake'),
                       'inBed': by.get('in bed') or span, 'sleepStart': a, 'sleepEnd': b})
    return nights


def _n_sleep(n, naps=()):
    if not n:
        return None
    core, deep, rem = n.get('core'), n.get('deep'), n.get('rem')
    stages = sum(x for x in (core, deep, rem) if x)
    asleep = n.get('totalSleep') or stages or n.get('asleep')
    a, b = _parse(n.get('sleepStart') or n.get('inBedStart')), _parse(n.get('sleepEnd') or n.get('inBedEnd'))
    span = (b - a).total_seconds() / 3600 if a and b and b > a else None
    in_bed = n.get('inBed') or span          # the Apple Watch alone writes no "In Bed": use the night's span
    awake = n.get('awake')
    if awake is None and in_bed and asleep and in_bed > asleep:
        awake = in_bed - asleep
    if not asleep and not in_bed:
        return None
    return {'start': _iso(a) if a else None, 'end': _iso(b) if b else None, 'asleep_h': _r(asleep, 2), 'in_bed_h': _r(in_bed, 2),
            'awake_h': _r(awake, 2), 'light_h': _r(core, 2), 'deep_h': _r(deep, 2), 'rem_h': _r(rem, 2), 'score': None,
            'efficiency_pct': _r(min(100, 100 * asleep / in_bed), 0) if asleep and in_bed else None,
            'respiratory_rate': None, 'need_h': None,
            'naps': [{'start': x.get('sleepStart'), 'end': x.get('sleepEnd'), 'asleep_h': _r(x.get('totalSleep'), 2)} for x in naps]}


def _n_workout(wid, w):
    secs = w.get('seconds')
    return {'id': wid, 'sport': w.get('sport'), 'start': w.get('start'), 'end': w.get('end'),
            'minutes': round(secs / 60) if isinstance(secs, (int, float)) else None, 'load': None,
            'avg_hr_bpm': _r(w.get('avg_hr'), 0), 'max_hr_bpm': _r(w.get('max_hr'), 0),
            'calories_kcal': _r(w.get('active_kcal'), 0), 'energy_kj': None, 'distance_m': _r(w.get('distance_m'), 0),
            'zones_min': None,
            'extras': {'apple_health': {'total_kcal': _r(w.get('total_kcal'), 0), 'location': w.get('location'),
                                        'intensity_met': _r(w.get('intensity_met'))}}}


def _n_day(day, rows):
    by = {}
    for r in rows:
        by.setdefault(r['kind'], []).append(r)
    m = {k[2:]: _agg(k[2:], v) for k, v in by.items() if k.startswith('m:') and k != 'm:sleep_analysis'}
    # sleep: the night summary from the most recent kind of push (aggregated summary or raw segments)
    agg = by.get('m:sleep_analysis') or []
    seg = by.get('s:sleep') or []
    nights = []
    if agg and (not seg or max(r['push_seq'] for r in agg) >= max(r['push_seq'] for r in seg)):
        nights = [json.loads(r['data']) for r in agg]
    elif seg:
        nights = _sessions([json.loads(r['data']) for r in seg])
    nights.sort(key=lambda n: n.get('totalSleep') or n.get('asleep') or n.get('inBed') or 0, reverse=True)
    sleep = _n_sleep(nights[0], nights[1:]) if nights else None
    ex = m.get('apple_exercise_time')
    load = {'value': _r(ex, 0), 'max': None, 'label': LOAD_LABEL, 'scale_max': LOAD_SCALE_MAX, 'unit': 'min'} if ex is not None else None
    active, a_kj = m.get('active_energy') or (None, False)
    basal, b_kj = m.get('basal_energy_burned') or (None, False)
    total = active + basal if active is not None and basal is not None else None
    hr = m.get('heart_rate') or {}
    works = sorted(((r['id'], json.loads(r['data'])) for r in by.get('w', [])), key=lambda x: x[1].get('start') or '')
    resp = m.get('respiratory_rate')
    if sleep and resp is not None:
        sleep['respiratory_rate'] = _r(resp)
    srcs = sorted({json.loads(r['data']).get('src') for r in rows if json.loads(r['data']).get('src')})
    extras = {'hrv_method': 'SDNN' if m.get('heart_rate_variability') is not None else None,
              'hrv_note': HRV_NOTE if m.get('heart_rate_variability') is not None else None,
              'readiness_note': 'Apple Health has no readiness score.',
              'load_basis': 'Apple Exercise minutes (Exercise ring), goal 30',
              'exercise_min': _r(ex, 0), 'active_energy_kcal': _r(active, 0), 'resting_energy_kcal': _r(basal, 0),
              'distance_m': _r(m.get('walking_running_distance'), 0), 'sources': srcs[:10],
              'via': 'Health Auto Export'}
    return {
        'date': day, 'updated_at': max((r['received'] for r in rows if r['received']), default=None), 'readiness': None,
        'hrv_ms': _r(m.get('heart_rate_variability')), 'resting_hr_bpm': _r(m.get('resting_heart_rate'), 0),
        'spo2_pct': _r(m.get('blood_oxygen_saturation')), 'skin_temp_c': _r(m.get('apple_sleeping_wrist_temperature'), 2),
        'skin_temp_delta_c': None, 'respiratory_rate': _r(resp), 'sleep': sleep, 'load': load,
        'calories_kcal': _r(total, 0), 'energy_kj': _r(total * 4.184, 0) if total is not None and (a_kj or b_kj) else None,
        'avg_hr_bpm': _r(hr.get('avg'), 0), 'max_hr_bpm': _r(hr.get('max'), 0),
        'steps': _r(m.get('step_count'), 0), 'workouts': [_n_workout(i, w) for i, w in works],
        'extras': {'apple_health': extras},
    }


def _build_days(dates):
    if not dates:
        return []
    with LOCK:
        c = db()
        try:
            marks = ','.join('?' * len(dates))
            by = {}
            for x in c.execute(f'SELECT kind, id, day, data, grouping, push_seq, changed_at FROM records WHERE day IN ({marks}) ORDER BY start', list(dates)):
                row = dict(x)
                row['received'] = row.pop('changed_at')
                by.setdefault(x['day'], []).append(row)
        finally:
            c.close()
    return [_n_day(day, by.get(day, [])) for day in dates]


def _page_token(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(',', ':')).encode()).decode().rstrip('=')


def _untoken(s):
    try:
        d = json.loads(base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)))
        if isinstance(d, dict):
            return d
    except (ValueError, TypeError):
        pass
    raise Problem(400, 'page is not a page token from this source.')


def _cursor_seq(s):
    if s in (None, ''):
        return 0
    m = re.fullmatch(r'a1\.(\d{1,15})', str(s))
    if not m:
        raise Problem(400, 'changed_since is not a cursor from this source.')
    return int(m.group(1))


def _seq_now(c):
    row = c.execute("SELECT data FROM meta WHERE key='seq'").fetchone()
    return int(json.loads(row['data'])) if row else 0


def _check_day(v, name):
    if v is None or v == '':
        return None
    if not DATE_RE.match(str(v)):
        raise Problem(400, f'{name} must be YYYY-MM-DD.')
    return str(v)


def contract_days(args):
    """GET /api/days: normalized day records, oldest first, paged; optionally only days changed since a cursor."""
    frm, to = _check_day(args.get('from'), 'from'), _check_day(args.get('to'), 'to')
    if frm and to and frm > to:
        raise Problem(400, 'from must be on or before to.')
    try:
        limit = max(1, min(500, int(args.get('limit') or 100)))
    except (TypeError, ValueError):
        raise Problem(400, 'limit must be a number.') from None
    page = _untoken(str(args['page'])) if args.get('page') else {}
    changed = 'changed_since' in args
    since = _cursor_seq(args.get('changed_since')) if changed else 0
    after = page.get('after') or ''
    with LOCK:
        c = db()
        try:
            hw = int(page.get('hw') or _seq_now(c))
            q, p = ['SELECT DISTINCT day FROM records WHERE day IS NOT NULL AND day>?'], [after]
            if frm:
                q.append('AND day>=?'); p.append(frm)
            if to:
                q.append('AND day<=?'); p.append(to)
            if changed:
                q.append('AND day IN (SELECT day FROM records WHERE changed_seq>? AND changed_seq<=?)')
                p += [since, hw]
            q.append('ORDER BY day LIMIT ?'); p.append(limit + 1)
            dates = [r['day'] for r in c.execute(' '.join(q), p)]
        finally:
            c.close()
    more = len(dates) > limit
    dates = dates[:limit]
    return {'contract': CONTRACT, 'source': APP_ID, 'dataset': _dataset(), 'days': _build_days(dates),
            'next_page': _page_token({'after': dates[-1], 'hw': hw}) if more else None, 'cursor': f'a1.{hw}'}


def _counts():
    with LOCK:
        c = db()
        span = c.execute('SELECT MIN(day) a, MAX(day) b, COUNT(DISTINCT day) n FROM records').fetchone()
        sleeps = c.execute("SELECT COUNT(DISTINCT day) n FROM records WHERE kind IN ('m:sleep_analysis','s:sleep')").fetchone()['n']
        works = c.execute("SELECT COUNT(*) n FROM records WHERE kind='w'").fetchone()['n']
        c.close()
    return span['a'], span['b'], span['n'], sleeps, works


def status_info():
    st = _state()
    lp = st.get('last_push')
    first, last, ndays, sleeps, works = _counts()
    state = 'connected' if lp else 'not_configured'
    err = None
    if lp and _ago(lp.get('at')) > QUIET_HOURS * 3600:
        err = (f'No data from your iPhone for {int(_ago(lp.get("at")) // 86400)} days. Unlock the iPhone, check Tailscale is on, '
               'and open Health Auto Export once.')
    rej = st.get('last_rejected')
    return {
        'contract': CONTRACT, 'source': SOURCE, 'status': state, 'version': VERSION,
        'provides': {'readiness': False}, 'account': None, 'dataset': _dataset(),
        'last_sync': lp.get('at') if lp else None, 'next_sync': None, 'sync_every_minutes': None, 'push_based': True,
        'history_from': first, 'latest_date': last,
        'counts': {'days': ndays, 'sleeps': sleeps, 'workouts': works},
        'error': err, 'scales': SCALES,
        # receiver state, for this app's own screen (never the token)
        'setup': {'received': bool(lp), 'last_push': lp, 'pushes': int(st.get('pushes') or 0),
                  'first_push_at': st.get('first_push_at'), 'last_rejected': rej, 'header_name': HEADER,
                  'token_created_at': _read('token.json').get('created_at')},
    }


def disconnect():
    """Wipe the copy and the token. A new token is made the next time the setup screen opens."""
    with LOCK:
        for p in list(DATA.glob('apple-health.sqlite3*')):
            p.unlink(missing_ok=True)
        _shred('state.json')     # also drops the dataset id, so readers know the copy is gone
        _shred('token.json')
    return status_info()


# ---------------- MCP ----------------

_NONE = {'type': 'object', 'properties': {}, 'additionalProperties': False}
TOOLS = [
    {'name': 'health_data_status', 'description': "Apple Health connector status (contract wizard.health-data/1): not_configured (no data received from the iPhone yet) or connected; when the iPhone last pushed (last_sync), history span and counts. Data arrives by push from the iPhone app Health Auto Export over Tailscale. Never returns the push token. If not set up, send the owner the app link [Apple Health](/wizard-apple-health/); its own screen walks him through it. Never ask for the token in chat. For health questions use the Health app's tools instead.",
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': True}},
    {'name': 'health_data_sync', 'description': 'Apple Health is push-based: the box cannot ask the iPhone for data. This reports the last push (when, how many records and days) and how to get fresh data (unlock the iPhone, Tailscale on, open Health Auto Export or run its Manual Export). Changes nothing.',
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': True, 'idempotentHint': True}},
    {'name': 'health_data_days', 'description': 'Raw contract read: normalized Apple Health day records (readiness always null: Apple has no score; HRV is SDNN, labelled in extras.apple_health; resting HR, SpO2, breathing rate, sleep with core/deep/REM stages, load = Apple Exercise minutes, calories, steps, workouts), oldest first, paged with next_page; pass changed_since=<cursor> to get only changed days. For owner questions prefer the Health app tools.',
     'inputSchema': {'type': 'object', 'properties': {
         'from': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'}, 'to': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
         'limit': {'type': 'integer', 'description': 'days per page, default 100, max 500'},
         'page': {'type': 'string', 'description': 'next_page from the previous reply'},
         'changed_since': {'type': 'string', 'description': 'cursor from an earlier reply; empty string = everything'}},
         'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
]


def sync_report():
    s = status_info()
    lp = (s.get('setup') or {}).get('last_push')
    if not lp:
        note = ('No data has arrived yet. The owner finishes setup in the Apple Health app (/wizard-apple-health/): '
                'Health Auto Export on his iPhone posts to this box over Tailscale.')
    else:
        note = (f"Push-based: the box cannot pull. Last push {lp.get('at')} with {lp.get('records')} records over "
                f"{lp.get('days')} days. Fresh data comes when the iPhone is unlocked with Tailscale on; opening Health "
                'Auto Export or its Manual Export sends it at once.')
    return {'status': s['status'], 'last_sync': s['last_sync'], 'last_push': lp, 'push_based': True, 'note': note,
            'error': s['error']}


def call(name, a):
    if name == 'health_data_status':
        return status_info()
    if name == 'health_data_sync':
        return sync_report()
    if name == 'health_data_days':
        return contract_days(a)
    raise KeyError(name)


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            try:
                v = call(p.get('name'), p.get('arguments') or {})
            except KeyError:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except Problem as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError, AttributeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
# A bare host name (no port) in WIZARD_TRUSTED_HOSTS is trusted on every shell port of that host.
TRUSTED_NAMES = {h.split(':')[0] for h in TRUSTED_HOSTS}
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
PING_HTML = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>Apple Health on Wizard</title><body style="font:1.1rem/1.5 system-ui;margin:2rem">'
             '<h1 style="font-size:1.4rem">Your iPhone can reach your Wizard.</h1>'
             '<p>Go back to Health Auto Export and finish the automation.</p>')


def _host_ok(host):
    host = (host or '').lower()
    return host in TRUSTED_HOSTS or host.split(':')[0] in TRUSTED_NAMES


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if not _host_ok(host):
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS and urlsplit(origin).netloc != host:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def forwarded(self):
        return any(self.headers.get(h) for h in FORWARDED)

    def send(self, code, body=None, kind='application/json', extra=None, close=False):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        if close:
            self.send_header('Connection', 'close')
            self.close_connection = True
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def _drop(self, n):
        while n > 0:
            got = self.rfile.read(min(n, 1 << 20))
            if not got:
                return
            n -= len(got)

    def raw_body(self, cap):
        """The request body, refusing more than `cap` bytes (Content-Length or chunked). A body over the cap but under
        DRAIN_BYTES is read and dropped first, so the 413 reaches the sender through a proxy."""
        if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
            out, total, over = bytearray(), 0, False
            while True:
                line = self.rfile.readline(64)
                try:
                    size = int(line.split(b';')[0].strip() or b'0', 16)
                except ValueError:
                    raise Problem(400, 'Bad chunked body.') from None
                if size == 0:
                    self.rfile.readline(64)
                    if over:
                        raise Problem(413, 'Request too large.')
                    return bytes(out)
                total += size
                if total > cap:
                    over = True
                    out = bytearray()
                    if total > DRAIN_BYTES:
                        raise Problem(413, 'Request too large.')
                    self._drop(size)
                else:
                    out += self.rfile.read(size)
                self.rfile.readline(4)
        try:
            size = int(self.headers.get('Content-Length', '0') or 0)
        except ValueError:
            raise Problem(400, 'Bad Content-Length.') from None
        if size < 0 or size > cap:
            if 0 < size <= DRAIN_BYTES:
                self._drop(size)
            raise Problem(413, 'Request too large.')
        return self.rfile.read(size) if size else b''

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise Problem(415, 'Use application/json.')
        try:
            d = json.loads(self.raw_body(65536) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query, keep_blank_values=True).items()}
        try:
            if path in ('/healthz', '/api/health'):
                s = status_info()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'status': s['status'],
                                       'connected': s['status'] == 'connected', 'last_sync': s['last_sync']})
            if path == '/api/status':
                return self.send(200, status_info())
            if path == '/api/setup':
                if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    return self.send(403, {'error': 'Cross-origin request refused.'})
                return self.send(200, setup_info())
            if path == '/api/days':
                if self.forwarded():
                    return self.send(403, {'error': 'Health data reads are box-only.'})
                return self.send(200, contract_days(query))
            if path == '/ping':
                return self.send(200, PING_HTML.encode(), 'text/html; charset=utf-8')
            if path == '/api/push':
                return self.send(405, {'error': 'Health Auto Export sends data with POST.'}, extra={'Allow': 'POST'})
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def push(self):
        """POST /api/push from Health Auto Export: token first (before reading the body), then size, then JSON."""
        if not _token_ok(self.headers):
            reject('The token did not match. Copy the header value again from this app.' if (self.headers.get(HEADER) or self.headers.get('Authorization'))
                   else f'The {HEADER} header was missing. Add it to the automation in Health Auto Export.')
            return self.send(401, {'error': 'Missing or wrong token.'}, extra={'WWW-Authenticate': f'Bearer realm="{APP_ID}"'}, close=True)
        ctype = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if ctype != 'application/json':
            reject('Health Auto Export sent CSV. Choose JSON as the export format.')
            return self.send(415, {'error': 'Choose JSON as the export format in Health Auto Export.'}, close=True)
        try:
            raw = self.raw_body(MAX_PUSH_BYTES)
        except Problem as e:
            if e.code == 413:
                reject(f'The export was over {round(MAX_PUSH_BYTES / 1048576)} MB. Turn on Batch Requests and turn off route data.')
            return self.send(e.code, {'error': e.message + (' Turn on Batch Requests in Health Auto Export.' if e.code == 413 else '')}, close=True)
        try:
            body = json.loads(raw or b'{}')
        except ValueError:
            reject('The export was not valid JSON.')
            return self.send(400, {'error': 'Invalid JSON.'})
        try:
            return self.send(200, receive(body, self.headers))
        except Problem as e:
            reject(e.message)
            return self.send(e.code, {'error': e.message})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/api/push':
                return self.push()
            if path == '/mcp':
                if self.forwarded():
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/token/rotate':
                return self.send(200, rotate_token())
            if path == '/api/sync':
                return self.send(200, sync_report())
            if path == '/api/disconnect':
                return self.send(200, disconnect())
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message}, close=e.code == 413)
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


def main():
    secure_files()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()

# ---- wizard.app-switches/1 ------------------------------------------------------------------------------------------
# Owner feature switches for the Tracker's Try it (wizard-os docs/APP_SWITCHES.md). Shared block: the same text ships in
# every Wizard app (template: wizard-app-manager templates/hello-wizard). Every new feature ships behind a switch, off by
# default: append it to SWITCHES with the issue it delivers, e.g.
#   {'id': 'compact-header', 'name': 'Compact top bar', 'description': '...', 'default': False,
#    'issues': ['humanitylabs-org/<repo>#20']}
# and gate the feature on switch_on('compact-header'). State lives on the box (DATA_DIR/switches.json), so the app's
# page, the Tracker (through the App Manager) and the agent see one state. GET/POST /api/switches, owner only: the
# box's Tailscale Serve owner login with X-Wizard-Switch: 1; never a bearer, agent grant or box-local host.
import json as _sw_json
import os as _sw_os
import re as _sw_re
import threading as _sw_threading
from pathlib import Path as _SwPath
from urllib.parse import urlsplit as _sw_urlsplit

SWITCHES = []
_SW_CONTRACT = 'wizard.app-switches/1'
_SW_ROUTE = _sw_re.compile(r'(?:/[a-z0-9][a-z0-9-]{0,62})?/api/switches/?')
_SW_LOCAL = {'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'}
_sw_lock = _sw_threading.Lock()


def _sw_file():
    return _SwPath(_sw_os.environ.get('WIZARD_SWITCHES_DIR') or _sw_os.environ.get('DATA_DIR') or '/data') / 'switches.json'


def _sw_saved():
    try:
        saved = _sw_json.loads(_sw_file().read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def switches_doc():
    saved = _sw_saved()
    return {'contract': _SW_CONTRACT, 'switches': [
        dict(s, on=saved[s['id']] if isinstance(saved.get(s['id']), bool) else bool(s.get('default'))) for s in SWITCHES]}


def switch_on(switch_id):
    return next((s['on'] for s in switches_doc()['switches'] if s['id'] == switch_id), False)


def _sw_set(switch_id, on):
    with _sw_lock:
        ids = {s['id'] for s in SWITCHES}
        saved = {k: v for k, v in _sw_saved().items() if k in ids and isinstance(v, bool)}
        saved[switch_id] = on
        f = _sw_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(_sw_json.dumps(saved))
        _sw_os.replace(tmp, f)
    return switches_doc()


def _sw_owner(h, write):
    hd = h.headers
    if hd.get('Authorization') or hd.get('X-Wizard-Switch') != '1':
        return False
    if (hd.get('Sec-Fetch-Site') or 'same-origin').lower() not in ('same-origin', 'none'):
        return False
    host = (hd.get('Host') or '').strip().lower()
    name = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
    if not name or name in _SW_LOCAL or '.' not in name:      # box-local and container hosts never prove the owner
        return False
    origin = hd.get('Origin')
    trusted = {o.strip() for o in (_sw_os.environ.get('WIZARD_TRUSTED_ORIGINS') or '').split(',') if o.strip()}
    if origin and _sw_urlsplit(origin).netloc.lower() != host and origin not in trusted:
        return False
    if write and not origin:
        return False
    logins = [x.strip() for x in (hd.get_all('Tailscale-User-Login') or [])]
    owners = {x.strip().lower() for x in (_sw_os.environ.get('WIZARD_OWNER_LOGINS') or '').split(',') if x.strip()}
    return len(logins) == 1 and '@' in logins[0] and (not owners or logins[0].lower() in owners)


def _sw_send(h, code, doc):
    body = _sw_json.dumps(doc).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _sw_handle(h):
    """Serve /api/switches (or /<app>/api/switches) and return True; False for every other request."""
    if not _SW_ROUTE.fullmatch(_sw_urlsplit(h.path).path):
        return False
    if h.command in ('GET', 'HEAD'):
        if not _sw_owner(h, False):
            _sw_send(h, 403, {'error': "Only this Wizard's owner can see these switches.", 'code': 'owner_only'})
        else:
            _sw_send(h, 200, switches_doc())
        return True
    if not _sw_owner(h, True):
        _sw_send(h, 403, {'error': "Only this Wizard's owner can change these switches.", 'code': 'owner_only'})
        return True
    try:
        if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            raise ValueError
        size = int(h.headers.get('Content-Length') or '0')
        if size <= 0 or size > 4096:
            raise ValueError
        body = _sw_json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or not isinstance(body.get('id'), str) or not isinstance(body.get('on'), bool) \
                or set(body) - {'id', 'on'}:
            raise ValueError
    except (ValueError, TypeError):
        _sw_send(h, 400, {'error': 'Send JSON {"id": "...", "on": true|false}', 'code': 'invalid_input'})
        return True
    if body['id'] not in {s['id'] for s in SWITCHES}:
        _sw_send(h, 404, {'error': 'No such switch', 'code': 'unknown_switch'})
        return True
    try:
        _sw_send(h, 200, _sw_set(body['id'], body['on']))
    except OSError:
        _sw_send(h, 500, {'error': 'storage_failure'})
    return True


def wizard_switches_install(handler_cls):
    """Route the switch endpoint ahead of the app's own GET/HEAD/POST handlers."""
    for method in ('do_GET', 'do_HEAD', 'do_POST'):
        orig = getattr(handler_cls, method, None)
        if getattr(orig, '_wizard_switches', False):
            continue

        def wrapped(self, _orig=orig):
            if _sw_handle(self):
                return None
            if _orig is None:
                return self.send_error(501)
            return _orig(self)
        wrapped._wizard_switches = True
        setattr(handler_cls, method, wrapped)
    return handler_cls
# ---- end wizard.app-switches/1 ---------------------------------------------------------------------------------------

wizard_switches_install(Handler)


if __name__ == '__main__':
    main()
