import type { GpxFile } from '../../mobile/src/api/explore';

export class BrowserFiles {
  private files = new Map<string, File>();
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
  clear(): void { this.files.clear(); }
}
