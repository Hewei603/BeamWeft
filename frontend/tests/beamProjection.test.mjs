import test from 'node:test';
import assert from 'node:assert/strict';
import {engineeringInView,engineeringProfile,initialEngineeringView} from '../src/components/beamProjection.js';

test('local transverse scale includes edge interpolants, signed centers and separate faces without joining gaps',()=>{
  const profile=engineeringProfile({center_profile:[[0,-10],[10,-2],[20,-1],[30,2]],center_profile_breaks:[2],
    profile:[[0,3],[10,1],[20,.1],[30,.2]],profile_breaks:[2]});
  const middle=engineeringInView(profile,{start:2.5,end:7.5});
  assert.deepEqual(middle.centers,[[[2.5,-8],[7.5,-4]]]);
  assert.equal(middle.extent,10.5); // lower edge is -8 - 2.5
  const gap=engineeringInView(profile,{start:12,end:18});
  assert.deepEqual(gap.centers,[]);assert.deepEqual(gap.bands,[]);assert.equal(gap.extent,0);
  const faces=engineeringProfile({center_profile:[[0,4],[5,4],[5,-3],[10,-3]],
    profile:[[0,.2],[5,.2],[5,2],[10,2]]});
  assert.equal(engineeringInView(faces,{start:5,end:7}).extent,5);
});

test('initial orientation uses specified alignment and known archived coordinates of the same physical beam',()=>{
  const axes={tangential:{center_profile:[[0,.2]]},sagittal:{center_profile:[[0,-.1]]}};
  const source={launch_ray:{t_offset_mm:.2,s_offset_mm:-.1}};
  const path={uid:'second',launch_ray:{s_angle_mrad:2}};
  const analysis={planes:axes,sections:{paths:[{uid:'second',axes}]}};
  const record={project:{system:{source},incident_paths:[{uid:'first'},path]},analysis};
  assert.deepEqual(initialEngineeringView(record),{projection:'tangential',pathUid:null});
  assert.deepEqual(initialEngineeringView({...record,project:{...record.project,system:{}}}),{projection:'sagittal',pathUid:'second'});
  assert.deepEqual(initialEngineeringView({...record,analysis:{planes:{}}}),{projection:'width',pathUid:null});
  assert.deepEqual(initialEngineeringView({project:{system:{source:{launch_ray:{t_angle_mrad:null,s_offset_mm:0}}}},analysis}),
    {projection:'width',pathUid:null});
});

test('engineering coordinates follow the signed ray and preserve independent waist samples',()=>{
  const ray=engineeringProfile({center_profile:[[0,-.25],[100,.25]],profile:[[0,.5],[50,.125],[100,.25]]});
  assert.deepEqual(ray.bands,[{upper:[[0,.25],[50,.125],[100,.5]],lower:[[0,-.75],[50,-.125],[100,0]]}]);
  assert.equal(ray.extent,.75);
});

test('a known ray can end before a width, continue without a width, or have no recorded coordinates',()=>{
  assert.deepEqual(engineeringProfile({profile:[[0,1],[10,2]]}),{centers:[],bands:[],extent:0});
  const ray=engineeringProfile({center_profile:[[-10,-3],[10,3]],profile:[[0,1],[20,1]]});
  assert.deepEqual(ray.bands,[{upper:[[0,1],[10,4]],lower:[[0,-1],[10,2]]}]);
  assert.deepEqual(ray.centers,[[[-10,-3],[10,3]]]);
  assert.deepEqual(engineeringProfile({center_profile:[[0,-1]]}).bands,[]);
});

test('an equivalent device with two known faces has no interpolated center or envelope inside',()=>{
  const ray=engineeringProfile({center_profile:[[0,1],[10,3],[20,3]],center_profile_breaks:[1],
    profile:[[0,.5],[10,1],[20,1]],profile_breaks:[1]});
  assert.deepEqual(ray.centers,[[[0,1]],[[10,3],[20,3]]]);
  assert.deepEqual(ray.bands,[{upper:[[0,1.5]],lower:[[0,.5]]},{upper:[[10,4],[20,4]],lower:[[10,2],[20,2]]}]);
});

test('coincident entrance and exit discontinuities keep their own incident beam sizes',()=>{
  const ray=engineeringProfile({center_profile:[[-10,0],[0,1],[0,-1],[10,0]],
    profile:[[-10,.5],[0,.25],[0,1],[10,.5]]});
  assert.deepEqual(ray.bands,[{upper:[[-10,.5],[0,1.25]],lower:[[-10,-.5],[0,.75]]},
    {upper:[[0,0],[10,.5]],lower:[[0,-2],[10,-.5]]}]);
});
