import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import test from 'node:test';
import { createPreviewQueue } from '../src/components/previewQueue.js';

function simulator(t) {
  let now = 0;
  t.mock.timers.enable({ apis: ['setTimeout'] });
  t.mock.method(performance, 'now', () => now);
  const calls = [];
  const receipts = [];
  const queue = createPreviewQueue((system, signal) => new Promise((resolve, reject) => {
    calls.push({ system, signal, resolve, reject });
  }), receipt => receipts.push(receipt));
  function advance(ms) {
    now += ms;
    t.mock.timers.tick(ms);
  }
  return { advance, calls, receipts, queue, edit: (system, attempt = 0) => queue.submit({ system, attempt }) };
}

test('rapid edits keep the newest queued geometry and skip an obsolete error', async t => {
  const { advance, calls, receipts, edit } = simulator(t);
  edit('A');
  advance(0);
  edit('B');
  edit('C');
  advance(50);
  assert.deepEqual(calls.map(call => call.system), ['A']);
  calls[0].reject(new Error('obsolete'));
  await Promise.resolve();
  assert.deepEqual(receipts, []);
  advance(25);
  assert.deepEqual(calls.map(call => call.system), ['A', 'C']);
  calls[1].reject(new Error('current'));
  await Promise.resolve();
  assert.deepEqual(receipts, [{ system: 'C', attempt: 0, error: 'current' }]);
  edit('C', 1);
  advance(75);
  calls[2].resolve('recovered');
  await Promise.resolve();
  assert.deepEqual(receipts.at(-1), { system: 'C', attempt: 1, data: 'recovered' });
});

test('a stalled obsolete request releases the latest edit, even if a solver ignores cancellation', async t => {
  const { advance, calls, receipts, edit } = simulator(t);
  edit('old');
  advance(0);
  // A current calculation may legitimately take time.
  advance(20_000);
  assert.equal(calls[0].signal.aborted, false);
  for (const draft of ['draft1', 'draft2', 'draft3', 'draft4']) {
    edit(draft);
    advance(300);
  }
  assert.equal(calls.length, 1);
  edit('new');
  advance(1000);
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(calls[1].system, 'new');
  calls[1].resolve('new result');
  await Promise.resolve();
  calls[0].resolve('late result');
  await Promise.resolve();
  assert.deepEqual(receipts, [{ system: 'new', attempt: 0, data: 'new result' }]);
});

test('undo to an in-flight geometry reuses it, and closing a scope drops its queued work', async t => {
  const { advance, calls, receipts, edit, queue } = simulator(t);
  edit('original');
  advance(0);
  edit('draft');
  edit('original');
  advance(1200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].signal.aborted, false);
  edit('another');
  queue.cancel();
  assert.equal(calls[0].signal.aborted, true);
  calls[0].reject(new Error('late failure'));
  await Promise.resolve();
  advance(5000);
  assert.equal(calls.length, 1);
  assert.deepEqual(receipts, []);
});

test('restoring a known result cancels a scheduled call and permits the next model to run', async t => {
  const {advance, calls, receipts, edit, queue} = simulator(t);
  edit('no longer displayed');
  queue.cancel();
  advance(100);
  assert.equal(calls.length, 0);
  edit('new geometry');
  advance(0);
  calls[0].resolve('new value');
  await Promise.resolve();
  assert.equal(receipts[0].data, 'new value');
});
