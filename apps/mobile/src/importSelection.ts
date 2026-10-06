export const MAX_IMPORT_FILES = 20;
export const MAX_IMPORT_FILE_BYTES = 10 * 1024 * 1024;

export interface LocalImportFile {
  uri: string;
  name: string;
  mimeType?: string | null;
  size?: number | null;
}

export type ImportFileKind = 'gpx' | 'fit';
export interface SelectedImportFile extends LocalImportFile { kind: ImportFileKind }
export interface ImportSelectionRejection { name: string; reason: string }
export interface ImportSelection { accepted: SelectedImportFile[]; rejected: ImportSelectionRejection[] }

export function prepareImportSelection(files: LocalImportFile[]): ImportSelection {
  const accepted: SelectedImportFile[] = [];
  const rejected: ImportSelectionRejection[] = [];
  for (const [index, file] of files.entries()) {
    if (index >= MAX_IMPORT_FILES) {
      rejected.push({ name: file.name, reason: `A batch can contain at most ${MAX_IMPORT_FILES} files.` });
      continue;
    }
    if (Array.from(file.name).length > 200) {
      rejected.push({ name: file.name, reason: 'File names must be 200 characters or fewer.' });
      continue;
    }
    const extension = file.name.toLowerCase().match(/\.([^.]+)$/)?.[1];
    if (extension !== 'gpx' && extension !== 'fit') {
      rejected.push({ name: file.name, reason: 'Choose a .gpx or .fit file.' });
      continue;
    }
    if (file.size != null && (file.size < 0 || file.size > MAX_IMPORT_FILE_BYTES)) {
      rejected.push({ name: file.name, reason: 'Files must be 10 MiB or smaller.' });
      continue;
    }
    accepted.push({ ...file, kind: extension });
  }
  return { accepted, rejected };
}
