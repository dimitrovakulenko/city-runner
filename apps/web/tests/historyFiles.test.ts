import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { BlobReader, BlobWriter, ZipWriter } from '@zip.js/zip.js';
import { prepareBrowserHistory, MAX_ZIP_ENTRIES } from '../src/historyFiles';
import { BrowserFiles } from '../src/files';

const signal = () => new AbortController().signal;
const gpx = '<gpx><trk><trkseg><trkpt lat="51" lon="3"><time>2026-10-13T08:00:00Z</time></trkpt></trkseg></trk></gpx>';
const hash = (value: string | Uint8Array) => createHash('sha256').update(value).digest('hex');
async function zip(entries: Array<{ name: string; value?: string | Uint8Array; directory?: boolean; unixMode?: number; password?: string }>, level = 0) {
  const writer = new ZipWriter(new BlobWriter(), { useWebWorkers: false });
  for (const entry of entries) await writer.add(entry.name, new BlobReader(new Blob([typeof entry.value === 'string' ? entry.value : new Uint8Array(entry.value ?? [])])), { ...entry, level });
  return new File([await writer.close()], 'history.zip');
}

test('folder preparation filters unrelated files, hashes unchanged bytes and sends only basenames', async () => {
  const file = new File([gpx], 'run.gpx');
  Object.defineProperty(file, 'webkitRelativePath', { value: 'export/activities/run.gpx' });
  const selected = await prepareBrowserHistory([file, new File(['private metadata'], 'profile.json')], signal());
  assert.equal(selected.skipped, 1);
  assert.equal(selected.files[0].file.name, 'run.gpx');
  assert.equal(selected.files[0].sha256, hash(gpx));
  assert.equal(await selected.files[0].file.text(), gpx);
});

test('a nested ZIP in a folder is skipped rather than opened', async () => {
  const file = new File([gpx], 'run.gpx'), nested = new File(['not an archive'], 'backup.zip');
  Object.defineProperty(file, 'webkitRelativePath', { value: 'export/run.gpx' });
  Object.defineProperty(nested, 'webkitRelativePath', { value: 'export/backup.zip' });
  const selected = await prepareBrowserHistory([file, nested], signal());
  assert.equal(selected.skipped, 1); assert.equal(selected.files.length, 1); assert.equal(selected.files[0].sha256, hash(gpx));
});

test('ZIP preparation preserves GPX/FIT bytes and distinct same-basename entries', async () => {
  const fit = new Uint8Array([14, 32, 0, 0, 0, 0, 0, 0, 46, 70, 73, 84, 0, 0]);
  const selected = await prepareBrowserHistory([await zip([
    { name: 'one/run.gpx', value: gpx }, { name: 'two/run.gpx', value: gpx + ' ' },
    { name: 'three/activity.fit', value: fit }, { name: 'profile.json', value: '{}' },
  ], 6)], signal());
  assert.equal(selected.skipped, 1);
  assert.deepEqual(selected.files.map((file) => file.file.name), ['run.gpx', 'run.gpx', 'activity.fit']);
  assert.deepEqual(selected.files.map((file) => file.sha256), [hash(gpx), hash(gpx + ' '), hash(fit)]);
  assert.deepEqual(new Uint8Array(await selected.files[2].file.arrayBuffer()), fit);
});

test('ZIP CRC corruption and truncated directories are rejected before returning a selection', async () => {
  const original = await zip([{ name: 'run.gpx', value: gpx }]);
  const bytes = new Uint8Array(await original.arrayBuffer());
  const header = new DataView(bytes.buffer);
  const payload = 30 + header.getUint16(26, true) + header.getUint16(28, true);
  bytes[payload] ^= 1;
  await assert.rejects(prepareBrowserHistory([new File([bytes], 'bad.zip')], signal()), /corrupt|inconsistent/i);
  await assert.rejects(prepareBrowserHistory([new File([bytes.slice(0, -12)], 'truncated.zip')], signal()), /corrupt|inconsistent/i);
});

test('ignored entries still require valid local metadata and data-area offsets', async () => {
  const original = await zip([{ name: 'run.gpx', value: gpx }, { name: 'ignored.txt', value: 'ignored' }]);
  const bytes = new Uint8Array(await original.arrayBuffer());
  const view = new DataView(bytes.buffer);
  const directories: number[] = [];
  for (let offset = 0; offset < bytes.length - 46; offset++) if (view.getUint32(offset, true) === 0x02014b50) directories.push(offset);
  assert.equal(directories.length, 2);
  view.setUint32(directories[1] + 42, directories[0], true);
  await assert.rejects(prepareBrowserHistory([new File([bytes], 'bad-offset.zip')], signal()), /invalid|data area|corrupt|inconsistent/i);
});

test('unsafe paths, symlinks and encrypted activity entries are rejected', async () => {
  for (const name of ['../run.gpx', '/run.gpx', 'C:/run.gpx']) {
    await assert.rejects(prepareBrowserHistory([await zip([{ name, value: gpx }])], signal()), /unsafe/i);
  }
  await assert.rejects(prepareBrowserHistory([await zip([{ name: 'run.gpx', value: 'target.gpx', unixMode: 0o120777 }])], signal()), /symbolic/i);
  await assert.rejects(prepareBrowserHistory([await zip([{ name: 'run.gpx', value: gpx, password: 'synthetic' }])], signal()), /Password-protected/i);
});

test('file-count, individual and aggregate byte limits fail before reading bytes', async () => {
  const unreadable = (name: string, size: number) => ({ name, size, arrayBuffer() { throw new Error('must not read'); } }) as unknown as File;
  await assert.rejects(prepareBrowserHistory(Array.from({ length: 1_001 }, (_, i) => unreadable(`${i}.gpx`, 1)), signal()), /1,000/);
  await assert.rejects(prepareBrowserHistory([unreadable('large.fit', 10 * 1024 * 1024 + 1)], signal()), /10 MiB/);
  await assert.rejects(prepareBrowserHistory(Array.from({ length: 26 }, (_, i) => unreadable(`${i}.gpx`, 10 * 1024 * 1024)), signal()), /256 MiB/);
  await assert.rejects(prepareBrowserHistory([unreadable('huge.zip', 64 * 1024 * 1024 + 1)], signal()), /64 MiB/);
  await assert.rejects(prepareBrowserHistory([unreadable('empty.gpx', 0)], signal()), /1 byte/);
});

test('actual ZIP output is bounded even when local and central sizes both lie', async () => {
  const archive = await zip([{ name: 'run.gpx', value: 'a'.repeat(1024 * 1024) }], 6);
  const bytes = new Uint8Array(await archive.arrayBuffer());
  const view = new DataView(bytes.buffer);
  view.setUint32(22, 10, true);
  for (let offset = 0; offset < bytes.length - 46; offset++) {
    if (view.getUint32(offset, true) === 0x02014b50) { view.setUint32(offset + 24, 10, true); break; }
  }
  await assert.rejects(prepareBrowserHistory([new File([bytes], 'bomb.zip')], signal()), /declared size|corrupt|inconsistent/i);
});

test('ZIP entry count is bounded even when entries contain no activity files', async () => {
  const archive = await zip(Array.from({ length: MAX_ZIP_ENTRIES + 1 }, (_, index) => ({ name: `${index}/`, directory: true })));
  await assert.rejects(prepareBrowserHistory([archive], signal()), /10,000/);
});

test('preparation cancellation and account reset abort private file work', async () => {
  const files = new BrowserFiles();
  const controller = files.beginPreparation();
  assert.throws(() => files.beginPreparation(), /current file selection/);
  const preparing = prepareBrowserHistory([new File([gpx], 'run.gpx')], controller.signal, () => files.clear());
  await assert.rejects(preparing, { name: 'AbortError' });
  assert.equal(files.isPreparing(), false);
  files.finishPreparation(controller);
  const next = files.beginPreparation(); files.finishPreparation(next);
  assert.equal(files.isPreparing(), false);
});
