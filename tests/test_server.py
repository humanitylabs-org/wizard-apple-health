import importlib
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'tests'))
import samples  # noqa: E402

TMP = tempfile.mkdtemp(prefix='wah-test-')


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()
os.environ.update(DATA_DIR=TMP, PORT=str(PORT), APPLE_MAX_PUSH_BYTES=str(256 * 1024),
                  WIZARD_TRUSTED_HOSTS='box.tail1234.ts.net', WIZARD_TRUSTED_ORIGINS='https://box.tail1234.ts.net:8444')
server = importlib.import_module('server')
server.secure_files()
HTTPD = server.ThreadingHTTPServer(('127.0.0.1', PORT), server.Handler)
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()
BASE = f'http://127.0.0.1:{PORT}'


def req(method, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {'Content-Type': 'application/json'} if data is not None else {}
    h.update(headers or {})
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            b = resp.read()
            try:
                return resp.status, json.loads(b or b'null')
            except ValueError:
                return resp.status, b.decode()
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b or b'null')
        except ValueError:
            return e.code, b.decode()


def token():
    return req('GET', '/api/setup')[1]['header_value']


def push(payload, tok=None, extra=None):
    h = {'X-Wizard-Token': tok or token(), 'automation-name': 'Wizard Health', 'automation-aggregation': 'days',
         'automation-period': 'Since Last Sync', 'session-id': 'abc'}
    h.update(extra or {})
    return req('POST', '/wizard-apple-health/api/push', payload, h)


def days(**q):
    from urllib.parse import urlencode
    return req('GET', '/api/days' + ('?' + urlencode(q) if q else ''))[1]


def day(d):
    out = [x for x in days(**{'from': d, 'to': d})['days']]
    return out[0] if out else None


class Base(unittest.TestCase):
    def setUp(self):
        req('POST', '/api/disconnect', {})


class TestAuth(Base):
    def test_no_token_is_401(self):
        s, b = req('POST', '/api/push', samples.metrics_payload())
        self.assertEqual(s, 401)
        st = req('GET', '/api/status')[1]
        self.assertEqual(st['status'], 'not_configured')
        self.assertIn('missing', st['setup']['last_rejected']['reason'])
        self.assertIsNone(day('2026-10-30'))

    def test_bad_token_is_401(self):
        token()
        s, _ = push(samples.metrics_payload(), tok='whp_wrong')
        self.assertEqual(s, 401)
        self.assertEqual(days()['days'], [])
        self.assertIn('did not match', req('GET', '/api/status')[1]['setup']['last_rejected']['reason'])

    def test_bearer_also_works(self):
        s, _ = req('POST', '/api/push', samples.metrics_payload(), {'Authorization': 'Bearer ' + token()})
        self.assertEqual(s, 200)

    def test_rotate_invalidates_old(self):
        old = token()
        new = req('POST', '/api/token/rotate', {})[1]['header_value']
        self.assertNotEqual(old, new)
        self.assertEqual(push(samples.metrics_payload(), tok=old)[0], 401)
        self.assertEqual(push(samples.metrics_payload(), tok=new)[0], 200)

    def test_status_never_has_token(self):
        t = token()
        push(samples.metrics_payload())
        for path in ('/api/status', '/healthz', '/api/days'):
            self.assertNotIn(t, json.dumps(req('GET', path)[1]))
        mcp = req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'health_data_status', 'arguments': {}}})[1]
        self.assertNotIn(t, json.dumps(mcp))

    def test_disconnect_wipes_data_and_token(self):
        t = token()
        push(samples.metrics_payload())
        ds1 = req('GET', '/api/status')[1]['dataset']
        req('POST', '/api/disconnect', {})
        st = req('GET', '/api/status')[1]
        self.assertEqual(st['status'], 'not_configured')
        self.assertEqual(st['counts']['days'], 0)
        self.assertNotEqual(st['dataset'], ds1)
        self.assertEqual(push(samples.metrics_payload(), tok=t)[0], 401)
        for p in Path(TMP).rglob('*'):
            if p.is_file():
                self.assertNotIn(t.encode(), p.read_bytes())

    def test_unknown_host_refused(self):
        s, _ = req('POST', '/api/push', samples.metrics_payload(), {'Host': 'evil.example', 'X-Wizard-Token': token()})
        self.assertEqual(s, 403)

    def test_tailnet_host_any_port_accepted(self):
        s, _ = req('POST', '/wizard-apple-health/api/push', samples.metrics_payload(),
                   {'Host': 'box.tail1234.ts.net:8444', 'X-Wizard-Token': token(), 'Tailscale-User-Login': 'owner@example.com'})
        self.assertEqual(s, 200)


class TestLimits(Base):
    def test_oversize_is_413(self):
        big = samples.metrics_payload()
        big['pad'] = 'x' * (300 * 1024)
        s, b = push(big)
        self.assertEqual(s, 413)
        self.assertIn('Batch Requests', b['error'])
        self.assertEqual(days()['days'], [])

    def test_oversize_rejected_before_body_by_content_length(self):
        r = urllib.request.Request(BASE + '/api/push', data=b'{}', method='POST',
                                   headers={'Content-Type': 'application/json', 'X-Wizard-Token': token(), 'Content-Length': str(10 ** 9)})
        try:
            urllib.request.urlopen(r, timeout=5)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 413)

    def test_csv_is_415(self):
        s, b = req('POST', '/api/push', raw=b'a,b\n1,2', headers={'Content-Type': 'multipart/form-data; boundary=x', 'X-Wizard-Token': token()})
        self.assertEqual(s, 415)
        self.assertIn('JSON', b['error'])

    def test_not_hae_json_is_400(self):
        self.assertEqual(push({'hello': 1})[0], 400)
        self.assertEqual(req('POST', '/api/push', raw=b'{nope', headers={'X-Wizard-Token': token()})[0], 400)

    def test_get_push_is_405(self):
        self.assertEqual(req('GET', '/api/push')[0], 405)

    def test_days_box_only(self):
        self.assertEqual(req('GET', '/api/days', headers={'X-Forwarded-For': '1.2.3.4'})[0], 403)
        self.assertEqual(req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, {'Tailscale-User-Login': 'x'})[0], 403)


class TestMapping(Base):
    def test_full_day(self):
        s, b = push(samples.metrics_payload())
        self.assertEqual(s, 200)
        self.assertEqual(b['received']['days'], 1)
        self.assertEqual(b['received']['ignored'], ['dietary_water'])
        self.assertEqual(push(samples.workouts_payload())[0], 200)
        d = day('2026-10-30')
        self.assertIsNone(d['readiness'])
        self.assertEqual(d['hrv_ms'], 48.3)
        self.assertEqual(d['extras']['apple_health']['hrv_method'], 'SDNN')
        self.assertIn('RMSSD', d['extras']['apple_health']['hrv_note'])
        self.assertEqual(d['resting_hr_bpm'], 56)
        self.assertEqual(d['spo2_pct'], 97.0)
        self.assertEqual(d['respiratory_rate'], 14.8)
        self.assertEqual(d['skin_temp_c'], 35.12)
        self.assertEqual(d['steps'], 8500)
        self.assertEqual(d['load'], {'value': 34, 'max': None, 'label': 'Exercise minutes', 'scale_max': 60, 'unit': 'min'})
        self.assertEqual(d['calories_kcal'], 2163)
        self.assertEqual(d['extras']['apple_health']['active_energy_kcal'], 512)
        self.assertEqual((d['avg_hr_bpm'], d['max_hr_bpm']), (71, 163))
        sl = d['sleep']
        self.assertEqual((sl['asleep_h'], sl['in_bed_h'], sl['light_h'], sl['deep_h'], sl['rem_h'], sl['awake_h']),
                         (7.25, 7.9, 3.9, 1.15, 2.2, 0.4))
        self.assertIsNone(sl['need_h'])
        self.assertIsNone(sl['score'])
        self.assertEqual(sl['efficiency_pct'], 92)
        self.assertEqual(sl['start'], '2026-10-29T16:41:00.000Z')
        self.assertEqual(sl['end'], '2026-10-30T00:20:00.000Z')
        w = d['workouts'][0]
        self.assertEqual((w['sport'], w['minutes'], w['calories_kcal'], w['distance_m'], w['avg_hr_bpm'], w['max_hr_bpm']),
                         ('Outdoor Run', 42, 402, 7210, 151, 176))
        self.assertEqual(w['start'], '2026-10-30T00:00:00.000Z')

    def test_status_contract(self):
        st = req('GET', '/api/status')[1]
        self.assertEqual(st['contract'], 'wizard.health-data/1')
        self.assertEqual(st['source']['id'], 'wizard-apple-health')
        self.assertFalse(st['scales']['readiness']['available'])
        self.assertFalse(st['provides']['readiness'])
        self.assertEqual(st['scales']['load']['scale_max'], 60)
        self.assertEqual(st['scales']['load']['targets'], {'high': [30, 60], 'medium': [15, 30], 'low': [0, 15]})
        self.assertEqual(st['scales']['hrv']['method'], 'SDNN')
        push(samples.metrics_payload())
        st = req('GET', '/api/status')[1]
        self.assertEqual(st['status'], 'connected')
        self.assertEqual(st['counts'], {'days': 1, 'sleeps': 1, 'workouts': 0})
        self.assertEqual(st['setup']['last_push']['automation'], 'Wizard Health')

    def test_unaggregated_sleep_segments(self):
        self.assertEqual(push(samples.sleep_segments_payload())[0], 200)
        sl = day('2026-10-28')['sleep']
        self.assertEqual((sl['light_h'], sl['deep_h'], sl['rem_h'], sl['asleep_h'], sl['awake_h']), (4.75, 1.0, 1.5, 7.25, 0.25))
        self.assertEqual(sl['in_bed_h'], 7.5)
        self.assertEqual(len(sl['naps']), 1)
        self.assertEqual(sl['naps'][0]['asleep_h'], 0.5)

    def test_kj_and_fahrenheit_and_fraction_spo2(self):
        p = {'data': {'metrics': [
            {'name': 'active_energy', 'units': 'kJ', 'data': [{'qty': 2092, 'date': '2026-10-01 00:00:00 +0700'}]},
            {'name': 'basal_energy_burned', 'units': 'kJ', 'data': [{'qty': 4184, 'date': '2026-10-01 00:00:00 +0700'}]},
            {'name': 'blood_oxygen_saturation', 'units': '%', 'data': [{'qty': 0.96, 'date': '2026-10-01 00:00:00 +0700'}]},
            {'name': 'apple_sleeping_wrist_temperature', 'units': 'degF', 'data': [{'qty': 95.0, 'date': '2026-10-01 00:00:00 +0700'}]}]}}
        push(p)
        d = day('2026-10-01')
        self.assertEqual(d['calories_kcal'], 1500)
        self.assertEqual(d['energy_kj'], 6276)
        self.assertEqual(d['spo2_pct'], 96.0)
        self.assertEqual(d['skin_temp_c'], 35.0)

    def test_missing_values_are_null(self):
        push({'data': {'metrics': [{'name': 'step_count', 'units': 'count', 'data': [{'qty': 100, 'date': '2026-09-01 00:00:00 +0700'}]}]}})
        d = day('2026-09-01')
        for k in ('hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'sleep', 'load', 'calories_kcal', 'readiness'):
            self.assertIsNone(d[k], k)
        self.assertEqual(d['steps'], 100)

    def test_workouts_v1_without_id(self):
        p = {'data': {'workouts': [{'name': 'Walking', 'start': '2026-10-02 18:00:00 +0700', 'end': '2026-10-02 18:30:00 +0700',
                                    'duration': 1800, 'activeEnergy': {'qty': 120, 'units': 'kcal'},
                                    'distance': {'qty': 1.5, 'units': 'mi'}, 'avgHeartRate': {'qty': 101, 'units': 'bpm'}}]}}
        push(p)
        push(p)
        ws = day('2026-10-02')['workouts']
        self.assertEqual(len(ws), 1)
        self.assertEqual((ws[0]['minutes'], ws[0]['calories_kcal'], ws[0]['distance_m'], ws[0]['avg_hr_bpm']), (30, 120, 2414, 101))


class TestIdempotent(Base):
    def test_resend_same_day_merges(self):
        push(samples.metrics_payload(steps=4000, exercise=10))
        c1 = days()['cursor']
        push(samples.metrics_payload(steps=9100, exercise=41))     # later the same day: totals replace, never add
        d = day('2026-10-30')
        self.assertEqual((d['steps'], d['load']['value']), (9100, 41))
        self.assertEqual(req('GET', '/api/status')[1]['counts']['days'], 1)
        self.assertEqual([x['date'] for x in days(changed_since=c1)['days']], ['2026-10-30'])

    def test_identical_resend_does_not_move_cursor(self):
        push(samples.metrics_payload())
        push(samples.workouts_payload())
        c1 = days()['cursor']
        before = day('2026-10-30')
        push(samples.metrics_payload())
        push(samples.workouts_payload())
        self.assertEqual(days(changed_since=c1)['days'], [])
        self.assertEqual(day('2026-10-30'), before)
        self.assertEqual(len(before['workouts']), 1)

    def test_grouping_switch_never_double_counts(self):
        hourly = {'data': {'metrics': [{'name': 'step_count', 'units': 'count', 'data': [
            {'qty': 1000, 'date': f'2026-10-03 {h:02d}:00:00 +0700'} for h in range(8, 13)]}]}}
        push(hourly, extra={'automation-aggregation': 'hours'})
        self.assertEqual(day('2026-10-03')['steps'], 5000)
        push({'data': {'metrics': [{'name': 'step_count', 'units': 'count', 'data': [{'qty': 7000, 'date': '2026-10-03 00:00:00 +0700'}]}]}})
        self.assertEqual(day('2026-10-03')['steps'], 7000)

    def test_paging_and_cursor(self):
        for i in range(1, 6):
            push(samples.metrics_payload(day=f'2026-08-0{i}', prev=f'2026-07-3{i % 2}'))
        a = days(limit=2)
        self.assertEqual([x['date'] for x in a['days']], ['2026-08-01', '2026-08-02'])
        b = days(limit=2, page=a['next_page'])
        self.assertEqual([x['date'] for x in b['days']], ['2026-08-03', '2026-08-04'])
        self.assertEqual(b['cursor'], a['cursor'])
        self.assertEqual(req('GET', '/api/days?changed_since=zz')[0], 400)


class TestMcp(Base):
    def call(self, name, args=None):
        r = req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}})[1]
        return r['result']['structuredContent']

    def test_tools(self):
        names = [t['name'] for t in req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})[1]['result']['tools']]
        self.assertEqual(names, ['health_data_status', 'health_data_sync', 'health_data_days'])
        self.assertIn('No data has arrived', self.call('health_data_sync')['note'])
        push(samples.metrics_payload())
        s = self.call('health_data_sync')
        self.assertTrue(s['push_based'])
        self.assertEqual(s['last_push']['days'], 1)
        self.assertEqual(self.call('health_data_days', {'from': '2026-10-30'})['days'][0]['steps'], 8500)
        self.assertEqual(self.call('health_data_status')['status'], 'connected')


class TestScreen(Base):
    def test_setup_shape(self):
        s = req('GET', '/api/setup')[1]
        self.assertEqual(s['header_name'], 'X-Wizard-Token')
        self.assertTrue(s['header_value'].startswith('whp_'))
        self.assertEqual(s['push_url_hint'], 'https://box.tail1234.ts.net:8444/wizard-apple-health/api/push')
        self.assertIn('$0.99', s['prices'])
        self.assertIn('Sleep Analysis', s['metrics_text'])
        self.assertEqual(req('GET', '/wizard-apple-health/ping')[0], 200)

    def test_static(self):
        for f in ('/', '/app.js', '/styles.css', '/icon.svg'):
            r = urllib.request.urlopen(BASE + '/wizard-apple-health' + f, timeout=5)
            self.assertEqual(r.status, 200)

    def test_files_private(self):
        token()
        push(samples.metrics_payload())
        server.secure_files()
        self.assertEqual(os.stat(TMP).st_mode & 0o777, 0o700)
        for p in Path(TMP).iterdir():
            if p.is_file():
                self.assertEqual(p.stat().st_mode & 0o777, 0o600, p.name)


if __name__ == '__main__':
    unittest.main()
