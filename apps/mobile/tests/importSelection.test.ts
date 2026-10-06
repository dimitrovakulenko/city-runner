import assert from 'node:assert/strict';
import test from 'node:test';
import { MAX_IMPORT_FILE_BYTES, MAX_IMPORT_FILES, prepareImportSelection } from '../src/importSelection';

test('accepts GPX and FIT extensions regardless of MIME and preserves local metadata', () => {
  const selection = prepareImportSelection([
    { uri: 'file://run', name: 'Morning.GPX', mimeType: 'application/octet-stream', size: 21 },
    { uri: 'file://fit', name: 'watch.fit', mimeType: 'application/octet-stream' },
  ]);
  assert.deepEqual(selection.accepted.map(({ name, kind }) => [name, kind]), [['Morning.GPX', 'gpx'], ['watch.fit', 'fit']]);
  assert.equal(selection.accepted[0]?.size, 21);
  assert.deepEqual(selection.rejected, []);
});

test('rejects archives and unknown extensions instead of uploading them', () => {
  const selection = prepareImportSelection([{ uri: 'u', name: 'runs.zip' }, { uri: 'u', name: 'run.fit.backup' }]);
  assert.equal(selection.accepted.length, 0);
  assert.deepEqual(selection.rejected.map((item) => item.reason), ['Choose a .gpx or .fit file.', 'Choose a .gpx or .fit file.']);
});

test('enforces 10 MiB per file but accepts unknown local size for server validation', () => {
  const selection = prepareImportSelection([
    { uri: 'u', name: 'limit.gpx', size: MAX_IMPORT_FILE_BYTES },
    { uri: 'u', name: 'oversize.fit', size: MAX_IMPORT_FILE_BYTES + 1 },
    { uri: 'u', name: 'unknown.gpx' },
  ]);
  assert.deepEqual(selection.accepted.map((item) => item.name), ['limit.gpx', 'unknown.gpx']);
  assert.equal(selection.rejected[0]?.name, 'oversize.fit');
});

test('rejects file names beyond the manifest contract limit', () => {
  const result = prepareImportSelection([{ uri: 'u', name: `${'x'.repeat(198)}.fit` }]);
  assert.equal(result.accepted.length, 0);
  assert.equal(result.rejected[0]?.reason, 'File names must be 200 characters or fewer.');
});

test('bounds a picker result to 20 files and reports every excess selection', () => {
  const files = Array.from({ length: MAX_IMPORT_FILES + 2 }, (_, index) => ({ uri: `u${index}`, name: `${index}.gpx` }));
  const selection = prepareImportSelection(files);
  assert.equal(selection.accepted.length, MAX_IMPORT_FILES);
  assert.deepEqual(selection.rejected.map((item) => item.name), ['20.gpx', '21.gpx']);
});
