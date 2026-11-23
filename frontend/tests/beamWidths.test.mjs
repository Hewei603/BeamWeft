import test from 'node:test';
import assert from 'node:assert/strict';
import {declaredRadiusBasis, setRadiusBasis, stationRadiusMarks} from '../src/components/beamWidths.js';

test('a change in radius convention replaces only that declaration', () => {
  const scenario = {radius_mm:.1,m2:1,wavefront:'waist',equivalent_radius:true};
  const measured = setRadiusBasis(scenario,'second_moment');
  assert.deepEqual(measured,{radius_mm:.1,m2:1,wavefront:'waist',second_moment_radius:true});
  assert.equal(declaredRadiusBasis(measured),'second_moment');
  const equivalent = setRadiusBasis(measured,'equivalent');
  assert.equal(declaredRadiusBasis(equivalent),'equivalent');
  assert.equal(equivalent.second_moment_radius,false);
  const auto = setRadiusBasis(equivalent,'model');
  assert.equal(Object.hasOwn(auto,'equivalent_radius'),false);
  assert.equal(declaredRadiusBasis(auto),'model');
  assert.equal(declaredRadiusBasis({radius_mm:.2,m2:1}),'model');
});

test('a station plots the stable axes actually reported for the visible paths', () => {
  const beams = [
    {uid:'primary',radii_mm:{tangential:.2,sagittal:.2}}, {uid:'left',radii_mm:{sagittal:.1}},
    {uid:'right',radii_mm:{tangential:.3,sagittal:.4}}, {uid:'empty',radii_mm:{}},
  ];
  assert.deepEqual(stationRadiusMarks(beams,'left'),[
    {uid:'primary',axis:'T/S',radius:.2},{uid:'left',axis:'S',radius:.1},
  ]);
  assert.deepEqual(stationRadiusMarks(beams,'right').map(m=>m.axis),['T/S','T','S']);
  assert.deepEqual(stationRadiusMarks(beams,'empty').map(m=>m.uid),['primary']);
  assert.deepEqual(stationRadiusMarks(undefined,'left'),[]);
});
