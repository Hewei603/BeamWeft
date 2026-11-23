import test from 'node:test';
import assert from 'node:assert/strict';
import {compareDeviations,deviationText} from '../src/components/studyDeviation.js';

test('pairwise target judgments preserve saved precision across decimal and numeric forms',()=>{
  assert.equal(compareDeviations('99999999999999999','99999999999999998'),1);
  assert.equal(compareDeviations('9.9999999999999999E+16',1e17),-1);
  assert.equal(compareDeviations('2.7976931348623157E+309','1.9e310'),-1);
  assert.equal(compareDeviations('0.00000001990000','1.99E-8'),0);
  assert.equal(compareDeviations('001.01','1E+0'),1);
  assert.equal(compareDeviations(0,'0.000E-122'),0);
  assert.equal(compareDeviations(0,2),-1);
  assert.equal(compareDeviations(undefined,'1'),null);
});

test('labels use the same significant figures across storage types and reveal precision for close pairs',()=>{
  assert.equal(deviationText('0.44182195399586726'),'0.441822');
  assert.equal(deviationText(Number('0.44182195399586726')),'0.441822');
  assert.equal(deviationText('99999999999999999',true),'99999999999999999');
  assert.equal(deviationText('1234567890123456789012345678901',true),'1.234567890… × 10^30');
  assert.equal(deviationText(.5000000001,true),'0.5000000001');
});
