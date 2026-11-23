import test from 'node:test';
import assert from 'node:assert/strict';
import {launchSettings, mainBeamStatus, setMainSource, sourceFromScalars, systemForMode} from '../src/components/mainBeamSource.js';
import {emptyProject, starterProject} from '../src/components/benchGeometry.js';

test('a new source holds unknown propagation inputs while a legacy example preserves its declared waist', () => {
  const fresh = emptyProject().system;
  assert.deepEqual(fresh.source.tangential, {radius_mm:null, curvature_mm:null, wavefront:'unknown', m2:null});
  const example = starterProject('single_pass').system;
  assert.equal(example.source,undefined);
  assert.deepEqual(sourceFromScalars(example).tangential,{radius_mm:.3,curvature_mm:null,wavefront:'waist',m2:1});
  assert.equal(sourceFromScalars({...example,beam_quality_m2:null}).tangential.m2,null);
});

test('upgrading scalar data and switching modes keep a single launch authority', () => {
  const base = {mode:'single_pass',elements:[],wavelength_nm:827,input_radius_mm:.42,input_curvature_mm:-320,beam_quality_m2:1.8};
  const source = sourceFromScalars(base);
  assert.deepEqual(source.tangential,{radius_mm:.42,curvature_mm:-320,wavefront:'curved',m2:1.8});
  const explicit = setMainSource(base,{...source,sagittal:{radius_mm:.1,m2:null,wavefront:'unknown'}});
  const retained = launchSettings(explicit);
  assert.deepEqual(Object.keys(retained),['source']);
  const cavity = systemForMode(explicit,'ring');
  assert.deepEqual(launchSettings(cavity),{});
  assert.deepEqual(systemForMode(cavity,'single_pass',retained),explicit);
  assert.equal(systemForMode(cavity,'single_pass').source.tangential.radius_mm,null);
});

test('a known source radius alone does not mark that axis as propagated', () => {
  const system=emptyProject().system;
  system.wavelength_nm=976;
  const good={radii_mm:[.4]}, partial={profile:[[0,.28]],radii_mm:[],message:'需补充 M²'};
  assert.equal(mainBeamStatus(system,good,partial),'已计算 T · S 待补入射参数');
  assert.equal(mainBeamStatus(system,partial,good),'已计算 S · T 待补入射参数');
  assert.equal(mainBeamStatus(system,good,good),'单程近轴传播');
});
