import assert from 'node:assert/strict';
import test from 'node:test';
import {exclusiveFields} from '../src/components/studyConnections.js';

const properties={measured:{},simulated:{}};
const plain=key=>({required:[key]});
const nonNull=key=>({required:[key],properties:{[key]:{not:{type:'null'}}}});

test('alternative inputs are derived from a method contract, including nullable fields',()=>{
  for(const option of [plain,nonNull]){
    const oneOf=['measured','simulated'].map(option);
    assert.deepEqual(exclusiveFields({properties,oneOf}),['measured','simulated']);
  }
});

test('compound or constrained alternatives retain all editable fields',()=>{
  for(const second of [plain('unknown'),plain('measured'),{required:['measured','simulated']},
    {...plain('simulated'),properties:{simulated:{minimum:1}}},{...plain('simulated'),additionalProperties:false}]) {
    assert.deepEqual(exclusiveFields({properties,oneOf:[plain('measured'),second]}),[]);
  }
});
