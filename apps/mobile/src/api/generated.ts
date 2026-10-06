// Generated from backend/app/main.py OpenAPI. Do not edit by hand.

export type AccountResponse = {
  id: string;
};

export type ActivitySummary = {
  id: string;
  name: string;
  date: string;
  type: string;
  processed: boolean;
  unmapped_points: number;
};

export type ActivityPage = {
  items: Array<ActivitySummary>;
  page: number;
  page_size: number;
  total: number;
};

export type ActivityDetail = {
  id: string;
  name: string;
  date: string;
  type: string;
  processed: boolean;
  unmapped_points: number;
  tracks: Array<Array<[number, number]>>;
  timestamps: Array<Array<string | null>>;
  bounds: [[number, number], [number, number]] | null;
};

export type ChallengeRequest = {
  provider: 'google' | 'apple';
};

export type ChallengeResponse = {
  id: string;
  nonce: string;
  expires_at: string;
};

export type ExchangeRequest = {
  challenge_id: string;
  id_token: string;
};

export type ExchangeResponse = {
  token: string;
  expires_at: string;
  account: AccountResponse;
};

export type MeResponse = {
  id: string;
};

export type MapResponse = {
  bbox: [number, number, number, number];
  zoom: number;
  geography_state: 'supported' | 'geography_pending';
  pending_imports: number;
  dataset_truncated: boolean;
  datasets: Array<DatasetStatus>;
  cities: Array<CityScope>;
  tracks: Array<TrackFeature>;
  streets: Array<StreetFeature>;
  missing_nodes: Array<MissingNode>;
  node_state: 'ready' | 'pending' | 'not-requested' | 'geography_pending';
  limits: MapLimits;
};

export type ProgressResponse = {
  state: 'ready' | 'pending' | 'failed' | 'not-matched' | 'unsupported-geography';
  rule: 'normal' | 'strict';
  datasets: Array<ProgressDataset>;
  datasets_truncated: boolean;
  unmapped_points: number;
  pending_imports: number;
};

export type UploadResponse = {
  id: string;
  status: 'queued' | 'processing' | 'succeeded' | 'failed' | 'cancelled';
  job_id: string;
  duplicate: boolean;
};

export type UploadStatusResponse = {
  id: string;
  status: 'queued' | 'processing' | 'succeeded' | 'failed' | 'cancelled';
  activity_id: string | null;
  job_id: string | null;
  error: string | null;
};

export type CityPage = {
  dataset_id: string;
  dataset_state: 'active' | 'importing' | 'retired';
  rule: 'normal' | 'strict';
  coverage: DatasetCoverage;
  items: Array<CityItem>;
  page: number;
  page_size: number;
  total: number;
};

export type StreetPage = {
  dataset_id: string;
  city_id: string;
  dataset_state: 'active' | 'importing' | 'retired';
  rule: 'normal' | 'strict';
  coverage: DatasetCoverage;
  filter: 'all' | 'incomplete' | 'partial' | 'completed';
  filter_applied: boolean;
  items: Array<StreetItem>;
  page: number;
  page_size: number;
  total: number;
};

export type StreetDetail = {
  id: string;
  dataset_id: string;
  city_id: string;
  name: string;
  dataset_state: 'active' | 'importing' | 'retired';
  rule: 'normal' | 'strict';
  coverage: DatasetCoverage;
  visited_nodes: number | null;
  eligible_nodes: number | null;
  threshold: number | null;
  state: 'complete' | 'partial' | 'missing' | null;
  manual_completed: boolean;
  manual_reason: string | null;
  effective_state: 'complete' | 'partial' | 'missing' | null;
  remaining_nodes: Array<RemainingNode> | null;
  remaining_nodes_page: PageInfo;
};

export type ContributionPage = {
  dataset_id: string;
  street_id: string;
  dataset_state: 'active' | 'importing' | 'retired';
  coverage: DatasetCoverage;
  activities_available: boolean;
  activities: Array<ContributingActivity>;
  page: number;
  page_size: number;
  total: number | null;
};

export type ManualCompletionBody = {
  reason: string;
};

export type ImportBatchCreate = {
  request_id: string;
  files: Array<ImportBatchFile>;
};

export type ImportBatchResponse = {
  id: string;
  state: 'open' | 'stopped';
  created_at: string;
  items: Array<ImportBatchItemResponse>;
  counts: ImportBatchCounts;
};

export type ImportBatchPage = {
  items: Array<ImportBatchResponse>;
  page: number;
  page_size: number;
  total: number;
};

export type CityItem = {
  id: string;
  name: string;
  admin_level: string;
  visited_nodes: number | null;
  eligible_nodes: number | null;
  completed_streets: number | null;
  manual_completed_streets: number;
  effective_completed_streets: number | null;
  eligible_streets: number | null;
};

export type CityScope = {
  id: string;
  dataset_id: string;
  name: string;
  bounds: [number, number, number, number];
};

export type ContributingActivity = {
  id: string;
  name: string;
  date: string;
  type: string;
  supported_nodes: number;
};

export type DatasetCoverage = {
  status: 'ready' | 'pending' | 'failed';
  progress_revision: string;
  pending_sources: number;
  failed_sources: number;
  pending_imports: number;
  visited_node_count: number | null;
  unsupported_sample_count: number | null;
};

export type DatasetStatus = {
  id: string;
  region: string;
  state: 'ready' | 'pending' | 'failed' | 'not-matched';
  progress_revision: string | null;
  visited_node_count: number | null;
  unsupported_sample_count: number | null;
  pending_sources: number | null;
  failed_sources: number | null;
};

export type ImportBatchCounts = {
  awaiting_upload: number;
  queued: number;
  processing: number;
  succeeded: number;
  failed: number;
  deleted: number;
};

export type ImportBatchFile = {
  name: string;
  format: 'gpx' | 'fit';
};

export type ImportBatchItemResponse = {
  id: string;
  name: string;
  format: 'gpx' | 'fit';
  status: 'awaiting_upload' | 'queued' | 'processing' | 'succeeded' | 'failed' | 'deleted';
  source_id: string | null;
  activity_id: string | null;
  duplicate: boolean;
  error: string | null;
};

export type LayerLimit = {
  returned: number;
  limit: number;
  truncated: boolean;
};

export type MapLimits = {
  tracks: LayerLimit;
  streets: LayerLimit;
  missing_nodes: LayerLimit;
  cities: LayerLimit;
  points: LayerLimit;
  geometry_bytes: LayerLimit;
};

export type MissingNode = {
  node_id: string;
  dataset_id: string;
  longitude: number;
  latitude: number;
};

export type PageInfo = {
  page: number;
  page_size: number;
  total: number | null;
};

export type ProgressDataset = {
  dataset_id: string;
  region: string;
  state: 'ready' | 'pending' | 'failed' | 'not-matched';
  progress_revision: string | null;
  visited_node_count: number | null;
  unsupported_sample_count: number | null;
  pending_sources: number | null;
  failed_sources: number | null;
  eligible_streets: number | null;
  completed_streets: number | null;
  manual_completed_streets: number;
  effective_completed_streets: number | null;
  eligible_nodes: number | null;
};

export type RemainingNode = {
  id: string;
  longitude: number;
  latitude: number;
};

export type StreetFeature = {
  street_id: string;
  way_id: string;
  dataset_id: string;
  city_id: string;
  name: string;
  geometry: Record<string, unknown>;
  visited_nodes: number | null;
  eligible_nodes: number | null;
  completed: boolean | null;
  manual_completed: boolean;
  manual_reason: string | null;
  effective_completed: boolean | null;
};

export type StreetItem = {
  id: string;
  dataset_id: string;
  city_id: string;
  name: string;
  visited_nodes: number | null;
  eligible_nodes: number | null;
  threshold: number | null;
  state: 'complete' | 'partial' | 'missing' | null;
  manual_completed: boolean;
  manual_reason: string | null;
  effective_state: 'complete' | 'partial' | 'missing' | null;
};

export type TrackFeature = {
  activity_id: string;
  name: string;
  date: string;
  geometry: Record<string, unknown>;
};
