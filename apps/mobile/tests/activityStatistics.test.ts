import assert from 'node:assert/strict';
import test from 'node:test';
import { formatActivityStatistics } from '../src/activityStatistics';

test('formats nullable GPS metrics without inventing values', () => {
  assert.deepEqual(formatActivityStatistics({ distance_m: null, duration_s: undefined, pace_s_per_km: null }), {
    distance: 'Unknown', time: 'Unknown', pace: 'Unknown',
  });
  assert.deepEqual(formatActivityStatistics({ distance_m: 0, duration_s: 0, pace_s_per_km: 0 }), {
    distance: '0.00 km', time: '0:00', pace: 'Unknown',
  });
});

test('rounds before splitting time and pace fields, including minute/hour rollover', () => {
  assert.deepEqual(formatActivityStatistics({ distance_m: 12_345, duration_s: 3_599.7, pace_s_per_km: 359.7 }), {
    distance: '12.35 km', time: '1:00:00', pace: '6:00/km',
  });
  assert.deepEqual(formatActivityStatistics({ distance_m: 4_999, duration_s: 59.6, pace_s_per_km: 59.6 }), {
    distance: '5.00 km', time: '1:00', pace: '1:00/km',
  });
  assert.equal(formatActivityStatistics({ distance_m: 4_000, duration_s: 3_661, pace_s_per_km: 4_321 }).time, '1:01:01');
});

test('small positive values do not round down to exact zero', () => {
  assert.deepEqual(formatActivityStatistics({ distance_m: 1, duration_s: 0.1, pace_s_per_km: 0.1 }), {
    distance: '<0.01 km', time: '<0:01', pace: '<0:01/km',
  });
});

test('negative and nonfinite values format as unknown', () => {
  assert.deepEqual(formatActivityStatistics({ distance_m: -1, duration_s: Number.NaN, pace_s_per_km: Number.POSITIVE_INFINITY }), {
    distance: 'Unknown', time: 'Unknown', pace: 'Unknown',
  });
});
