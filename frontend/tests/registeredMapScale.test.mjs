import assert from 'node:assert/strict';
import test from 'node:test';
import {mapScales,cellAt,mapDomains} from '../src/components/registeredMapScale.js';

test('calibrated maps retain negative camera noise and share a scale; differences have equal signed limits',()=>{
  const axes={x_edges_mm:[0,1,2,3],y_edges_mm:[0,1]}, one={x_edges_mm:[0,1],y_edges_mm:[0,1]};
  const result=mapScales({...axes,reference:[[null,2,9]],observed:[[null,-.5,11]],residual:[[null,-2.5,2]]});
  assert.deepEqual(result,{density:[-.5,11],difference:[-2.5,2.5],gaps:true});
  assert.deepEqual(mapScales({...one,reference:[[1]],observed:[[1]],residual:[[0]]}),
    {density:[0,1],difference:[0,0],gaps:false});
  assert.equal(mapScales({...one,reference:[[null]],observed:[[null]],residual:[[null]]}),null);
  assert.equal(mapScales({...one,observed:[[1]],residual:[[0]]}),null);
  assert.equal(mapScales({...one,reference:[[null]],observed:[[1]],residual:[[null]]}),null);
  assert.equal(mapScales({...axes,reference:[[1]],observed:[[1]],residual:[[0]]}),null);
});

test('a common physical interval keeps its aspect at different viewport sizes and both y orientations',()=>{
  const x=[-.3,.7],y=[1.7,-.3];
  for(const [width,height] of [[260,236],[206,330],[530,220]]) {
    const domain=mapDomains(x,y,width,height);
    const plottedWidth=width*(domain.x[1]-domain.x[0]),plottedHeight=height*(domain.y[1]-domain.y[0]);
    assert(Math.abs(plottedWidth/plottedHeight-.5)<1e-12);
    assert.deepEqual(mapDomains(x,[...y].reverse(),width,height),domain);
  }
});

test('a probe uses the declared cell boundaries, including nonuniform widths and out-of-field positions',()=>{
  const edges=[-.5,0,.01,3.5];
  assert.deepEqual([-.51,-.5,0,.009,.01,1,3.5,4,null].map(x=>cellAt(edges,x)),[-1,0,1,1,2,2,2,-1,-1]);
});
