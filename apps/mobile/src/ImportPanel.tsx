import { useEffect, useRef, useState } from 'react';
import { AppState, Pressable, Text, View } from 'react-native';
import * as DocumentPicker from 'expo-document-picker';
import type { GpxFile } from './api/explore';
import type { ImportState } from './importStore';
import { ImportStore } from './importStore';

export function ImportPanel({ store, enabled, fixtureMode, accountGeneration }: { store: ImportStore; enabled: boolean; fixtureMode: boolean; accountGeneration: number }) {
  const [state, setState] = useState<ImportState>(store.getState());
  const enabledRef = useRef(enabled); enabledRef.current = enabled;
  useEffect(() => {
    const unsubscribe = store.subscribe(setState);
    if (enabled && !fixtureMode) void store.loadPage(1);
    return unsubscribe;
  }, [store, enabled, fixtureMode, accountGeneration]);
  useEffect(() => {
    if (!enabled || fixtureMode) return;
    const timer = setInterval(() => { if (AppState.currentState === 'active') void store.pollForeground(); }, 2500);
    return () => clearInterval(timer);
  }, [store, enabled, fixtureMode]);

  const pickNew = async () => {
    if (!enabledRef.current || fixtureMode) return;
    const generation = store.getAccountGeneration();
    try {
      const result = await DocumentPicker.getDocumentAsync({ type: '*/*', multiple: true, copyToCacheDirectory: true });
      if (result.canceled || !enabledRef.current || generation !== store.getAccountGeneration()) return;
      const files: GpxFile[] = result.assets.map((file) => ({ uri: file.uri, name: file.name, mimeType: file.mimeType, size: file.size }));
      await store.createFromSelection(files);
    } catch { if (generation === store.getAccountGeneration()) store.setError('Could not open the file picker. Retry and select GPX or FIT files.'); }
  };
  const reselect = async (batchId: string, itemId: string) => {
    if (!enabledRef.current || fixtureMode) return;
    const generation = store.getAccountGeneration();
    try {
      const result = await DocumentPicker.getDocumentAsync({ type: '*/*', copyToCacheDirectory: true });
      if (result.canceled || !enabledRef.current || generation !== store.getAccountGeneration()) return;
      const asset = result.assets[0];
      if (asset) await store.reselect(batchId, itemId, { uri: asset.uri, name: asset.name, mimeType: asset.mimeType, size: asset.size });
    } catch { if (generation === store.getAccountGeneration()) store.setError('Could not reopen the picker. Select the listed file again.'); }
  };

  const canImport = enabled && !fixtureMode;
  return <View style={styles.card}>
    <Text style={styles.title}>Import GPX or FIT activities</Text>
    <Text style={styles.copy}>Select up to 20 files. Each file uploads separately; accepted activities keep processing on the server.</Text>
    <Pressable accessibilityRole="button" disabled={!canImport || state.uploadingBatchId !== null} onPress={() => void pickNew()} style={[styles.button, (!canImport || state.uploadingBatchId !== null) && styles.disabled]}>
      <Text style={styles.buttonText}>{fixtureMode ? 'Unavailable in fixture mode' : state.uploadingBatchId ? 'Uploading one file…' : 'Choose GPX or FIT files'}</Text>
    </Pressable>
    {state.error && <Text accessibilityRole="alert" style={styles.error}>{state.error}</Text>}
    {state.rejected.map((item, index) => <Text key={`${item.name}-${index}`} style={styles.error}>{item.name}: {item.reason}</Text>)}
    {state.pollingStopped && <Text style={styles.copy}>Status refresh paused after 30 checks. Refresh to continue.</Text>}
    {state.batches.map((batch) => <View key={batch.id} style={styles.batch}>
      <View style={styles.row}><Text style={styles.batchTitle}>Import · {new Date(batch.created_at).toLocaleString()}</Text>
        {batch.state === 'open' && batch.counts.awaiting_upload > 0 && <Pressable onPress={() => void store.stop(batch.id)}><Text style={styles.link}>Stop uploading</Text></Pressable>}
        {batch.state === 'stopped' && <Pressable disabled={state.uploadingBatchId !== null} onPress={() => void store.resume(batch.id)}><Text style={styles.link}>Resume uploading</Text></Pressable>}
      </View>
      <Text style={styles.copy}>{batch.counts.awaiting_upload} waiting · {batch.counts.queued} queued · {batch.counts.processing} processing · {batch.counts.succeeded} imported · {batch.counts.failed} failed · {batch.counts.deleted} deleted</Text>
      {batch.items.map((item) => <View key={item.id} style={styles.item}>
        <View style={styles.row}><Text style={styles.itemName} numberOfLines={1}>{item.name} · {item.format.toUpperCase()}</Text><Text style={item.status === 'failed' ? styles.error : styles.status}>{state.uploadingItemId === item.id ? 'uploading' : statusLabel(item.status)}{item.duplicate ? ' · duplicate' : ''}</Text></View>
        {item.error && <Text style={styles.error}>{item.error}</Text>}
        {item.status === 'awaiting_upload' && <><Text style={styles.copy}>Select this exact file to upload. If the app restarts before upload, you must select it again.</Text>
          <Pressable disabled={!canImport || state.uploadingBatchId !== null || batch.state !== 'open'} onPress={() => void reselect(batch.id, item.id)}><Text style={styles.link}>Select file</Text></Pressable></>}
        {item.status === 'failed' && item.source_id && <Pressable disabled={!canImport} onPress={() => void store.retry(batch.id, item.id)}><Text style={styles.link}>Retry import</Text></Pressable>}
      </View>)}
    </View>)}
    {state.batches.length < state.total && <Pressable disabled={!canImport || state.loading} onPress={() => void store.loadMore()}><Text style={styles.link}>{state.loading ? 'Loading…' : 'Load older imports'}</Text></Pressable>}
    {state.batches.length > 0 && <Pressable disabled={!canImport || state.loading} onPress={() => void store.refresh()}><Text style={styles.link}>Refresh import status</Text></Pressable>}
  </View>;
}

function statusLabel(status: string): string { return status.replaceAll('_', ' '); }
const styles = {
  card: { borderWidth: 1, borderColor: '#e6ebe5', borderRadius: 13, padding: 13, marginBottom: 10, gap: 7, backgroundColor: '#fff' },
  title: { color: '#31594c', fontSize: 13, fontWeight: '800' as const },
  copy: { color: '#7b8981', fontSize: 11, lineHeight: 16 },
  button: { backgroundColor: '#173e35', padding: 11, borderRadius: 9, alignItems: 'center' as const }, disabled: { opacity: 0.55 },
  buttonText: { color: '#fff', fontSize: 11, fontWeight: '800' as const },
  batch: { borderTopWidth: 1, borderTopColor: '#dce5db', paddingTop: 9, marginTop: 4, gap: 5 },
  row: { flexDirection: 'row' as const, justifyContent: 'space-between' as const, alignItems: 'center' as const, gap: 8 },
  batchTitle: { flex: 1, color: '#31594c', fontSize: 11, fontWeight: '700' as const },
  item: { backgroundColor: '#f4f6f3', borderRadius: 9, padding: 8, gap: 3 },
  itemName: { flex: 1, color: '#31594c', fontSize: 11, fontWeight: '700' as const },
  status: { color: '#65756c', fontSize: 10, textTransform: 'capitalize' as const },
  link: { color: '#31594c', fontSize: 11, paddingVertical: 4, fontWeight: '700' as const },
  error: { color: '#a33d32', fontSize: 10 },
};
