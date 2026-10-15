import assert from 'node:assert/strict';
import test from 'node:test';
import { insightLink } from '../src/insightLinks';

test('Insights activity links preserve signed legacy BIGINT IDs and require an owner', () => {
  const owner = 'a'.repeat(64);
  assert.equal(insightLink(`?owner=${owner}&activity=-9223372036854775808`)?.activity, '-9223372036854775808');
  assert.equal(insightLink(`?owner=${owner}&activity=9223372036854775808`), null);
  assert.equal(insightLink('?activity=1'), null);
  assert.equal(insightLink(`?owner=${owner}&dataset=1&city=2&street=-3`), null);
  assert.deepEqual(insightLink(`?owner=${owner}&dataset=1&city=2&street=3`)?.street, { dataset: '1', city: '2', id: '3' });
});
