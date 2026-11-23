import test from 'node:test';
import assert from 'node:assert/strict';
import {engineeringObjects,findStockObject,objectReferences} from '../src/components/inventoryTargets.js';

test('the same element identifier on different paths resolves to the parts of its own path',()=>{
  const lens={uid:'shared-label',kind:'lens',name:'relay'};
  const arm={uid:'arm',name:'side',elements:[lens,{uid:'tube',kind:'space',name:'tube'}],
    parts:[{element_index:0,role:'coupler',inventory_id:12,source:'measured'}]};
  const base={system:{elements:[lens]},parts:[{element_index:0,inventory_id:8}],incident_paths:[arm],assemblies:[]};
  const objects=engineeringObjects(base),main=findStockObject(objects,'primary',lens.uid),side=findStockObject(objects,'arm',lens.uid);
  assert.notEqual(main.key,side.key);
  assert.deepEqual(side.inventoryIds,[12]);
  assert.equal(side.category,'lens');
  assert.deepEqual(main.inventoryIds,[8]);
  assert.equal(findStockObject(objects,'arm','tube').category,null);
  assert.equal(findStockObject(objects,'deleted',lens.uid),undefined);
  assert.deepEqual(objectReferences(side,null),{followCanvas:true,inventory_ids:[12],
    project_objects:[{path_uid:'arm',element_uid:lens.uid}]});
});

test('same named path and port components keep separate identities and follow their selected model',()=>{
  const optic = {uid:'repeat',name:'relay',kind:'lens'};
  const device = {uid:'repeat',name:'relay',model:{kind:'matrix',ports:['I','R','T']}};
  const base = {system:{elements:[optic]},network:{components:[device]}};
  const [path, network] = engineeringObjects(base);
  assert.notEqual(network.key, path.key);
  assert.deepEqual(objectReferences(path,23),{project_revision_id:23,inventory_ids:[],
    project_objects:[{path_uid:'primary',element_uid:'repeat'}]});
  assert.deepEqual(objectReferences(network,null),{followCanvas:true,
    project_objects:device.model.ports.map(port=>({component:'repeat',port}))});
  assert.equal(findStockObject([network,path],'primary','repeat'),path);
});

test('the replacement target covers exactly one physical assembly in its owning path',()=>{
  const elements=['end','active'].map(uid=>({uid,kind:'space',name:uid}));
  const project={system:{elements:[]},parts:[],incident_paths:[{uid:'arm',name:'side',elements,
    parts:elements.map((_,i)=>({element_index:i,role:'crystal',inventory_id:7}))}],
    assemblies:[{uid:'sample',path_uid:'arm',name:'doped block',element_uids:['end','active']}]};
  const objects=engineeringObjects(project);
  assert.equal(objects.length,1);
  assert.equal(findStockObject(objects,'arm','active').category,'gain_crystal');
  assert.deepEqual(objects[0].entries.map(e=>e.element.uid),['end','active']);
});

test('uniform media are recognized by their model on either path, independent of the object name',()=>{
  const elements=[
    {uid:'tube',n:1}, {uid:'historical-air'}, {uid:'unknown',n:null},
    {uid:'historical-plate',n:1.45}, {uid:'matched',n:1,medium_role:'medium'},
    {uid:'bound-air',n:1}, {uid:'declared',n:1}, {uid:'aperture',n:1.6,clear_aperture:{width_mm:2}},
    {uid:'reserved',n:1.6}, {uid:'zero',n:1.6,length_mm:0}, {uid:'calibrated',n:1,index_reference:{wavelength_nm:633}},
  ].map(fields=>({kind:'space',name:'sample',length_mm:3,...fields}));
  const parts=[{element_index:5,inventory_id:14},{element_index:6,role:'medium'},{element_index:8,role:'pump_input'}];
  const project={system:{elements},parts,incident_paths:[{uid:'side',name:'side',elements,parts}]};
  const objects=engineeringObjects(project);
  for (const path of ['primary','side']) {
    const eligible=elements.filter(el=>findStockObject(objects,path,el.uid)?.category==='medium');
    assert.deepEqual(eligible.map(el=>el.uid),['unknown','historical-plate','matched','declared','calibrated']);
  }
});

test('a medium stack must contain only material segments and retain a single physical role',()=>{
  const elements=['front','rear'].map(uid=>({uid,name:uid,length_mm:1,n:null,kind:'space'}));
  const project={system:{elements},parts:[{element_index:0,role:'sample'}],
    assemblies:[{uid:'mount',path_uid:'primary',name:'sample mount',element_uids:['front','rear']}]};
  const mount=()=>findStockObject(engineeringObjects(project),'primary','rear').category;
  assert.equal(mount(),'medium');
  project.parts.push({element_index:1,role:'detector'});
  assert.equal(mount(),null);
  project.parts[1].role='sample';
  elements[1].n=1;
  assert.equal(mount(),null);
});
