import { useEffect, useMemo, useState } from 'react';
import { ActivityIndicator, Pressable, SafeAreaView, ScrollView, StatusBar, StyleSheet, Text, TextInput, useWindowDimensions, View } from 'react-native';
import { Camera, GeoJSONSource, Layer, Map } from '@maplibre/maplibre-react-native';
import type { Feature, MultiLineString } from 'geojson';
import { ActivityStore } from './src/activityStore';
import { createActivityApi, noSessionStore } from './src/api/client';
import { fixtureApi } from './src/api/fixture';
import type { ActivityExplorerState } from './src/activityStore';

const fixtureMode = process.env.EXPO_PUBLIC_FIXTURE_MODE === 'true';
const api = fixtureMode ? fixtureApi : createActivityApi({
  baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? 'http://localhost:8000',
  sessionStore: noSessionStore,
});
const store = new ActivityStore(api);
const emptyState = store.getState();

export default function App() {
  const tablet = useWindowDimensions().width >= 850;
  const [state, setState] = useState<ActivityExplorerState>(emptyState);
  const [search, setSearch] = useState('');

  useEffect(() => {
    const unsubscribe = store.subscribe(setState);
    return unsubscribe;
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void store.loadPage(search), 250);
    return () => clearTimeout(timer);
  }, [search]);

  const track = useMemo<Feature<MultiLineString> | null>(() => {
    if (!state.detail) return null;
    const lines = state.detail.tracks.filter((segment) => segment.length > 1);
    return lines.length ? { type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: lines } } : null;
  }, [state.detail]);

  const isAuthError = state.listStatus === 'sign-in-required';
  const isFixture = fixtureMode;
  const selected = state.detail;
  const selecting = state.selectedId !== null;
  const failure = state.listStatus === 'offline' || state.listStatus === 'error';

  return <SafeAreaView style={[styles.safe, tablet && styles.tabletShell]}>
    <StatusBar barStyle="dark-content" />
    <View style={[styles.panel, tablet && styles.tabletPanel]}>
      <Text style={styles.brand}>CITY RUNNER</Text>
      <Text style={styles.heading}>{selected ? selected.name : selecting ? 'Activity detail' : 'Activities'}</Text>
      {selecting ? <>
        <Pressable onPress={() => store.clearSelection()}><Text style={styles.link}>← All activities</Text></Pressable>
        {selected && <View style={styles.detail}><Text style={styles.meta}>{selected.date} · {selected.type}</Text><Text style={styles.meta}>{selected.tracks.length} track segments</Text></View>}
        {state.detailStatus === 'loading' && <Message text="Loading activity…" loading />}
        {state.detailStatus === 'offline' || state.detailStatus === 'error' ? <Message text={state.detailError ?? 'Could not load activity.'} action="Retry" onPress={() => void store.retryDetail()} /> : null}
      </> : <>
        <TextInput value={search} onChangeText={setSearch} placeholder="Search activities" style={styles.search} accessibilityLabel="Search activities" />
        <ScrollView style={styles.list}>
          {state.listStatus === 'loading' && <Message text="Loading activities…" loading />}
          {state.listStatus === 'loading-more' && <ActivityIndicator />}
          {isAuthError && <Message text="Sign-in required. Native sign-in is not available yet." />}
          {failure && <Message text={state.listError ?? 'Connection failed.'} action="Retry" onPress={() => void store.retryList()} />}
          {state.listStatus === 'empty' && <Message text="No activities found." />}
          {state.items.map((activity) => <Pressable key={activity.id} style={styles.row} onPress={() => void store.selectActivity(activity.id)}>
            <Text style={styles.rowTitle}>{activity.name}</Text><Text style={styles.meta}>{activity.date} · {activity.type}</Text>
          </Pressable>)}
          {state.listStatus === 'ready' && state.items.length < state.total && <Pressable style={styles.more} onPress={() => void store.loadMore()}><Text style={styles.link}>Load more</Text></Pressable>}
        </ScrollView>
      </>}
      <Text style={styles.mode}>{isFixture ? 'FIXTURE MODE · LOCAL SAMPLE DATA' : isAuthError ? 'API · SIGN-IN REQUIRED' : 'API'}</Text>
    </View>
    <View style={styles.mapPanel}>
      <Map style={StyleSheet.absoluteFill} mapStyle="https://demotiles.maplibre.org/style.json">
        <Camera key={selected?.id ?? 'overview'} initialViewState={{ center: selected?.bounds ? [(selected.bounds[0][0] + selected.bounds[1][0]) / 2, (selected.bounds[0][1] + selected.bounds[1][1]) / 2] : [3.72, 51.055], zoom: selected ? 13 : 12 }} />
        {track && <GeoJSONSource id="selected-track" data={track}><Layer id="selected-track-line" type="line" style={{ lineColor: '#ef704f', lineWidth: 4 }} /></GeoJSONSource>}
      </Map>
      {!selected && <View style={styles.mapMessage}><Text style={styles.mapText}>{isAuthError ? 'Sign in to view your routes' : 'Select an activity to view its route'}</Text></View>}
    </View>
  </SafeAreaView>;
}

function Message({ text, action, onPress, loading }: { text: string; action?: string; onPress?: () => void; loading?: boolean }) {
  return <View style={styles.message}>{loading && <ActivityIndicator color="#ef704f" />}<Text style={styles.meta}>{text}</Text>{action && <Pressable onPress={onPress}><Text style={styles.link}>{action}</Text></Pressable>}</View>;
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: '#f4f6f3' },
  tabletShell: { flexDirection: 'row' },
  panel: { flex: 1, paddingHorizontal: 22, paddingTop: 24, backgroundColor: '#fbfcfa' },
  tabletPanel: { flex: 0, width: 390 },
  brand: { color: '#143e35', fontSize: 12, letterSpacing: 1.5, fontWeight: '900' },
  heading: { color: '#153c34', fontSize: 25, fontWeight: '800', marginTop: 22, marginBottom: 12 },
  link: { color: '#31594c', fontWeight: '700', paddingVertical: 8 },
  search: { backgroundColor: '#f0f3f0', borderRadius: 10, padding: 12, marginBottom: 8 },
  list: { flex: 1 },
  row: { borderBottomWidth: 1, borderBottomColor: '#eef1ed', paddingVertical: 13 },
  rowTitle: { color: '#24463b', fontWeight: '700', fontSize: 14, marginBottom: 5 },
  meta: { color: '#8d9b92', fontSize: 11, textTransform: 'capitalize' },
  more: { alignItems: 'center', padding: 8 },
  message: { alignItems: 'center', gap: 8, padding: 20 },
  detail: { paddingVertical: 16, gap: 7 },
  mode: { color: '#9ba79f', fontSize: 8, fontWeight: '800', letterSpacing: 1, paddingVertical: 12 },
  mapPanel: { flex: 1, minHeight: 230, backgroundColor: '#dce5db', overflow: 'hidden' },
  mapMessage: { position: 'absolute', alignSelf: 'center', top: 20, backgroundColor: '#fbfcfa', padding: 10, borderRadius: 10 },
  mapText: { color: '#31594c', fontSize: 11 },
});
