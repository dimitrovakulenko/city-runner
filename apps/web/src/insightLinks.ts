const id = (value: string | null) => value !== null && /^[1-9][0-9]{0,18}$/.test(value) && BigInt(value) <= 9223372036854775807n;
const activityId = (value: string | null) => value !== null && /^-?(0|[1-9][0-9]{0,18})$/.test(value)
  && BigInt(value) >= -9223372036854775808n && BigInt(value) <= 9223372036854775807n;

export function insightLink(search: string) {
  const query = new URLSearchParams(search);
  const owner = query.get('owner');
  if (!owner || !/^[0-9a-f]{64}$/.test(owner)) return null;
  const activity = query.get('activity');
  if (activityId(activity)) return { owner, activity: activity!, street: null };
  const dataset = query.get('dataset'), city = query.get('city'), street = query.get('street');
  return id(dataset) && id(city) && id(street) ? { owner, activity: null, street: { dataset: dataset!, city: city!, id: street! } } : null;
}

export async function insightOwner(account: string) {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(account));
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}
