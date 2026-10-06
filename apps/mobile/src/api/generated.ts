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
