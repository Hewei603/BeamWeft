import assert from 'node:assert/strict';
import test from 'node:test';
import {createBenchLayoutSelector} from '../src/components/benchSolverLayout.js';

test('a port schematic can be rearranged without changing the model used for computation', () => {
  const layout = createBenchLayoutSelector();
  const project = {system:{mode:'single_pass', elements:[]}, network:{
    components:[{uid:'tap', name:'Tap', model:{kind:'coupler', through_fraction:0.5}, diagram_xy:[110,90]}],
    links:[], drives:[{at:{component:'tap',port:'1'}, power_w:2}],
  }};
  const baseline = layout(project);
  const moved = globalThis.structuredClone(project);
  moved.network.components[0].diagram_xy = [326,115];
  const drawn = layout(moved);
  assert.strictEqual(drawn, baseline);
  assert.equal(Object.hasOwn(drawn.network.components[0], 'diagram_xy'), false);
  assert.deepEqual(moved.network.components[0].diagram_xy, [326,115]);

  for (const edit of [
    net => {net.components[0].model.through_fraction = 0.25;},
    net => {net.components[0].transverse_action = 'uniform';},
    net => {net.drives[0].power_w = 0;},
    net => {net.links = [{a:{component:'tap',port:'2'}, b:{component:'tap',port:'3'},length_mm:100}];},
  ]) {
    const changed = globalThis.structuredClone(moved);
    edit(changed.network);
    assert.notStrictEqual(layout(changed), baseline);
    assert.strictEqual(layout(moved), baseline);
  }
});

test('moving an ordered optic or an observation plane changes the physical layout', () => {
  const layout = createBenchLayoutSelector();
  const project = {system:{mode:'single_pass',elements:[{kind:'space',length_mm:60}]},
    observation_planes:[{uid:'detector',position_reference:'fixed_z',z_mm:40}]};
  const baseline = layout(project);
  assert.strictEqual(layout({...project,title:'Notebook annotation'}), baseline);
  assert.notStrictEqual(layout({...project,system:{...project.system,elements:[{kind:'space',length_mm:62}]}}), baseline);
  assert.notStrictEqual(layout({...project,observation_planes:[{...project.observation_planes[0],z_mm:41}]}), baseline);
  assert.strictEqual(layout(project), baseline);
});
