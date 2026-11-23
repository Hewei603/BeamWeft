import test from 'node:test';
import assert from 'node:assert/strict';
import {
  benchLayout, benchPositions, incidentControl, moveAlongAxis, moveIncidentElement, positionControl,
} from '../src/components/benchGeometry.js';

const lens = uid => ({uid, kind:'lens', focal_mm:40});
const gap = (uid, length_mm) => ({uid, kind:'space', length_mm, n:1});
const system = elements => ({system:{mode:'single_pass', elements}, parts:[]});

test('opening a contact changes only the selected position and retains part identities', () => {
  const a=lens('a'), b=lens('b');
  const project={...system([a,b]),parts:[{element_index:1,inventory_id:5,locked_parameters:['focal_mm']}]};
  const motion=positionControl(project,1);
  assert.equal(moveAlongAxis(project,motion,0),project);
  assert.equal(moveAlongAxis(project,motion,-1),project);
  const moved=moveAlongAxis(project,motion,42,'new-gap');
  assert.deepEqual(benchPositions(moved).map(r=>r.start),[0,0,42]);
  assert.equal(moved.system.elements[0],a);
  assert.equal(moved.system.elements[2],b);
  assert.deepEqual(moved.system.elements[1],{...gap('new-gap',42),name:'空气间距',index_reference:{model:'air'}});
  assert.deepEqual(moved.parts,[{...project.parts[0],element_index:2}]);
  const further=moveAlongAxis(moved,positionControl(moved,2),90);
  assert.equal(further.system.elements[1].uid,'new-gap');
  assert.equal(further.system.elements[1].length_mm,90);
  const sourceReferenced=system([lens('first')]);
  const shifted=moveAlongAxis(sourceReferenced,positionControl(sourceReferenced,0),15);
  assert.equal(benchPositions(shifted).at(-1).start,15);
});

test('an internal contact exchanges an explicit air interval in either direction', () => {
  const a=lens('a'), b=lens('b'), c=lens('c');
  for(const [project,index,z] of [[system([a,b,gap('space',30),c]),1,9],
                                   [system([a,gap('space',30),b,c]),2,9]]) {
    const moved=moveAlongAxis(project,positionControl(project,index),z);
    assert.deepEqual(benchPositions(moved).filter(r=>r.element.kind==='lens').map(r=>r.start),[0,9,30]);
  }
});

test('source and join stay fixed on a private path while a contact opens; inventory follows its optic', () => {
  const incoming={uid:'arm',target_uid:'host',through_uid:'host',elements:[lens('a'),lens('b'),gap('r',20)],
    parts:[{element_index:1,inventory_id:4,locked_parameters:['kind']}],downstream:[]};
  const project={...system([gap('host',15)]),incident_paths:[incoming]};
  const route=benchLayout(project).routes[0];
  const moved=moveIncidentElement(project,incidentControl(route,1),-7);
  const result=benchLayout(moved).routes[0];
  assert.equal(result.sourceZ,route.sourceZ);
  assert.equal(result.entrance.start,route.entrance.start);
  assert.equal(result.sourceZ+result.rows[2].start,-7);
  assert.equal(result.rows[2].element.uid,'b');
  assert.equal(result.path.parts[0].element_index,2);
  const atSource={...project,incident_paths:[{...incoming,parts:[],elements:[lens('at-source'),gap('last',20)]}]};
  const incident=benchLayout(atSource).routes[0];
  const displaced=benchLayout(moveIncidentElement(atSource,incidentControl(incident,0),-12)).routes[0];
  assert.equal(displaced.sourceZ,-20);
  assert.equal(displaced.entrance.start,0);
  assert.equal(displaced.sourceZ+displaced.rows[1].start,-12);
});

test('moving preserves a cavity datum, spacers, library instances, grouped components and element limit', () => {
  const a=lens('a'), b=lens('b');
  const assembly={uid:'mount',path_uid:'primary',name:'复合物件',element_uids:['a','b']};
  const project={...system([lens('free'),a,b]),assemblies:[assembly]};
  const placed=moveAlongAxis(project,positionControl(project,2),25);
  assert.deepEqual(benchPositions(placed).filter(r=>r.element.kind==='lens').map(r=>r.start),[0,25,25]);
  const part=e=>({...e,component_origin:{instance_uid:'shared'}});
  assert.equal(positionControl(system([part(a),part(b)]),1),null);
  assert.equal(positionControl({...system([gap('locked',10),b]),parts:[{element_index:0,locked_parameters:['length_mm']}]},1),null);
  assert.equal(positionControl(system(Array.from({length:80},(_,i)=>lens(String(i)))),79),null);
  assert.equal(positionControl({system:{mode:'standing_wave',elements:[a,gap('space',20),b]},parts:[]},0),null);
});

test('separate plates can move; contiguous media on an archived shared path stay connected', () => {
  const plate=uid=>({...gap(uid,3),medium_role:'medium',n:null,index_reference:{model:null}});
  const project=system([plate('window'),plate('sample')]);
  const separated=moveAlongAxis(project,positionControl(project,1),10);
  assert.deepEqual(benchPositions(separated).map(row=>row.start),[0,3,10]);
  assert.equal(separated.system.elements[2].n,null);
  const bound={...project,incident_paths:[{uid:'pump',target_uid:'window',downstream:[{target_uid:'sample'}]}]};
  assert.equal(positionControl(bound,1),null);
});
