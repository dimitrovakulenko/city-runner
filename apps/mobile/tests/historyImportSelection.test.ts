import assert from 'node:assert/strict';
import test from 'node:test';
import { MAX_HISTORY_BYTES, MAX_HISTORY_IMPORT_FILE_BYTES, MAX_HISTORY_FILES,
  historyBasename, prepareHistoryImportSelection, samePreparedHistoryFile } from '../src/historyImportSelection';
import type { PreparedHistoryFile } from '../src/historyImportSelection';

const file = (name: string, kind: 'gpx' | 'fit' = name.toLowerCase().endsWith('.fit') ? 'fit' : 'gpx', size = 10, sha256 = 'a'.repeat(64)): PreparedHistoryFile =>
  ({ uri: `memory:${name}`, name, kind, size, sha256 });

test('accepts prepared GPX/FIT files with required exact size and SHA-256', () => {
  const result = prepareHistoryImportSelection([file('run.gpx'), file('watch.fit')]);
  assert.equal(result.accepted.length, 2);
  assert.equal(result.totalBytes, 20);
  assert.deepEqual(result.rejected, []);
});

test('preserves duplicate basenames and matching is independent of selection order', () => {
  const first = file('folder-a/run.gpx', 'gpx', 10, '1'.repeat(64));
  const second = file('folder-b/run.gpx', 'gpx', 10, '1'.repeat(64));
  const selection = prepareHistoryImportSelection([first, second]);
  assert.deepEqual(selection.accepted.map((item) => item.name), ['run.gpx', 'run.gpx']);
  assert.equal(samePreparedHistoryFile(selection.accepted[0]!, selection.accepted[1]!), true);
  assert.equal(samePreparedHistoryFile(file('run.gpx', 'gpx', 10, '1'.repeat(64)), file('run.gpx', 'gpx', 11, '1'.repeat(64))), false);
  assert.equal(samePreparedHistoryFile(file('run.gpx', 'gpx', 10, '1'.repeat(64)), file('run.gpx', 'gpx', 10, '2'.repeat(64))), false);
});

test('sanitizes archive paths and server-unprintable basename characters consistently', () => {
  assert.equal(historyBasename('folder\\run\u200b.fit'), 'run.fit');
  assert.equal(historyBasename('folder/run.fit'), 'run.fit');
});

test('rejects malformed names, format, size and hash before manifest creation', () => {
  const result = prepareHistoryImportSelection([
    file('../run.gpx', 'fit'), file('x.txt'), file('bad.gpx', 'gpx', 0),
    file('large.fit', 'fit', MAX_HISTORY_IMPORT_FILE_BYTES + 1), file('hash.gpx', 'gpx', 10, 'A'.repeat(64)),
  ]);
  assert.equal(result.accepted.length, 0);
  assert.equal(result.rejected.length, 5);
});

test('rejects too many files and aggregate bytes rather than creating partial history', () => {
  const tooMany = prepareHistoryImportSelection(Array.from({ length: MAX_HISTORY_FILES + 1 }, (_item, index) => file(`run-${index}.gpx`)));
  assert.equal(tooMany.accepted.length, 0);
  assert.match(tooMany.rejected[0]?.reason ?? '', /at most 1000/);
  const count = Math.floor(MAX_HISTORY_BYTES / MAX_HISTORY_IMPORT_FILE_BYTES);
  const aggregate = prepareHistoryImportSelection(Array.from({ length: count }, (_item, index) => file(`run-${index}.gpx`, 'gpx', MAX_HISTORY_IMPORT_FILE_BYTES))
    .concat(file('overflow.gpx', 'gpx', MAX_HISTORY_IMPORT_FILE_BYTES)));
  assert.equal(aggregate.accepted.length, 0);
  assert.match(aggregate.rejected[0]?.reason ?? '', /256 MiB/);
});
