import test from 'node:test';
import assert from 'node:assert/strict';
import {axialPixel, axisTicks, displayProfile, fitInterval, profileInView, revealPosition, zoomInterval} from '../src/components/plotViewport.js';
import {moveAlongAxis, moveObservationPlane} from '../src/components/benchGeometry.js';

test('view navigation respects negative source coordinates, the selected optic and changed model extents', () => {
  const bounds = {start:-30, end:90};
  assert.deepEqual(zoomInterval(bounds,bounds,.5,0),{start:-30,end:30});
  assert.deepEqual(fitInterval(-70,20,bounds),{start:-30,end:-10});
  assert.deepEqual(fitInterval(78,20,bounds),{start:70,end:90});
  assert.equal(fitInterval(23,400,bounds),bounds);
  assert.deepEqual(zoomInterval({start:20,end:40},bounds,.5,-10),{start:25,end:35});
  assert.equal(axisTicks({start:0,end:0},5).ticks.length,1);
  const local={start:49,end:51};
  for (const invalid of [null,undefined,Infinity,-40,91]) assert.equal(revealPosition(local,bounds,invalid),local);
  assert.equal(revealPosition(local,bounds,49),local);
  assert.deepEqual(revealPosition(local,bounds,-30),{start:-30,end:-28});
  assert.deepEqual(revealPosition(local,bounds,20),{start:19,end:21});
  assert.deepEqual(revealPosition(local,bounds,90),{start:88,end:90});
});

test('a clipped envelope preserves coincident jumps, empty interiors and isolated known faces', () => {
  const points = [[-2,1],[0,3],[0,2],[1,3],[3,6],[5,4]];
  assert.deepEqual(profileInView(points,[4],{start:-1,end:4}),[
    [[-1,2],[0,3]],[[0,2],[1,3]],[[3,6],[4,5]]]);
  assert.deepEqual(profileInView(points,[4],{start:1.25,end:2.75}),[]);
  assert.deepEqual(profileInView([[10,3]],[],{start:9,end:10}),[[[10,3]]]);
  assert.deepEqual(profileInView([[5,1],[10,3]],[],{start:10,end:12}),[[[10,3]]]);
  assert.deepEqual(profileInView([[0,2],[20,4]],[],{start:5,end:10}),[[[5,2.5],[10,3]]]);
});

test('coincident reference faces retain each transformed radius without an artificial propagation distance', () => {
  const view={start:0,end:0};
  assert.equal(axialPixel(0, view, 60, 900),480);
  assert.deepEqual(profileInView([[0,.2],[0,.2],[0,.4],[0,.1]],[],view),
    [[[0,.2]],[[0,.4]],[[0,.1]]]);
  assert.deepEqual(profileInView([[0,.2],[0,.2+Number.EPSILON/8]],[],view),[[[0,.2]]]);
  const adjacent=[[0,.2],[10,.25],[10,.25+Number.EPSILON/4],[20,.3]];
  assert.equal(profileInView(adjacent,[],{start:0,end:20}).length,1);
  assert.equal(axialPixel(0, {start:-10,end:30}, 60, 900),270);
});

test('ruler labels remain distinct over a narrow interval far from zero', () => {
  const {offset,ticks} = axisTicks({start:1000,end:1000.0001},5);
  assert.equal(offset,1000);
  assert.equal(new Set(ticks.map(t=>t.label)).size,6);
  for (const {z,label} of ticks) assert(Math.abs(z-(offset+Number(label)))<1e-12);
  assert.deepEqual(axisTicks({start:999.8,end:1000.6},5).ticks.map(t=>t.label),
    ['999.8','1000','1000.2','1000.4','1000.6']);
});

test('display reduction bounds every radius error and preserves a waist, flat edges and coincident faces', () => {
  const samples=Array.from({length:1001},(_,i)=>[i/10,Math.hypot(.002,(i-843)/37)]);
  const tolerance=.012;
  const kept=displayProfile(samples,tolerance);
  assert(kept.length<samples.length/10);
  assert(kept.includes(samples[843]));
  let next=1;
  for(const [z,r] of samples) {
    while(kept[next][0]<z)next++;
    const [a,b]=kept[next-1], [c,d]=kept[next];
    assert(Math.abs(r-(b+(d-b)*(z-a)/(c-a)))<=tolerance+1e-12);
  }
  const faces=[[0,1],[1,2],[2,2],[3,2],[3,3],[3,1],[4,2]];
  assert.deepEqual(displayProfile(faces,10),[faces[0],faces[1],faces[3],faces[4],faces[5],faces[6]]);
});

test('editing a model coordinate preserves a representable displacement at any display scale', () => {
  const project={system:{elements:[{length_mm:1},{length_mm:1}]},observation_planes:[{uid:'p',z_mm:1}]};
  const target=1+2e-11, control={value:1,min:0,max:2,before:0,after:1};
  const moved=moveAlongAxis(project,control,target).system.elements;
  assert.equal(moved[0].length_mm,target);
  assert.equal(moved[0].length_mm+moved[1].length_mm,2);
  assert.equal(moveObservationPlane(project,{...control,uid:'p',fixed:true},target).observation_planes[0].z_mm,target);
});
