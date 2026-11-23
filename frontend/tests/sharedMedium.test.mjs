import assert from 'node:assert/strict';
import {test} from 'node:test';
import {sharedIndexReading, withIncidentWavelength, withSharedIndex} from '../src/components/sharedMedium.js';
import {withElementParameter} from '../src/components/benchGeometry.js';
import {scanFields} from '../src/components/scanAxes.js';

test('a branch calibration expires by wavelength without changing a legacy path or the host',()=>{
  const old={uid:'probe',wavelength_nm:780,target_uid:'a',target_n:1,downstream:[{target_uid:'b',n:1.6},{target_uid:'c',n:null}]};
  const next=withIncidentWavelength(old,532);
  assert.deepEqual(next.independent_index_uids,['a','b']);
  assert.deepEqual(next.downstream,[{target_uid:'b',n:null},{target_uid:'c',n:null}]);
  assert.equal(old.downstream[0].n,1.6);
  assert.equal(next.shared_index_wavelength_nm,532);
  const calibrated=withSharedIndex(next,'b',1.62);
  assert.deepEqual(calibrated.downstream,[{target_uid:'b',n:1.62},{target_uid:'c',n:null}]);
  assert.equal(calibrated.shared_index_wavelength_nm,532);
  assert.equal(withIncidentWavelength(calibrated,532),calibrated);
});

test('empty independent medium and automatic medium remain distinct after editing one or both indices',()=>{
  let path={uid:'p',wavelength_nm:780,target_uid:'first',through_uid:'last',target_n:null};
  path=withSharedIndex(path,'first',1.37);
  path=withSharedIndex(path,'last',1.51);
  path=withIncidentWavelength(path,405);
  assert.deepEqual(path.downstream,[]);
  assert.deepEqual(path.independent_index_uids,['first','last']);
  path=withSharedIndex(path,'first',null,false);
  assert.deepEqual(path.independent_index_uids,['last']);
  path=withSharedIndex(path,'last',1.55);
  path=withSharedIndex(path,'last',null);
  assert.deepEqual(path.downstream,[]);
  assert.deepEqual(path.independent_index_uids,['last']);
});

test('old result rows retain their recorded index without assigning a new material source',()=>{
  assert.match(sharedIndexReading({n:1.51}), /1.51.*原记录未注明来源/);
  assert.match(sharedIndexReading({n:null,index_source:'out_of_range',index_model:'nbk7'}),/n 待定.*超出.*nbk7/);
});

test('switching material sources clears the old number and makes only independent indices scannable',()=>{
  const original={uid:'plate',kind:'space',n:1.4,length_mm:7};
  const catalog={models:{space:{parameters:['length_mm','n']}},parameters:{length_mm:'长度',n:'折射率'},bounds:{}};
  const sourced=withElementParameter(original,'index_reference',{model:'nbk7',wavelength_nm:780},780);
  assert.equal(sourced.n,null);
  assert.deepEqual(Object.keys(scanFields(catalog,sourced)),['length_mm']);
  const entered=withElementParameter({...sourced,n:1.511},'index_reference',{model:null,wavelength_nm:780},780);
  assert.equal(entered.n,null);
  assert.deepEqual(Object.keys(scanFields(catalog,entered)),['length_mm','n']);
  assert.equal(withElementParameter(original,'n',1.4,780).index_reference.wavelength_nm,780);
  for (const kind of ['thick_lens','grin']) {
    const element={kind,n:null};
    const measured=withElementParameter(element,'n',1.53,780);
    assert.deepEqual(measured.index_reference,{model:null,wavelength_nm:780});
    assert.equal(withElementParameter(element,'n',1.53,null).index_reference,undefined);
  }
});
