import assert from 'node:assert/strict';
import test from 'node:test';
import {cavityStatus, halfTraceReading} from '../src/components/cavityStability.js';
import {emptyFieldMessage} from '../src/components/opticalProfile.js';

test('a definitively unstable plane rules out a two-axis mode even when the other plane is marginal or missing', () => {
  const unstable = {m:-5/3, stable:false, marginal:false};
  for (const other of [{m:-1, stable:false, marginal:true}, {m:.3, stable:true}, undefined]) {
    for (const planes of [{tangential:unstable, sagittal:other}, {tangential:other, sagittal:unstable}]) {
      assert.equal(cavityStatus(planes), '存在失稳平面');
      assert.match(emptyFieldMessage({mode:'ring'}, {planes}), /存在失稳平面/);
    }
  }
});

test('both planes must be resolved inside the stability interval to label the cavity stable', () => {
  const stable = {m:.3, stable:true}, boundary = {m:-1, stable:false, marginal:true};
  assert.equal(cavityStatus({tangential:stable, sagittal:stable}), '双平面稳定');
  assert.equal(cavityStatus({tangential:stable}), '主光路待补全');
  assert.equal(cavityStatus({}), '主光路待补全');
  assert.equal(cavityStatus({sagittal:boundary}), '临界附近');
  assert.equal(cavityStatus({tangential:boundary,sagittal:stable}), '临界附近');
  assert.match(emptyFieldMessage({mode:'standing_wave'}, {planes:{sagittal:boundary}}), /稳定边界附近/);
});

test('the displayed half trace preserves the side of a nearby stability boundary', () => {
  assert.equal(halfTraceReading({m:-.999999, stable:true}), '-0.999999（稳定）');
  assert.equal(halfTraceReading({m:1.0000001, stable:false}), '1.0000001（失稳）');
  assert.equal(halfTraceReading({m:-1, stable:false, marginal:true}), '-1（临界附近）');
  assert.equal(halfTraceReading({m:1-Number.EPSILON, stable:true, marginal:true}), '≈1（临界附近）');
  assert.equal(halfTraceReading(undefined), '—（待定）');
});
