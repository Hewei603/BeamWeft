import {strict as assert} from 'node:assert';
import test from 'node:test';
import {maxNetworkScale,networkView,readableScale,zoomNetworkView} from '../src/components/networkCamera.js';

test('a sparse network opens on its selected node while fit includes all components',()=>{
  const bounds={x:-100,y:20,width:5200,height:700}, size={width:330,height:250};
  const selection={x:5000,y:560};
  const local=networkView(bounds,size,null,selection);
  assert.equal(local.scale,readableScale);
  assert(local.x<=selection.x && local.x+local.width>=selection.x);
  assert(local.y<=selection.y && local.y+local.height>=selection.y);
  const fit=networkView(bounds,size,{fit:true},selection);
  assert.equal(fit.scale,330/5200);
  assert(fit.x<=bounds.x && fit.x+fit.width>=bounds.x+bounds.width);
  assert(fit.y<=bounds.y && fit.y+fit.height>=bounds.y+bounds.height);
});

test('zoom keeps an interior anchor at the same screen coordinates and clamps panning',()=>{
  const bounds={x:0,y:0,width:9000,height:9000},size={width:600,height:400};
  const view=networkView(bounds,size,{x:4000,y:2000,scale:1});
  const anchor={x:3990,y:2010};
  const next=networkView(bounds,size,zoomNetworkView(view,2,anchor));
  assert.equal((anchor.x-next.x)*next.scale,(anchor.x-view.x)*view.scale);
  assert.equal((anchor.y-next.y)*next.scale,(anchor.y-view.y)*view.scale);
  const edge=networkView(bounds,size,{scale:1e6,x:-1e6,y:1e6});
  assert.equal(edge.scale,maxNetworkScale);
  assert.equal(edge.x,0);assert.equal(edge.y+edge.height,bounds.height);
});
