import { BlobReader, ZipReader, ERR_UNSAFE_FILENAME, type FileEntry, type EntryMetaData } from '@zip.js/zip.js';
import { historyBasename, MAX_HISTORY_FILES, MAX_HISTORY_BYTES, MAX_HISTORY_IMPORT_FILE_BYTES } from '../../mobile/src/historyImportSelection';

class PreparationError extends Error {}

export const MAX_ZIP_BYTES = 64 * 1024 * 1024;
export const MAX_ZIP_ENTRIES = 10_000;
export interface PreparedBrowserFile { file: File; kind: 'gpx' | 'fit'; size: number; sha256: string }
export interface BrowserHistorySelection { files: PreparedBrowserFile[]; skipped: number }
export interface PreparationProgress { current: number; total: number }

const kindOf = (name: string): 'gpx' | 'fit' | null => {
  const extension = name.toLowerCase().split('.').pop();
  return extension === 'gpx' || extension === 'fit' ? extension : null;
};
const checkAbort = (signal: AbortSignal) => { if (signal.aborted) throw new DOMException('File preparation cancelled.', 'AbortError'); };

function safePath(path: string): void {
  const normalized = path.replace(/\\/g, '/');
  if (!normalized || normalized.startsWith('/') || /^[a-z]:/i.test(normalized) ||
      /[\p{C}]/u.test(normalized) || normalized.split('/').some((part) => part === '..' || part === '.')) {
    throw new PreparationError('This selection contains an unsafe file path. Choose a regular GPX/FIT export.');
  }
}

function validateCandidates(candidates: Array<{ name: string; size: number }>): void {
  if (!candidates.length) throw new PreparationError('No GPX or FIT activity files were found.');
  if (candidates.length > MAX_HISTORY_FILES) throw new PreparationError('Choose at most 1,000 activity files per history import.');
  let total = 0;
  for (const candidate of candidates) {
    const name = historyBasename(candidate.name);
    if (!name || Array.from(name).length > 200) throw new PreparationError('Activity file names must contain 1 to 200 characters.');
    if (!Number.isSafeInteger(candidate.size) || candidate.size < 1 || candidate.size > MAX_HISTORY_IMPORT_FILE_BYTES) {
      throw new PreparationError('Each activity file must contain 1 byte to 10 MiB.');
    }
    total += candidate.size;
  }
  if (total > MAX_HISTORY_BYTES) throw new PreparationError('Choose at most 256 MiB of activity files per history import.');
}

async function extractEntry(entry: FileEntry, signal: AbortSignal): Promise<File> {
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let size = 0;
  const destination = new WritableStream<Uint8Array>({
    write(chunk) {
      checkAbort(signal);
      size += chunk.byteLength;
      if (size > MAX_HISTORY_IMPORT_FILE_BYTES || size > entry.uncompressedSize) {
        throw new PreparationError('A ZIP entry exceeds its declared size or the 10 MiB activity limit.');
      }
      chunks.push(new Uint8Array(chunk));
    },
  });
  await entry.getData(destination, { signal, checkCrc32: true, checkOverlappingEntry: true, strictness: 'strict', useWebWorkers: true });
  checkAbort(signal);
  if (size !== entry.uncompressedSize) throw new PreparationError('A ZIP entry does not match its declared size.');
  return new File(chunks, historyBasename(entry.filename), { type: 'application/octet-stream' });
}

async function entryRange(file: File, entry: EntryMetaData, directoryOffset: number, signal: AbortSignal) {
  const header = new DataView(await file.slice(entry.offset, entry.offset + 30).arrayBuffer());
  checkAbort(signal);
  if (header.byteLength !== 30 || header.getUint32(0, true) !== 0x04034b50) throw new PreparationError('The ZIP contains an invalid local entry header.');
  const nameLength = header.getUint16(26, true), extraLength = header.getUint16(28, true);
  const dataStart = entry.offset + 30 + nameLength + extraLength, end = dataStart + entry.compressedSize;
  if (!Number.isSafeInteger(directoryOffset) || directoryOffset < 0 || directoryOffset > file.size || end > directoryOffset) {
    throw new PreparationError('The ZIP contains an entry outside its data area.');
  }
  const localName = new Uint8Array(await file.slice(entry.offset + 30, entry.offset + 30 + nameLength).arrayBuffer());
  if (localName.length !== entry.rawFilename.length || localName.some((byte, index) => byte !== entry.rawFilename[index])) {
    throw new PreparationError('The ZIP contains inconsistent entry names.');
  }
  return { start: entry.offset, end };
}

async function extractZip(file: File, signal: AbortSignal): Promise<{ files: File[]; skipped: number }> {
  if (file.size < 1 || file.size > MAX_ZIP_BYTES) throw new PreparationError('Choose one ZIP file of at most 64 MiB.');
  const reader = new ZipReader(new BlobReader(file), { strictness: 'strict' });
  const entries: FileEntry[] = [];
  const skippedEntries: FileEntry[] = [];
  const ranges: Array<{ start: number; end: number }> = [];
  let count = 0, skipped = 0;
  try {
    for await (const entry of reader.getEntriesGenerator()) {
      checkAbort(signal);
      if (++count > MAX_ZIP_ENTRIES) throw new PreparationError('Choose a ZIP containing at most 10,000 entries.');
      safePath(entry.filename);
      if (entry.symlink) throw new PreparationError('ZIP symbolic links are not supported. Choose regular activity files.');
      if (!Number.isSafeInteger(entry.offset) || entry.offset < 0 || entry.offset > file.size - 30 ||
          !Number.isSafeInteger(entry.compressedSize) || entry.compressedSize < 0 || entry.compressedSize > file.size - entry.offset - 30) {
        throw new PreparationError('The ZIP contains invalid entry offsets or sizes.');
      }
      ranges.push(await entryRange(file, entry, reader.directoryOffset!, signal));
      if (entry.directory) continue;
      if (!kindOf(entry.filename)) { skipped++; skippedEntries.push(entry); continue; }
      if (entry.encrypted) throw new PreparationError('Password-protected activity files are not supported. Export an unencrypted ZIP.');
      if (entry.compressionMethod !== 0 && entry.compressionMethod !== 8) throw new PreparationError('This ZIP uses unsupported compression. Export a standard ZIP.');
      entries.push(entry);
    }
    ranges.sort((first, second) => first.start - second.start);
    if (ranges.some((range, index) => index > 0 && ranges[index - 1].end > range.start)) throw new PreparationError('The ZIP contains overlapping entries.');
    validateCandidates(entries.map((entry) => ({ name: entry.filename, size: entry.uncompressedSize })));
    // Validate skipped local metadata and data descriptors while discarding compressed bytes.
    for (const entry of skippedEntries) {
      checkAbort(signal);
      await entry.getData(new WritableStream({ write() { checkAbort(signal); } }), {
        signal, passThrough: true, checkCrc32: false, strictness: 'strict', checkOverlappingEntry: true, useWebWorkers: false,
      });
    }
    const files: File[] = [];
    for (const entry of entries) { checkAbort(signal); files.push(await extractEntry(entry, signal)); }
    return { files, skipped };
  } catch (error) {
    if (signal.aborted || error instanceof Error && error.name === 'AbortError') throw error;
    if (error instanceof PreparationError) throw error;
    if (error instanceof Error && error.message === ERR_UNSAFE_FILENAME) throw new PreparationError('The ZIP contains an unsafe file path. Choose a regular activity export.');
    throw new PreparationError('The ZIP is corrupt or inconsistent. Export it again or select the activity folder.');
  } finally { await reader.close(); }
}

export async function prepareBrowserHistory(input: File[], signal: AbortSignal,
  onProgress: (progress: PreparationProgress) => void = () => {},
  folder = input.some((file) => Boolean(file.webkitRelativePath))): Promise<BrowserHistorySelection> {
  checkAbort(signal);
  const zip = folder ? [] : input.filter((file) => file.name.toLowerCase().endsWith('.zip'));
  let files: File[], skipped: number;
  if (zip.length) {
    if (zip.length !== 1 || input.length !== 1) throw new PreparationError('Choose one ZIP, or select loose GPX/FIT files or a folder.');
    ({ files, skipped } = await extractZip(zip[0], signal));
  } else {
    if (input.length > MAX_ZIP_ENTRIES) throw new PreparationError('Choose a folder containing at most 10,000 entries.');
    for (const file of input) safePath(file.webkitRelativePath || file.name);
    files = input.filter((file) => kindOf(file.name)); skipped = input.length - files.length;
    validateCandidates(files);
  }
  const prepared: PreparedBrowserFile[] = [];
  for (const file of files) {
    checkAbort(signal);
    onProgress({ current: prepared.length, total: files.length });
    const bytes = await file.arrayBuffer();
    checkAbort(signal);
    if (bytes.byteLength !== file.size) throw new PreparationError('An activity file changed while being prepared. Select it again.');
    const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (value) => value.toString(16).padStart(2, '0')).join('');
    checkAbort(signal);
    const basename = historyBasename(file.name);
    prepared.push({ file: basename === file.name ? file : new File([file], basename, { type: file.type }), kind: kindOf(basename)!, size: file.size, sha256 });
  }
  onProgress({ current: prepared.length, total: files.length });
  return { files: prepared, skipped };
}
