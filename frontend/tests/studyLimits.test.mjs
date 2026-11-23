import test from 'node:test';
import assert from 'node:assert/strict';
import {acceptanceInequality} from '../src/components/studyLimits.js';

test('two inclusive measurement limits put the measured quantity between physical endpoints',()=>{
  assert.equal(acceptanceInequality({label:'光功率 / W',unit:'W',lower:.9,upper:1.1}),'0.9 W ≤ 光功率 ≤ 1.1 W');
  assert.equal(acceptanceInequality({label:'相位',unit:'°',lower:-.25,upper:0}),'-0.25 ° ≤ 相位 ≤ 0 °');
  assert.equal(acceptanceInequality({label:'通过率',unit:'1',lower:0,upper:1}),'0 ≤ 通过率 ≤ 1');
});

test('one-sided, coincident and close bounds retain their exact stored numeric endpoints',()=>{
  const limit=(lower,upper)=>acceptanceInequality({label:'x',unit:'mm',lower,upper});
  assert.equal(limit(null,0),'x ≤ 0 mm');
  assert.equal(limit(-1,null),'x ≥ -1 mm');
  assert.equal(limit(0,0),'x = 0 mm');
  assert.equal(limit(1,1.00000000001),'1 mm ≤ x ≤ 1.00000000001 mm');
});
