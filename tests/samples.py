"""Sample Health Auto Export payloads, shaped exactly like its documented JSON export (Export Version 2):
https://help.healthyapps.dev/en/health-auto-export/export-format (data.metrics / data.workouts),
.../export-format/health-metrics (snake_case names, {qty, date, source}, heart_rate Min/Avg/Max, sleep_analysis
aggregated and unaggregated), .../export-format/workouts (v2 id/name/start/end/duration + optional objects).
Values are made up; dates use the documented `yyyy-MM-dd HH:mm:ss Z` format in the iPhone's local zone (+0700)."""


def day_metrics(day, steps=8500, exercise=34, active=512.4, basal=1650.2, hrv=48.3, rhr=56, spo2=97.0, resp=14.8):
    d = f'{day} 00:00:00 +0700'
    return [
        {'name': 'step_count', 'units': 'count', 'data': [{'qty': steps, 'date': d, 'source': 'iPhone|Apple Watch'}]},
        {'name': 'apple_exercise_time', 'units': 'min', 'data': [{'qty': exercise, 'date': d, 'source': 'Apple Watch'}]},
        {'name': 'active_energy', 'units': 'kcal', 'data': [{'qty': active, 'date': d, 'source': 'Apple Watch'}]},
        {'name': 'basal_energy_burned', 'units': 'kcal', 'data': [{'qty': basal, 'date': d, 'source': 'Apple Watch'}]},
        {'name': 'heart_rate_variability', 'units': 'ms', 'data': [{'qty': hrv, 'date': d, 'source': 'Apple Watch'}]},
        {'name': 'resting_heart_rate', 'units': 'bpm', 'data': [{'qty': rhr, 'date': d, 'source': 'Apple Watch'}]},
        {'name': 'blood_oxygen_saturation', 'units': '%', 'data': [{'qty': spo2, 'date': d, 'source': 'Apple Watch'}]},
        {'name': 'respiratory_rate', 'units': 'count/min', 'data': [{'qty': resp, 'date': d, 'source': 'Apple Watch'}]},
        {'name': 'heart_rate', 'units': 'bpm', 'data': [{'date': d, 'Min': 48, 'Avg': 71, 'Max': 163, 'source': 'Apple Watch'}]},
        {'name': 'apple_sleeping_wrist_temperature', 'units': 'degC', 'data': [{'qty': 35.12, 'date': d, 'source': 'Apple Watch'}]},
        {'name': 'walking_running_distance', 'units': 'km', 'data': [{'qty': 6.21, 'date': d, 'source': 'iPhone|Apple Watch'}]},
        {'name': 'dietary_water', 'units': 'mL', 'data': [{'qty': 1500, 'date': d}]},
    ]


def sleep_aggregated(day, prev):
    return {'name': 'sleep_analysis', 'units': 'hr', 'data': [{
        'date': day, 'totalSleep': 7.25, 'asleep': 0, 'core': 3.9, 'deep': 1.15, 'rem': 2.2, 'awake': 0.4,
        'sleepStart': f'{prev} 23:41:00 +0700', 'sleepEnd': f'{day} 07:20:00 +0700',
        'inBed': 7.9, 'inBedStart': f'{prev} 23:20:00 +0700', 'inBedEnd': f'{day} 07:25:00 +0700', 'source': 'Apple Watch'}]}


def workout(day, wid='550e8400-e29b-41d4-a716-446655440000'):
    return {'id': wid, 'name': 'Outdoor Run', 'start': f'{day} 07:00:00 +0700', 'end': f'{day} 07:42:30 +0700',
            'duration': 2550, 'location': 'Outdoor', 'isIndoor': False,
            'activeEnergyBurned': {'qty': 402.5, 'units': 'kcal'}, 'totalEnergy': {'qty': 470.1, 'units': 'kcal'},
            'intensity': {'qty': 9.1, 'units': 'MET'}, 'distance': {'qty': 7.21, 'units': 'km'},
            'heartRate': {'min': {'qty': 98, 'units': 'bpm'}, 'avg': {'qty': 151, 'units': 'bpm'}, 'max': {'qty': 176, 'units': 'bpm'}},
            'maxHeartRate': {'qty': 176, 'units': 'bpm'}, 'avgHeartRate': {'qty': 151, 'units': 'bpm'}}


def metrics_payload(day='2026-10-30', prev='2026-10-29', **kw):
    return {'data': {'metrics': day_metrics(day, **kw) + [sleep_aggregated(day, prev)]}}


def workouts_payload(day='2026-10-30'):
    return {'data': {'workouts': [workout(day)]}}


def sleep_segments_payload():
    """Summarize Data off: one row per sleep stage segment (documented unaggregated shape)."""
    night = {'sleepStart': '2026-10-27 23:00:00 +0700', 'sleepEnd': '2026-10-28 06:30:00 +0700', 'inBed': 7.75,
             'inBedStart': '2026-10-27 22:45:00 +0700', 'inBedEnd': '2026-10-28 06:30:00 +0700'}
    segs = [('2026-10-27 23:00:00 +0700', '2026-10-28 00:30:00 +0700', 1.5, 'Core'),
            ('2026-10-28 00:30:00 +0700', '2026-10-28 01:30:00 +0700', 1.0, 'Deep'),
            ('2026-10-28 01:30:00 +0700', '2026-10-28 01:45:00 +0700', 0.25, 'Awake'),
            ('2026-10-28 01:45:00 +0700', '2026-10-28 03:15:00 +0700', 1.5, 'REM'),
            ('2026-10-28 03:15:00 +0700', '2026-10-28 06:30:00 +0700', 3.25, 'Core'),
            # an afternoon nap, more than 2 hours after the night
            ('2026-10-28 14:00:00 +0700', '2026-10-28 14:30:00 +0700', 0.5, 'Core')]
    data = []
    for a, b, q, v in segs:
        row = {'startDate': a, 'endDate': b, 'qty': q, 'value': v, 'deep': 0.0, 'rem': 0.0}
        row.update(night)
        if a.startswith('2026-10-28 14'):
            row.update(sleepStart='2026-10-28 14:00:00 +0700', sleepEnd='2026-10-28 14:30:00 +0700')
        data.append(row)
    return {'data': {'metrics': [{'name': 'sleep_analysis', 'units': 'hr', 'data': data}]}}
