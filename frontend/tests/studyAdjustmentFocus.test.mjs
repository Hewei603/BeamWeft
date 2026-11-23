import test from 'node:test';
import assert from 'node:assert/strict';
import {adjustmentView,selectedDesignIndex} from '../src/components/studyAdjustmentRoles.js';

test('manual evidence links, including an explicit first row, supersede the saved opening design',()=>{
  const record={roles:['design'],designs:[{design:[.15,2]},{design:[.25,1]},{design:[.5,3]}],focus_design:[.5,3]};
  assert.equal(selectedDesignIndex(record),2);
  assert.equal(selectedDesignIndex(record,{designIndex:0}),0);
  assert.equal(selectedDesignIndex(record,{focus_design:[.25,1]}),1);
  assert.equal(selectedDesignIndex(record,{designIndex:2,focus_design:[.25,1]}),1);
  assert.equal(selectedDesignIndex(record,{designIndex:40}),2);
  assert.deepEqual(adjustmentView(record),{mode:'adjustment',roles:['design'],focus_design:[.5,3],designIndex:2});
});

test('old records keep their original first-row behavior; a compact tool receipt can still name another row',()=>{
  const record={roles:['design'],designs:[{design:[.15]},{design:[.5]}]};
  assert.equal(selectedDesignIndex(record),0);
  assert.deepEqual(adjustmentView(record),{mode:'adjustment',roles:['design']});
  assert.equal(adjustmentView({roles:record.roles,designIndex:1}).designIndex,1);
});
