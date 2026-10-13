import type { SelectedImportFile, ImportSelectionRejection, ImportFileKind } from './importSelection';

export const MAX_HISTORY_FILES = 1_000;
export const MAX_HISTORY_IMPORT_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_HISTORY_BYTES = 256 * 1024 * 1024;

export interface PreparedHistoryFile extends SelectedImportFile {
  size: number;
  sha256: string;
}

export interface HistoryImportSelection {
  accepted: PreparedHistoryFile[];
  rejected: ImportSelectionRejection[];
  totalBytes: number;
}

export function historyBasename(name: string): string {
  return (name.replace(/\\/g, '/').split('/').pop() ?? '')
    .replace(/[\p{C}\p{Z}]/gu, (character) => character === ' ' ? character : '').trim();
}

export function prepareHistoryImportSelection(files: PreparedHistoryFile[]): HistoryImportSelection {
  if (files.length < 1) return { accepted: [], rejected: [{ name: '', reason: 'Choose at least one GPX or FIT activity file.' }], totalBytes: 0 };
  if (files.length > MAX_HISTORY_FILES) {
    return { accepted: [], rejected: [{ name: '', reason: `A history import can contain at most ${MAX_HISTORY_FILES} files.` }], totalBytes: 0 };
  }
  const accepted: PreparedHistoryFile[] = [];
  const rejected: ImportSelectionRejection[] = [];
  let totalBytes = 0;
  for (const file of files) {
    const name = historyBasename(file.name);
    const extension = name.toLowerCase().match(/\.([^.]+)$/)?.[1];
    const kind: ImportFileKind | null = extension === 'gpx' || extension === 'fit' ? extension : null;
    if (!name || Array.from(name).length > 200) {
      rejected.push({ name: file.name, reason: 'File names must be 1 to 200 characters.' });
    } else if (!kind || kind !== file.kind) {
      rejected.push({ name: name || file.name, reason: 'Choose a matching .gpx or .fit activity file.' });
    } else if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > MAX_HISTORY_IMPORT_FILE_BYTES) {
      rejected.push({ name, reason: 'Activity files must be between 1 byte and 10 MiB.' });
    } else if (!/^[a-f0-9]{64}$/.test(file.sha256)) {
      rejected.push({ name, reason: 'Could not verify this file. Select it again.' });
    } else {
      totalBytes += file.size;
      accepted.push({ ...file, name, kind, size: file.size, sha256: file.sha256 });
    }
  }
  if (totalBytes > MAX_HISTORY_BYTES) {
    return { accepted: [], rejected: [{ name: '', reason: 'A history import cannot exceed 256 MiB of activity files.' }], totalBytes };
  }
  return { accepted, rejected, totalBytes };
}

export function samePreparedHistoryFile(file: PreparedHistoryFile, candidate: PreparedHistoryFile): boolean {
  return historyBasename(file.name) === historyBasename(candidate.name) && file.kind === candidate.kind &&
    file.size === candidate.size && file.sha256 === candidate.sha256;
}
