import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, SafeAreaView, ScrollView, StatusBar, StyleSheet, Text, TextInput, useWindowDimensions, View } from 'react-native';
import { Camera, GeoJSONSource, Layer, Map } from '@maplibre/maplibre-react-native';
import type { Feature, MultiLineString } from 'geojson';
import { ActivityStore } from './src/activityStore';
import { ExploreScreen } from './src/ExploreScreen';
import { ExploreStore } from './src/exploreStore';
import { CityExplorerScreen } from './src/CityExplorerScreen';
import { CityExplorerStore } from './src/cityExplorerStore';
import { createExploreApi, createFixtureExploreApi } from './src/api/explore';
import { createCityExplorerApi, createFixtureCityExplorerApi } from './src/api/cities';
import { fixtureApi } from './src/api/fixture';
import { AuthPanel } from './src/auth/AuthPanel';
import { mobileApi, authController } from './src/auth/runtime';
import { secureSessionStore } from './src/auth/secureSession';
import type { ActivityExplorerState } from './src/activityStore';
import { createCorrectionApi } from './src/api/corrections';
import { CorrectionStore } from './src/correctionStore';
import type { CorrectionState } from './src/correctionStore';

const fixtureMode = process.env.EXPO_PUBLIC_FIXTURE_MODE === 'true';
const apiBaseUrl = process.env.EXPO_PUBLIC_API_BASE_URL ?? 'http://localhost:8001';
const defaultMapStyle = 'https://tiles.openfreemap.org/styles/liberty';
const activityStore = new ActivityStore(fixtureMode ? fixtureApi : mobileApi);
const exploreStore = new ExploreStore(fixtureMode ? createFixtureExploreApi() : createExploreApi({ baseUrl: apiBaseUrl, sessionStore: secureSessionStore }));
const cityExplorerStore = new CityExplorerStore(fixtureMode ? createFixtureCityExplorerApi() : createCityExplorerApi({ baseUrl: apiBaseUrl, sessionStore: secureSessionStore }));
const correctionStore = new CorrectionStore(createCorrectionApi({ baseUrl: apiBaseUrl, sessionStore: secureSessionStore }), (operation, id) => {
  if (operation === 'activity-delete') activityStore.activityDeleted(id);
  void exploreStore.refreshAfterCorrection();
  void cityExplorerStore.refreshAfterCorrection();
});
const demoStyle = 'https://demotiles.maplibre.org/style.json';

export default function App() {
  const tablet = useWindowDimensions().width >= 850;
  const [page, setPage] = useState<'activities' | 'explore' | 'cities'>('activities');
  const [activity, setActivity] = useState<ActivityExplorerState>(activityStore.getState());
  const [correction, setCorrection] = useState<CorrectionState>(correctionStore.getState());
  const [deleteConfirmationFor, setDeleteConfirmationFor] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [hasSession, setHasSession] = useState<boolean | null>(fixtureMode ? true : null);
  const [accountGeneration, setAccountGeneration] = useState(0);
  const [mapError, setMapError] = useState(false);
  const [mapRevision, setMapRevision] = useState(0);
  const accountChangeRequest = useRef(0);
  const knownSessionToken = useRef<string | null | undefined>(undefined);
  const selected = activity.detail;
  const selecting = activity.selectedId !== null;
  const styleUrl = fixtureMode ? demoStyle : process.env.EXPO_PUBLIC_MAP_STYLE_URL?.trim() || defaultMapStyle;

  useEffect(() => activityStore.subscribe(setActivity), []);
  useEffect(() => correctionStore.subscribe(setCorrection), []);
  useEffect(() => { correctionStore.clearFeedback(); setDeleteConfirmationFor(null); }, [activity.selectedId]);
  useEffect(() => {
    if (page !== 'activities' || (!fixtureMode && (hasSession !== true || authController.getState().status !== 'signed-in'))) return;
    const timer = setTimeout(() => void activityStore.loadPage(search), 250);
    return () => clearTimeout(timer);
  }, [page, search, hasSession]);

  const changeAccount = useCallback(() => {
    const request = ++accountChangeRequest.current;
    setAccountGeneration((value) => value + 1);
    knownSessionToken.current = undefined;
    activityStore.reset();
    correctionStore.reset();
    exploreStore.reset();
    cityExplorerStore.reset();
    setSearch('');
    setHasSession(false);
    void secureSessionStore.getToken().then((token) => {
      if (request !== accountChangeRequest.current) return;
      const signedIn = authController.getState().status === 'signed-in' && Boolean(token);
      knownSessionToken.current = signedIn ? token : null;
      setHasSession(signedIn);
      if (signedIn) void activityStore.loadPage();
    }).catch(() => {
      if (request === accountChangeRequest.current) setHasSession(false);
    });
  }, []);

  useEffect(() => fixtureMode ? undefined : secureSessionStore.subscribe((token) => {
    if (token === knownSessionToken.current && authController.getState().status === 'signed-in') return;
    accountChangeRequest.current++;
    setAccountGeneration((value) => value + 1);
    knownSessionToken.current = token;
    activityStore.reset();
    correctionStore.reset();
    exploreStore.reset();
    cityExplorerStore.reset();
    setSearch('');
    const signedIn = authController.getState().status === 'signed-in' && Boolean(token);
    setHasSession(signedIn);
    if (signedIn) void activityStore.loadPage();
  }), [changeAccount]);

  const track = useMemo<Feature<MultiLineString> | null>(() => {
    if (!selected) return null;
    const segments = selected.tracks.filter((segment) => segment.length > 1);
    return segments.length ? { type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: segments } } : null;
  }, [selected]);
  const listFailure = activity.listStatus === 'offline' || activity.listStatus === 'error';
  const signedOut = activity.listStatus === 'sign-in-required' || hasSession === false;
  const correctionsEnabled = !fixtureMode && hasSession === true && authController.getState().status === 'signed-in';
  const deleteActivity = useCallback((id: string, generation: number) => {
    if (generation !== accountChangeRequest.current || !correctionsEnabled || authController.getState().status !== 'signed-in' || activityStore.getState().selectedId !== id) return;
    void correctionStore.deleteActivity(id);
  }, [correctionsEnabled]);
  const markStreetManually = useCallback((streetId: string, datasetId: string, reason: string, generation: number) => {
    const current = cityExplorerStore.getState();
    if (generation !== accountChangeRequest.current || !correctionsEnabled || authController.getState().status !== 'signed-in' || current.selectedStreetId !== streetId || current.selectedDatasetId !== datasetId) return;
    void correctionStore.markComplete(streetId, datasetId, reason);
  }, [correctionsEnabled]);
  const undoStreetManually = useCallback((streetId: string, datasetId: string, generation: number) => {
    const current = cityExplorerStore.getState();
    if (generation !== accountChangeRequest.current || !correctionsEnabled || authController.getState().status !== 'signed-in' || current.selectedStreetId !== streetId || current.selectedDatasetId !== datasetId) return;
    void correctionStore.undoManualCompletion(streetId, datasetId);
  }, [correctionsEnabled]);

  return <SafeAreaView style={styles.safe}>
    <StatusBar barStyle="dark-content" />
    {!fixtureMode && <AuthPanel controller={authController} onAccountChange={changeAccount} />}
    <View style={styles.navigation}>
      <Text style={styles.brand}>CITY RUNNER</Text>
      <View style={styles.tabs}>
        <Tab title="Activities" selected={page === 'activities'} onPress={() => setPage('activities')} />
        <Tab title="Explore" selected={page === 'explore'} onPress={() => setPage('explore')} />
        <Tab title="Cities" selected={page === 'cities'} onPress={() => setPage('cities')} />
      </View>
    </View>
    {page === 'explore'
      ? <ExploreScreen store={exploreStore} fixtureMode={fixtureMode} enabled={fixtureMode || hasSession === true} accountGeneration={accountGeneration} tablet={tablet} />
      : page === 'cities'
        ? <CityExplorerScreen store={cityExplorerStore} correctionStore={correctionStore} fixtureMode={fixtureMode} enabled={!fixtureMode && hasSession === true} accountGeneration={accountGeneration}
            onMarkComplete={markStreetManually} onUndoManual={undoStreetManually}
            onShowNode={(node, rule, generation) => {
              if (generation !== accountChangeRequest.current || hasSession !== true || authController.getState().status !== 'signed-in') return;
              exploreStore.setRule(rule); exploreStore.focusCoordinates(node.longitude, node.latitude); setPage('explore');
            }}
            onOpenActivity={(id, generation) => {
              if (generation !== accountChangeRequest.current || hasSession !== true || authController.getState().status !== 'signed-in') return;
              void activityStore.selectActivity(id); setPage('activities');
            }} />
      : <View style={[styles.activityShell, tablet && styles.activityTablet]}>
          {correction.message && <Text accessibilityRole="alert" style={styles.notice}>{correction.message}</Text>}
          <View style={[styles.panel, tablet && styles.tabletPanel]}>
            <Text style={styles.heading}>{selected?.name ?? (selecting ? 'Activity detail' : 'Activities')}</Text>
            {selecting ? <>
              <Pressable onPress={() => activityStore.clearSelection()}><Text style={styles.link}>← All activities</Text></Pressable>
              {activity.detailStatus === 'loading' && <Message text="Loading activity…" loading />}
              {activity.detailError && <Message text={activity.detailError} action="Retry" onPress={() => { if (activity.selectedId) void activityStore.selectActivity(activity.selectedId); }} />}
              {selected && <><Text style={styles.meta}>{selected.date} · {selected.type}</Text><Text style={styles.meta}>{selected.tracks.length} track segments preserved</Text>
                {correctionsEnabled && deleteConfirmationFor !== selected.id && <Pressable disabled={correction.pending !== null} onPress={() => { correctionStore.clearFeedback(); setDeleteConfirmationFor(selected.id); }}><Text style={styles.dangerLink}>Delete activity</Text></Pressable>}
                {correction.error && <Text accessibilityRole="alert" style={styles.error}>{correction.error}</Text>}
                {correctionsEnabled && deleteConfirmationFor === selected.id && <View style={styles.confirmation}>
                  <Text style={styles.meta}>Delete this activity? Progress it supported will be recalculated. Original-file cleanup is queued after deletion.</Text>
                  <View style={styles.confirmationActions}>
                    <Pressable disabled={correction.pending !== null} onPress={() => { setDeleteConfirmationFor(null); correctionStore.clearFeedback(); }}><Text style={styles.link}>Cancel</Text></Pressable>
                    <Pressable disabled={correction.pending !== null} onPress={() => deleteActivity(selected.id, accountGeneration)}><Text style={styles.dangerLink}>{correction.pending === 'activity-delete' ? 'Deleting…' : 'Confirm delete'}</Text></Pressable>
                  </View>
                </View>}
              </>}
            </> : <>
              <TextInput value={search} onChangeText={setSearch} placeholder="Search activities" style={styles.search} accessibilityLabel="Search activities" />
              <ScrollView style={styles.list}>
                {activity.listStatus === 'loading' && <Message text="Loading activities…" loading />}
                {signedOut && <Message text="Sign in to view your activities." />}
                {listFailure && <Message text={activity.listError ?? 'Could not load activities.'} action="Retry" onPress={() => void activityStore.retryList()} />}
                {activity.listStatus === 'empty' && <Message text="No activities found." />}
                {activity.items.map((item) => <Pressable key={item.id} style={styles.row} onPress={() => void activityStore.selectActivity(item.id)}>
                  <Text style={styles.rowTitle}>{item.name}</Text><Text style={styles.meta}>{item.date} · {item.type}</Text>
                </Pressable>)}
                {activity.listStatus === 'ready' && activity.items.length < activity.total && <Pressable style={styles.more} onPress={() => void activityStore.loadMore()}><Text style={styles.link}>Load more</Text></Pressable>}
                {activity.listStatus === 'loading-more' && <Message text="Loading more activities…" loading />}
              </ScrollView>
            </>}
            <Text style={styles.footer}>{fixtureMode ? 'FIXTURE MODE · SAMPLE ACTIVITIES' : signedOut ? 'SIGN-IN REQUIRED' : 'PRIVATE ACTIVITY HISTORY'}</Text>
          </View>
          <View style={styles.mapPanel}>
            <Map key={mapRevision} style={StyleSheet.absoluteFill} mapStyle={styleUrl} onDidFinishLoadingMap={() => setMapError(false)} onDidFailLoadingMap={() => setMapError(true)}>
              <Camera key={selected?.id ?? 'activity-overview'} initialViewState={{ center: selected?.bounds ? [(selected.bounds[0][0] + selected.bounds[1][0]) / 2, (selected.bounds[0][1] + selected.bounds[1][1]) / 2] : [3.72, 51.055], zoom: selected ? 13 : 12 }} />
              {track && <GeoJSONSource id="selected-activity" data={track}><Layer id="selected-activity-line" type="line" style={{ lineColor: '#ef704f', lineWidth: 4 }} /></GeoJSONSource>}
            </Map>
            {mapError && <View style={styles.mapUnavailable}><Text style={styles.rowTitle}>Map is unavailable</Text><Pressable onPress={() => setMapRevision((value) => value + 1)}><Text style={styles.link}>Retry map</Text></Pressable></View>}
            {selected && <View style={styles.mapCaption}><Text style={styles.rowTitle}>{selected.name}</Text><Text style={styles.meta}>{selected.tracks.length} separate GPS segments</Text></View>}
          </View>
        </View>}
  </SafeAreaView>;
}

function Tab({ title, selected, onPress }: { title: string; selected: boolean; onPress: () => void }) {
  return <Pressable onPress={onPress} style={[styles.tab, selected && styles.tabSelected]}><Text style={[styles.tabText, selected && styles.tabTextSelected]}>{title}</Text></Pressable>;
}

function Message({ text, action, onPress, loading }: { text: string; action?: string; onPress?: () => void; loading?: boolean }) {
  return <View style={styles.message}>{loading && <ActivityIndicator color="#ef704f" />}<Text style={styles.meta}>{text}</Text>{action && <Pressable onPress={onPress}><Text style={styles.link}>{action}</Text></Pressable>}</View>;
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: '#f4f6f3' },
  navigation: { paddingHorizontal: 18, paddingTop: 10, backgroundColor: '#fbfcfa' },
  brand: { color: '#143e35', fontSize: 12, fontWeight: '900', letterSpacing: 1.5 },
  tabs: { flexDirection: 'row', gap: 8, marginTop: 10 }, tab: { paddingVertical: 8, paddingHorizontal: 14, borderRadius: 9, backgroundColor: '#f0f3f0' },
  tabSelected: { backgroundColor: '#173e35' }, tabText: { color: '#607168', fontWeight: '700', fontSize: 11 }, tabTextSelected: { color: '#fff' },
  activityShell: { flex: 1 }, activityTablet: { flexDirection: 'row', padding: 16, gap: 14 },
  panel: { flex: 1, paddingHorizontal: 20, paddingTop: 16, backgroundColor: '#fbfcfa' }, tabletPanel: { flex: 0, width: 370, borderRadius: 18 },
  heading: { color: '#153c34', fontSize: 24, fontWeight: '800', marginBottom: 12 }, link: { color: '#31594c', fontWeight: '700', paddingVertical: 8 },
  search: { backgroundColor: '#f0f3f0', borderRadius: 10, padding: 12, marginBottom: 8 }, list: { flex: 1 }, row: { borderBottomWidth: 1, borderBottomColor: '#eef1ed', paddingVertical: 13 },
  rowTitle: { color: '#24463b', fontWeight: '700', fontSize: 13, marginBottom: 4 }, meta: { color: '#8d9b92', fontSize: 11 }, more: { alignItems: 'center', padding: 9 },
  message: { alignItems: 'center', padding: 18, gap: 8 }, footer: { color: '#9ba79f', fontSize: 8, fontWeight: '800', letterSpacing: 1, paddingVertical: 12 },
  mapPanel: { flex: 1, minHeight: 250, backgroundColor: '#dce5db', overflow: 'hidden' }, mapUnavailable: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 20, gap: 8 },
  mapCaption: { position: 'absolute', bottom: 15, left: 15, right: 15, backgroundColor: '#fbfcfa', borderRadius: 12, padding: 12 },
  dangerLink: { color: '#a33d32', fontWeight: '800', paddingVertical: 8 },
  error: { color: '#a33d32', fontSize: 11, paddingVertical: 6 },
  notice: { color: '#31594c', backgroundColor: '#eaf0e7', padding: 10, fontSize: 11 },
  confirmation: { backgroundColor: '#fbece9', borderRadius: 10, padding: 10, marginTop: 8 },
  confirmationActions: { flexDirection: 'row', justifyContent: 'flex-end', gap: 18 },
});
