const paths = {
  compass: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Zm4-16-3 7-7 3 3-7 7-3Z',
  route: 'M6 6a3 3 0 1 0 0 .01ZM18 18a3 3 0 1 0 0 .01ZM9 6h6a3 3 0 0 1 0 6H9a3 3 0 0 0 0 6h6',
  city: 'M3 21V9h7v12M10 21V3h11v18M1 21h22M14 7h3M14 11h3M14 15h3M6 13v1M6 17v1',
  upload: 'M12 16V3m-5 5 5-5 5 5M3 16v5h18v-5',
  search: 'M21 21l-5-5M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z',
  arrow: 'M5 12h14m-6-6 6 6-6 6',
  back: 'M19 12H5m6-6-6 6 6 6',
  close: 'm6 6 12 12M6 18 18 6',
  check: 'm5 12 4 4L19 6',
  refresh: 'M20 7a9 9 0 1 0 1 8M20 2v5h-5',
  layers: 'm12 2 10 6-10 6L2 8l10-6ZM2 12l10 6 10-6M2 16l10 6 10-6',
  plus: 'M12 5v14M5 12h14',
  pin: 'M12 22s8-8 8-14A8 8 0 0 0 4 8c0 6 8 14 8 14ZM12 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z',
  logout: 'M9 3H3v18h6M9 12h12m-5-5 5 5-5 5',
};
export type IconName = keyof typeof paths;
export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]} /></svg>;
}
