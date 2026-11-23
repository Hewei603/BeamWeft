import assert from 'node:assert/strict';
import test from 'node:test';
import {newInputRecord,objectInputSchema,objectInputValue} from '../src/components/studyObjectSchema.js';

const attrs={type:'object',additionalProperties:false,properties:{
  sample:{type:'string'}, weight:{anyOf:[{type:'number'},{type:'null'}],default:null},
  quality:{type:'string',enum:['A','B']}, flag:{type:'boolean',default:false}, tag:{type:'string',const:'lab'}},
  required:['sample','tag']};
const schema={$defs:{Sample:attrs}};
const optional={anyOf:[{$ref:'#/$defs/Sample'},{type:'null'}]};

test('one uniform object schema describes optional records and ordered arrays with native scalar types',()=>{
  const entry=objectInputSchema(optional,schema);
  assert.ok(entry.nullable && entry.properties.weight.nullable && entry.properties.sample.required);
  const array=objectInputSchema({type:'array',items:{$ref:'#/$defs/Sample'},minItems:1,maxItems:4},schema);
  assert.equal(array.list,true); assert.equal(array.min,1); assert.equal(array.max,4);
  assert.deepEqual(newInputRecord(entry.properties),{tag:'lab'});
  assert.deepEqual(objectInputValue(array,'[{"sample":"a","weight":0,"quality":"B","flag":false}]').value,
    [{sample:'a',weight:0,quality:'B',flag:false}]);
  assert.deepEqual(objectInputValue(entry,'{"sample":"","weight":null}').value,{sample:'',weight:null});
  assert.equal(objectInputValue(entry,'null').value,null);
});

test('unsupported schemas and historical or invalid input stay in the original JSON editor',()=>{
  for(const field of [{type:'object',additionalProperties:{type:'number'}},
    {type:'object',properties:{variable:{oneOf:[{type:'string'},{type:'number'}]}}},
    {type:'object',properties:{map:{type:'object',properties:{key:{type:'string'}}}}},
    {type:'array',items:{$ref:'#/$defs/Absent'}}, {...attrs,'x-beamweft-input':'original-file'}]) {
    assert.equal(objectInputSchema(field,schema),null);
  }
  const descriptor=objectInputSchema(optional,schema);
  for (const raw of ['{','[]','{"previous_solver_field":12}','{"weight":"0.3"}','{"quality":"C"}','{"tag":"foreign"}']) {
    assert.ok(objectInputValue(descriptor,raw).error);
  }
  assert.equal(objectInputValue(descriptor,'').value,undefined);
});

test('an ordered list of calibrated originals can use the picker while preserving frozen references',()=>{
  const root={$defs:{File:{type:'object','x-beamweft-input':'original-file'},Layer:{type:'object',properties:{
    image:{$ref:'#/$defs/File'},gain:{type:'number'},reference:{anyOf:[{$ref:'#/$defs/File'},{type:'null'}]},
  },required:['image']}}};
  const descriptor=objectInputSchema({type:'array',items:{$ref:'#/$defs/Layer'}},root);
  assert.equal(descriptor.properties.image['x-beamweft-input'],'original-file');
  assert(descriptor.properties.image.required);
  const sample=[{image:{attachment_id:3,sha256:'a'.repeat(64)},gain:0,reference:null},{image:{attachment_id:4},gain:-1}];
  assert.deepEqual(objectInputValue(descriptor,JSON.stringify(sample)).value,sample);
  for(const image of [{attachment_id:0},{attachment_id:1,from_plugin:'retain me'},'original.npy'])
    assert.match(objectInputValue(descriptor,JSON.stringify([{image}])).error,/JSON/);
});
