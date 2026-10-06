import { createApiRequest } from './client';
import type { SessionStore } from './client';
import type {
  CityPage,
  ContributionPage,
  ProgressResponse,
  StreetDetail,
  StreetPage,
} from './generated';

export type StreetFilter = 'all' | 'incomplete' | 'partial' | 'completed';

export interface CityExplorerApi {
  getProgress(rule: 'normal' | 'strict'): Promise<ProgressResponse>;
  getCities(datasetId: string, query: { rule: 'normal' | 'strict'; q: string; page: number; pageSize: number }): Promise<CityPage>;
  getStreets(cityId: string, query: { datasetId: string; rule: 'normal' | 'strict'; filter: StreetFilter; q: string; page: number; pageSize: number }): Promise<StreetPage>;
  getStreet(streetId: string, query: { datasetId: string; rule: 'normal' | 'strict'; page: number; pageSize: number }): Promise<StreetDetail>;
  getContributions(streetId: string, query: { datasetId: string; page: number; pageSize: number }): Promise<ContributionPage>;
}

export function createFixtureCityExplorerApi(): CityExplorerApi {
  const unavailable = (): Promise<never> => Promise.reject(new Error('City browsing is unavailable in fixture mode.'));
  return { getProgress: unavailable, getCities: unavailable, getStreets: unavailable, getStreet: unavailable, getContributions: unavailable };
}

export function createCityExplorerApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: typeof fetch }): CityExplorerApi {
  const request = createApiRequest({ baseUrl: options.baseUrl, sessionStore: options.sessionStore, fetchImpl: options.fetchImpl });
  return {
    getProgress(rule) {
      return request<ProgressResponse>(`/api/progress?${new URLSearchParams({ rule })}`);
    },
    getCities(datasetId, input) {
      const query = new URLSearchParams({ dataset_id: datasetId, rule: input.rule, page: String(input.page), page_size: String(input.pageSize) });
      if (input.q.trim()) query.set('q', input.q.trim());
      return request<CityPage>(`/api/cities?${query}`);
    },
    getStreets(cityId, input) {
      const query = new URLSearchParams({ dataset_id: input.datasetId, rule: input.rule, filter: input.filter,
        page: String(input.page), page_size: String(input.pageSize) });
      if (input.q.trim()) query.set('q', input.q.trim());
      return request<StreetPage>(`/api/cities/${encodeURIComponent(cityId)}/streets?${query}`);
    },
    getStreet(streetId, input) {
      const query = new URLSearchParams({ dataset_id: input.datasetId, rule: input.rule, page: String(input.page), page_size: String(input.pageSize) });
      return request<StreetDetail>(`/api/streets/${encodeURIComponent(streetId)}?${query}`);
    },
    getContributions(streetId, input) {
      const query = new URLSearchParams({ dataset_id: input.datasetId, page: String(input.page), page_size: String(input.pageSize) });
      return request<ContributionPage>(`/api/streets/${encodeURIComponent(streetId)}/contributions?${query}`);
    },
  };
}
