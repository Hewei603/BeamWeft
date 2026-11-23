import test from 'node:test';
import assert from 'node:assert/strict';
import {distributionDraft,percent,readDistribution,requestDistribution,sameDistribution,scenarioCoordinates}
  from '../src/components/studyScenarioWeights.js';

test('condition coordinates retain original interleaved and linked column order',()=>{
  const cells=[{coordinates:[10,20,30,40,50]},{coordinates:[10,21,30,40,50]},{coordinates:[11,20,30,40,50]}];
  assert.deepEqual(scenarioCoordinates(cells,[[{}],[{}],[{},{}],[{}]],['hidden','control','observed','design']),
    [[10,30,40],[11,30,40]]);
});

test('an explicitly zero condition survives round-trip with its evidence reference; a blank does not become zero',()=>{
  const input={basis:'observed',source:'log A',scenarios:[
    {coordinates:[0],weight:0,fraction:0},{coordinates:[3],weight:4,fraction:1}],
  originals:[{attachment_id:6,sha256:'a'.repeat(64),relative_path:'raw/tally.csv'}]};
  const draft=distributionDraft(input),out=readDistribution(draft,[[3],[0]]);
  assert(sameDistribution(out,input));
  assert.deepEqual(out.originals,[{attachment_id:6,sha256:'a'.repeat(64)}]);
  assert(!('fraction' in requestDistribution(input).scenarios[0]));
  assert(!sameDistribution(out,{...input,originals:[]}));
  draft.weights['[0]']='';
  assert.throws(()=>readDistribution(draft,[[3],[0]]),/非负权重/);
  draft.weights['[0]']='1e-999';
  assert.throws(()=>readDistribution(draft,[[3],[0]]),/数字表示范围/);
});

test('a numeric endpoint with a nontrivial exact mass is distinguishable from certainty',()=>{
  assert(sameDistribution(null,undefined));
  assert.equal(percent(0,'1/100000000000000000000'),'>0%');
  assert.equal(percent(1,'99999999999999999999/100000000000000000000'),'<100%');
  assert.equal(percent(.999999999999),'<100%');
  assert.equal(percent(.09),'9%');
});
