import test from 'node:test';
import assert from 'node:assert/strict';
import { BrowserViewports, DEFAULT_POSITION } from '../src/viewports';

test('map position survives a new adapter and never leaks between account keys', () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
  const viewports = new BrowserViewports(storage);
  viewports.write('alice/a', { center: [4.35, 50.85], zoom: 16 });
  viewports.write('bob', { center: [3.7, 51], zoom: 12 });
  const restored = new BrowserViewports(storage);
  assert.deepEqual(restored.read('alice/a'), { center: [4.35, 50.85], zoom: 16 });
  assert.deepEqual(restored.read('bob'), { center: [3.7, 51], zoom: 12 });
  assert.deepEqual(restored.read('new-user'), DEFAULT_POSITION);
  assert.deepEqual(Object.keys(JSON.parse(values.get('city-runner.viewport.alice%2Fa')!)), ['center', 'zoom']);
  restored.forget('alice/a'); assert.deepEqual(restored.read('alice/a'), DEFAULT_POSITION);
  assert.deepEqual(restored.read('bob'), { center: [3.7, 51], zoom: 12 });
});

test('corrupt, nonfinite, out-of-range and inaccessible viewport storage falls back safely', () => {
  for (const raw of ['{', 'null', '[]', '{"center":[3,51],"zoom":"16"}', '{"center":[181,51],"zoom":16}', '{"center":[3,86],"zoom":16}', '{"center":[3,51,1],"zoom":16}', '{"center":[3,51],"zoom":20}']) {
    const viewports = new BrowserViewports({ getItem: () => raw, setItem: () => { throw Error('Denied'); } });
    assert.deepEqual(viewports.read('alice'), DEFAULT_POSITION);
    assert.doesNotThrow(() => viewports.write('alice', { center: [3, 51], zoom: 16 }));
  }
  let writes = 0;
  const unavailable = new BrowserViewports({ getItem: () => { throw Error('Denied'); }, setItem: () => { writes++; } });
  assert.deepEqual(unavailable.read('alice'), DEFAULT_POSITION);
  unavailable.write('alice', { center: [NaN, 51], zoom: 16 });
  unavailable.write('alice', { center: [3, 51], zoom: Infinity });
  assert.equal(writes, 0);
});
