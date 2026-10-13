import type { GpxFile } from '../../mobile/src/api/explore';

export class BrowserFiles {
  private files = new Map<string, File>();
  private preparation: AbortController | null = null;
  isPreparing(): boolean { return this.preparation !== null; }
  beginPreparation(): AbortController {
    if (this.preparation) throw new Error('Wait for the current file selection to finish.');
    this.preparation = new AbortController(); return this.preparation;
  }
  finishPreparation(controller: AbortController): void { if (this.preparation === controller) this.preparation = null; }
  register(file: File): GpxFile {
    const uri = `web-file:${crypto.randomUUID()}`;
    this.files.set(uri, file);
    return { uri, name: file.name, mimeType: file.type, size: file.size };
  }
  form(selection: GpxFile): FormData {
    const file = this.files.get(selection.uri);
    if (!file) throw new Error('Select this file again before uploading.');
    const form = new FormData(); form.append('file', file, file.name); return form;
  }
  release(uri: string): void { this.files.delete(uri); }
  clear(): void { this.preparation?.abort(); this.preparation = null; this.files.clear(); }
}
