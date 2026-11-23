import test from 'node:test';
import assert from 'node:assert/strict';
import {elementRemoval, removeOptic} from '../src/components/elementRemovalPlan.js';
import {benchLayout} from '../src/components/benchGeometry.js';

const space=(uid,length_mm,n=1)=>({uid,name:uid,kind:'space',length_mm,n});
const rule=scope=>({scope,element_uid:'sample',law:{mode:'net',retained:{low:.95}}});
const arm={uid:'arm',name:'Auxiliary',through_uid:'end',target_uid:'start',elements:[space('sample',3,1.3)],
  downstream:[{target_uid:'sample',n:1.4}],independent_index_uids:['sample'],power_budget:{stages:[rule('arm'),rule('host')]}};
const project={system:{mode:'single_pass',elements:[space('start',10),space('sample',6,1.55),space('end',20)]},
  parts:[{element_index:2,source:'survey',locked_parameters:['length_mm']}],incident_paths:[arm],single_pass_power:{stages:[rule('host')]}};

test('keep mounting positions or close the space, with scoped removal of calibrations and losses',()=>{
  const frozen=globalThis.structuredClone(project);
  const plan=elementRemoval(project,1);
  assert.deepEqual([plan.laws,plan.calibrated,plan.blockers],[2,1,[]]);
  const saved=removeOptic(project,1);
  const air=saved.system.elements[1];
  assert.notEqual(air.uid,'sample');assert.equal(air.index_reference.model,'air');
  assert.equal(benchLayout(saved).positions.at(-1).end,36);
  assert.deepEqual(saved.single_pass_power.stages,[]);
  assert.deepEqual(saved.incident_paths[0].power_budget.stages,[rule('arm')]);
  assert.deepEqual(saved.incident_paths[0].downstream,[]);assert.deepEqual(saved.incident_paths[0].independent_index_uids,[]);
  const closed=removeOptic(project,1,{geometry:'close'});
  assert.equal(benchLayout(closed).positions.at(-1).end,30);
  assert.equal(closed.parts[0].element_index,1);
  const privateEdit=removeOptic(project,0,{pathUid:'arm'});
  assert.equal(benchLayout(privateEdit).routes[0].sourceZ,-3);
  assert.deepEqual(privateEdit.incident_paths[0].power_budget.stages,[rule('host')]);
  assert.deepEqual(privateEdit.single_pass_power,project.single_pass_power);
  assert.deepEqual(privateEdit.incident_paths[0].downstream,arm.downstream);
  assert.deepEqual(project,frozen);
});

test('removal names dependent instruments and paths without confusing their scoped identities',()=>{
  const model={...project,observation_planes:[{uid:'station',name:'Camera',target_uid:'sample'}],
    network:{components:[{uid:'coupler',name:'Receiver'}],drives:[{at:{component:'coupler',port:'1'},
      path_output:{path_uid:'arm',scope:'arm',element_uid:'sample'},profile:{kind:'layout',beam_uid:'arm',element_exit:{scope:'host',element_uid:'sample'}}}]}};
  const blockers=elementRemoval(model,1).blockers;
  assert.equal(blockers.length,2);assert.match(blockers[0],/Camera/);assert.match(blockers[1],/Receiver/);
  assert.equal(elementRemoval(model,0,'arm').blockers.length,1);
  assert.equal(removeOptic(model,1,{geometry:'close'}),model);
  for (const path of [{...arm,target_uid:'sample'},{...arm,through_uid:'sample'},{...arm,through_uid:null}]) {
    const withAnchor={...project,incident_paths:[path]};
    assert.match(elementRemoval(withAnchor,1).blockers.join(' '),/Auxiliary/);
    assert.equal(removeOptic(withAnchor,1),withAnchor);
  }
  const fixed={...project,observation_planes:[{position_reference:'fixed_z',z_mm:12,name:'Camera'}]};
  assert.equal(removeOptic(fixed,1).observation_planes,fixed.observation_planes);
});

test('last element and installed mounts preserve valid topology or require an explicit geometry change',()=>{
  const bare={system:{mode:'single_pass',elements:[space('sample',6,1.5)]},parts:[]};
  assert.deepEqual(removeOptic(bare,0).system.elements,[]);
  const metered={...bare,single_pass_power:{input_w:{low:1},stages:[rule('host')]}};
  assert.equal(removeOptic(metered,0).system.elements[0].length_mm,6);
  assert.equal(removeOptic(metered,0,{geometry:'close'}),metered);
  const locked={...project,parts:[{element_index:1,source:'lab',inventory_id:2,locked_parameters:['n']}]};
  assert.equal(removeOptic(locked,1),locked);
  assert.notEqual(removeOptic(locked,1,{unlocked:true}),locked);
  const stockWithoutLocks={...project,parts:[{element_index:1,source:'lab',inventory_id:2,locked_parameters:[]}]};
  assert.notEqual(removeOptic(stockWithoutLocks,1),stockWithoutLocks);
  const group={...locked,assemblies:[{path_uid:'primary',name:'Housing',element_uids:['sample']}]};
  assert.equal(removeOptic(group,1,{unlocked:true}),group);
});

test('unmounting a complete assembly checks every member and retains independent installations',()=>{
  const elements=[space('lead',3),space('cap',2,1.8),space('active',7,1.8),space('copy',7,1.8),space('end',3)];
  const group={uid:'mount',name:'Bonded sample',path_uid:'primary',element_uids:['cap','active']};
  const other={uid:'mount-copy',name:'Second fixture',path_uid:'primary',element_uids:['copy']};
  const parts=[1,2,3].map(element_index=>({element_index,inventory_id:8,locked_parameters:['length_mm']}));
  const model={system:{mode:'single_pass',elements},assemblies:[group,other],parts};
  const action={assemblyUid:'mount',unlocked:true};
  const anchored={...model,observation_planes:[{uid:'one',name:'Sample entrance',target_uid:'cap',depth_mm:0}]};
  assert.match(elementRemoval(anchored,2,'primary',true).blockers.join(' '),/Sample entrance/);
  assert.equal(removeOptic(anchored,2,action),anchored);
  assert.equal(removeOptic(model,1,{assemblyUid:'mount'}),model);
  assert.equal(removeOptic(model,2,{unlocked:true}),model);
  const removed=removeOptic(model,2,action);
  assert.deepEqual(removed.assemblies,[other]);
  assert.deepEqual(removed.system.elements.map(e=>e.length_mm),[3,9,7,3]);
  assert.deepEqual(removed.parts,[{...parts[2],element_index:2}]);
  assert.equal(removed.system.elements[2].uid,'copy');
  assert.equal(removed.system.elements[1].index_reference.model,'air');
  assert.equal(model.assemblies.length,2);
});
