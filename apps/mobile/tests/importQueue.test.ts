import assert from "node:assert/strict";
import test from "node:test";
import { uploadSequentially } from "../src/importQueue";

test("uploads are sequential and stop is checked after the active request settles", async () => {
  let release!: () => void;
  let allowed = true;
  let active = 0;
  let maxActive = 0;
  const seen: number[] = [];
  const run = uploadSequentially([1, 2, 3], {
    canContinue: () => allowed,
    async submit(item) {
      active++; maxActive = Math.max(active, maxActive); seen.push(item);
      if (item === 1) { await new Promise<void>((resolve) => { release = resolve; }); allowed = false; }
      active--;
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  allowed = false;
  release();
  await run;
  assert.deepEqual(seen, [1]);
  assert.equal(maxActive, 1);
});

test("a stale account generation prevents the next queued submission", async () => {
  let generation = 1;
  const seen: number[] = [];
  await uploadSequentially([1, 2], {
    canContinue: () => generation === 1,
    async submit(item) { seen.push(item); generation = 2; },
  });
  assert.deepEqual(seen, [1]);
});
