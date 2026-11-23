import { test } from 'node:test';
import {removeOptic} from '../src/components/elementRemovalPlan.js';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { URL } from 'node:url';
import { appendOptic, instantiateOptic, savedPreview } from '../src/components/benchGeometry.js';
import { emptyFieldMessage, profileSegments, profileSeries, wavefrontAtFace } from '../src/components/opticalProfile.js';
import { initialScanAxes, scanAxis, scanFields, validScanPair } from '../src/components/scanAxes.js';
import { networkInputReferences, orderedSelections, studyFieldSelections } from '../src/components/layoutSelections.js';
import { groupPlotLabels } from '../src/components/plotLabels.js';

test('a reference and a probe can use independent defaults even while another path is selected on the canvas', () => {
  const selections=orderedSelections({incident_paths:[{uid:'arm',name:'Incoming path'}]},'arm');
  const selector={'x-beamweft-input':'path-uid'};
  const properties={reference:{...selector,title:'Reference path',default:'primary'},
    probe:{...selector,title:'Probe path'}, optional:{...selector,title:'Optional comparison path',default:null}};
  const fields=studyFieldSelections({properties,required:['probe']},selections);
  assert.equal(fields.reference.current,'primary');
  assert.equal(fields.reference.label,'Reference path');
  assert.equal(fields.probe.current,undefined);
  assert.equal(fields.probe.follow,'请选择');
  assert.equal(fields.optional.current,null);
  assert.equal(fields.optional.follow,'未指定');
  assert.equal(studyFieldSelections({properties:{probe:properties.probe}},selections).probe.current,'arm');
});

test('a change of viewport only regroups labels, preserving every physical object and coordinate', () => {
  const rows = Array.from({length:80}, (_, index) => ({
    element:{uid:String(index), name:'sample'}, start:Math.floor(index / 4), end:Math.floor(index / 4),
  }));
  const original = globalThis.structuredClone(rows);
  for (const width of [320, 768, 1440]) {
    const groups = groupPlotLabels(rows, z => 32 + z / 19 * (width - 64), width);
    assert.deepEqual(groups.flatMap(group => group.rows), original);
    let right = 0;
    for (const group of groups) {
      assert.ok(group.boxLeft >= right && group.boxWidth > 0);
      right = group.boxLeft + group.boxWidth;
      assert.ok(right <= width);
      assert.equal(group.rows.length % 4, 0);
    }
  }
  assert.deepEqual(rows, original);
});

test('the port drawing identifies a private element and a registered host plane independently',()=>{
  const project={system:{elements:[{uid:'a',name:'same',kind:'space',length_mm:10}]},
    incident_paths:[{uid:'b',name:'second beam',target_uid:'a',through_uid:'a',elements:[{uid:'a',name:'same',kind:'space',length_mm:3}]}],
    observation_planes:[{uid:'s1',name:'same station',target_uid:'a'},{uid:'s2',name:'same station',position_reference:'fixed_z'}],
    network:{drives:[{at:{component:'reader',port:'IN'},path_output:{path_uid:'b',scope:'arm',element_uid:'a'},
      profile:{kind:'layout',beam_uid:'primary',plane_uid:'s2'}}]}};
  const [source]=networkInputReferences(project,benchLayout(project),{planes:[{uid:'s2',target_uid:'a'}]});
  assert.deepEqual(source.rows.map(r=>r.target),[{pathUid:'b',scope:'arm',elementUid:'a'},
    {pathUid:'primary',scope:'host',elementUid:'a',planeUid:'s2'}]);
  assert.equal(source.rows[1].label,'2 · same station');
  project.network.drives[0].profile={kind:'layout',beam_uid:'b'};
  project.incident_paths[0].through_uid='removed';
  const [broken]=networkInputReferences(project,benchLayout(project),{});
  assert.ok(!broken.rows[1].target);
});

const catalog = JSON.parse(readFileSync(new URL('../../backend/app/workbench/element_catalog.json', import.meta.url), 'utf8'));
catalog.bounds = {n: {minimum: 1, maximum: 5}};
const { presets } = catalog;
import { adjustableGap, benchLayout, benchPositions, bindSharedRange, incidentControl, insertOptic, moveAlongAxis, moveIncidentElement, moveObservationPlane, newIncidentPath, observationControl, positionControl, reorderedElements, starterProject, withPathElements } from '../src/components/benchGeometry.js';

test('an index-matched plate remains a physical object when inserting optics or moving adjacent mounts',()=>{
  const project=starterProject('single_pass');
  const inserted=insertOptic(project,0,presets.space);
  const plate=inserted.system.elements[1];
  assert.equal(plate.medium_role,'medium');
  plate.n=1;
  assert.equal(adjustableGap(inserted,1),false);
  assert.equal(insertOptic(inserted,1,presets.lens),inserted);
  const moved=moveAlongAxis(inserted,positionControl(inserted,1),50);
  assert.equal(moved.system.elements[1].length_mm,plate.length_mm);
  assert.equal(benchPositions(moved)[1].start,50);
  assert.equal(benchPositions(moved).at(-1).end,100);
});

test('a shared route follows its endpoints across inserted optics and exposes a missing endpoint', () => {
  const elements = [{uid:'left',kind:'lens',focal_mm:30}, {uid:'right',kind:'space',length_mm:12}];
  const project = {system:{elements},incident_paths:[{uid:'feed',target_uid:'left',through_uid:'right',elements:[]}]};
  const getRoute=()=>benchLayout(project).routes[0];
  assert.deepEqual(getRoute().shared.map(row=>row.element.uid),['left','right']);
  elements.splice(1,0,{uid:'glass',kind:'thick_lens',length_mm:4,n:1.5});
  assert.deepEqual(getRoute().shared.map(row=>row.element.uid),['left','glass','right']);
  assert.equal(getRoute().endZ,16);
  const calibrated={...project.incident_paths[0],target_n:null,downstream:[{target_uid:'glass',n:1.42},{target_uid:'right',n:1.2}]};
  const moved=bindSharedRange(calibrated,elements,'glass','right');
  assert.equal(moved.target_n,1.42);
  assert.deepEqual(moved.downstream,[{target_uid:'right',n:1.2}]);
  assert.deepEqual(bindSharedRange(moved,elements,'left','right').downstream,calibrated.downstream);
  elements.pop();
  assert.equal(getRoute().broken,true);
  assert.deepEqual(getRoute().shared,[]);
});

test('saved reference-plane transfers do not draw or sample the interior', () => {
  const samples = [0,5,15,20].map(z_mm => ({z_mm, radius_mm:z_mm+1}));
  const plane = {samples, profile_breaks:[2], elements:[]};
  const trace = profileSeries(profileSegments(plane),'radius_mm');
  assert.deepEqual(trace.x, [0,5,null,15,20]);
  assert.deepEqual(trace.y, [1,6,null,16,21]);
  assert.deepEqual(savedPreview({planes:{tangential:plane}}).planes.tangential.profile_breaks,[2]);
  assert.equal(samples.length,4);
});

test('archived discrete states are markers; unknown interiors and curvature poles have no connecting line', () => {
  const a = {z_mm:0, radius_mm:.12, curvature_mm:null};
  const b = {z_mm:0, radius_mm:.24, curvature_mm:-60};
  const c = {z_mm:2, radius_mm:.25, curvature_mm:80};
  const compact = {input:a, elements:[b,c], samples:[]};
  const legacy = profileSeries(profileSegments(compact), 'radius_mm', 1000);
  assert.equal(legacy.mode,'markers');
  assert.deepEqual(legacy.y,[120,null,240,null,250]);
  const saved = savedPreview({planes:{tangential:compact}}).planes.tangential;
  assert.deepEqual(saved.profile_breaks,[1,2]);
  const fresh = {samples:[a,b,c]};
  assert.deepEqual(profileSeries(profileSegments(fresh),'radius_mm').x,[0,null,0,2]);
  assert.deepEqual(profileSeries(profileSegments(fresh,'curvature_mm'),'curvature_mm').y,[-60,null,80]);
});

test('a radius-only face and a planar wavefront have different readouts', () => {
  assert.equal(wavefrontAtFace({radius_mm:.3}),'未知');
  assert.equal(wavefrontAtFace({wavefront:'unknown',curvature_mm:null}),'未知');
  assert.equal(wavefrontAtFace({wavefront:'waist',curvature_mm:null}),'∞');
  assert.equal(wavefrontAtFace({q_real_mm:0,q_imag_mm:35,curvature_mm:null}),'∞');
  assert.equal(wavefrontAtFace({curvature_mm:-3}),'-3.0000');
  const system={mode:'single_pass',wavelength_nm:780,input_beam:{frame:'air.aligned-forward-ts.v2'}};
  assert.equal(emptyFieldMessage(system,{planes:{sagittal:{message:'补充波前'} }}),'补充波前');
});

test('scan fields belong to the exact installed prescription and use the path length once', () => {
  const prescription={study:'relay',provider:'lab',version:'1',parameters:{ratio:0.8}};
  const device={prescription,length_input:'span',wavelength_input:'colour',
    inputs:{properties:{ratio:{type:'number'},span:{type:'number'},colour:{type:'number'}}}};
  const models={devices:{relay:device}};
  const element={prescription,length_mm:12};
  const fields=scanFields(models,element);
  assert.deepEqual(Object.keys(fields),['parameters.ratio','length_mm']);
  assert.equal(fields['parameters.ratio'].value,.8);
  assert.equal(fields.length_mm.value,12);
  assert.deepEqual(scanFields(models,{...element,prescription:{...prescription,version:'2'}}),{});
});

test('incident positions share the unfolded origin while editing stays local to the selected path', () => {
  const project = starterProject('standing_wave');
  const host = benchPositions(project);
  const added = newIncidentPath(project, host[1].element.uid);
  assert(added && added.tangential.radius_mm === null);
  assert.equal(newIncidentPath(project, host[2].element.uid).through_uid, host[2].element.uid);
  const space = (uid, length_mm) => ({uid, kind:'space', length_mm, n:1});
  const lens = {uid:host[2].element.uid, kind:'lens', focal_mm:40};
  const pump = {...added, elements:[space('a',25),lens,space('b',25)]};
  const probe = {...pump, uid:'probe'};
  project.incident_paths = [pump,probe];
  const path = benchLayout(project).routes[0];
  assert.equal(path.sourceZ,-50);
  assert.equal(incidentControl(path,0),null);
  const control = incidentControl(path,1);
  assert.deepEqual([control.min,control.value,control.max],[-50,-25,0]);
  const moved = moveIncidentElement(project,control,-15);
  assert.equal(moved.system,project.system);
  assert.equal(moved.incident_paths[1],probe);
  assert.deepEqual(moved.incident_paths[0].elements.map(el=>el.length_mm),[35,undefined,15]);
  assert.equal(moveIncidentElement(project,control,1),project);
  assert.equal(benchLayout(moved).routes[0].sourceZ,-50);
  const interrupted = benchLayout({...project,incident_paths:[{...pump,through_uid:undefined,downstream:[{target_uid:'absent'}]}]}).routes[0];
  assert.equal(interrupted.broken,true);
  assert.equal(interrupted.endZ,host[1].end);
  const detached = benchLayout({...project,incident_paths:[{...pump,target_uid:'absent'}]}).routes[0];
  assert.equal(detached.sourceZ,null);
  assert.equal(incidentControl(detached,1),null);
});

test('branch locks follow a part through motion, reordering and removal of its neighbor', () => {
  const project = starterProject('standing_wave');
  const entrance = project.system.elements[1].uid;
  const gap = (uid, length_mm) => ({uid, kind:'space', n:1, length_mm});
  const lens = {uid:'f', kind:'lens', focal_mm:30};
  const part = {element_index:1, inventory_id:73, ownership:'owned', locked_parameters:['focal_mm']};
  const branch = {...newIncidentPath(project, entrance), elements:[gap('first',22),lens,gap('last',28)], parts:[part]};
  project.incident_paths = [branch];
  const route = benchLayout(project).routes[0];
  const control = incidentControl(route,1);
  const moved = moveIncidentElement(project,control,control.value+3).incident_paths[0];
  assert.deepEqual(moved.elements.map(e=>e.length_mm), [25,undefined,25]);
  assert.deepEqual(moved.parts,[part]);
  assert.equal(moved.elements[moved.parts[0].element_index],lens);
  const reordered = withPathElements(branch,reorderedElements(route.arm,1,1));
  assert.equal(reordered.elements[reordered.parts[0].element_index],lens);
  assert.deepEqual(reordered.parts[0],{...part, element_index:2});
  const removed = withPathElements(reordered,reordered.elements.slice(1));
  assert.equal(removed.elements[removed.parts[0].element_index],lens);
  assert.deepEqual(removed.parts[0],{...part,element_index:1});
  const spacer = {...part,element_index:0,locked_parameters:['length_mm']};
  const constrained = {...project,incident_paths:[{...branch, parts:[spacer,part]}]};
  assert.equal(incidentControl(benchLayout(constrained).routes[0],1), null);
});

test('internal optics trade neighboring gaps without moving the endpoints', () => {
  const base = starterProject('standing_wave');
  base.parts.push({ element_index: 2, inventory_id: 57, locked_parameters: ['radius_mm'] });
  const project = insertOptic(base, 1, presets.lens);
  assert.equal(project.parts[0].element_index, 4);
  const control = positionControl(project, 2);
  const moved = moveAlongAxis(project, control, 35);
  assert.deepEqual(benchPositions(moved).map(x => x.end), [0,35,35,100,100]);
  assert.equal(project.system.elements[1].length_mm, 50);
  assert.equal(moveAlongAxis(project, control, 120), project);
  assert.equal(moved.system.elements[2].uid, project.system.elements[2].uid);
});

test('a dragged observation retains its chosen reference and never displaces an optic', () => {
  const bench = insertOptic(starterProject('single_pass'), 0, presets.lens);
  const positions = benchPositions(bench);
  const fixed = { uid: 'camera', position_reference: 'fixed_z', z_mm: 75 };
  const attached = { uid: 'mount', target_uid: positions[2].element.uid, depth_mm: 25 };
  bench.observation_planes = [fixed, attached];
  for (const [plane, field, expected] of [[fixed, 'z_mm', 85], [attached, 'depth_mm', 35]]) {
    const control = observationControl(plane, positions);
    assert.equal(control.value, 75);
    const changed = moveObservationPlane(bench, control, 85);
    assert.equal(changed.observation_planes.find(p => p.uid === plane.uid)[field], expected);
    assert.equal(changed.system, bench.system);
    assert.equal(moveObservationPlane(bench, control, control.min - 1), bench);
    assert.equal(moveObservationPlane(bench, control, 75), bench);
  }
});

test('numeric origin, bonded optics and locked mechanical spacers remain constrained', () => {
  const project = insertOptic(starterProject('standing_wave'), 1, presets.lens);
  assert.equal(positionControl(project, 0), null);
  project.parts.push({element_index: 3, ownership: 'owned', locked_parameters: ['length_mm']});
  assert.equal(positionControl(project, 2), null);
  assert.equal(positionControl(project, 4), null);
  assert.equal(insertOptic(project, 3, presets.lens), project);
  project.system.elements[1].n = 1.8;
  assert.equal(positionControl(project, 1), null);
});

test('finite components displace air, removal restores air and preserves the reference planes', () => {
  const base = starterProject('standing_wave');
  for (const kind of ['space', 'thick_lens', 'grin', 'matrix', 'mirror']) {
    const placed = insertOptic(base, 1, presets[kind]);
    assert.equal(benchPositions(placed).at(-1).end, 100);
    assert.equal(benchPositions(removeOptic(placed, 2)).at(-1).end, 100);
    assert.equal(removeOptic(placed, 4), placed);
  }
  base.system.elements[1].length_mm = 2;
  assert.equal(insertOptic(base, 1, presets.thick_lens), base);
});

test('splitting the last shared gap preserves its exit face, calibrated medium and mounted detector', () => {
  const project=starterProject('standing_wave');
  const gap=project.system.elements[1];
  project.incident_paths=[{uid:'probe',target_uid:gap.uid,through_uid:gap.uid,target_n:1.001,downstream:[]}];
  project.observation_planes=[{uid:'near',target_uid:gap.uid,depth_mm:10},{uid:'far',target_uid:gap.uid,depth_mm:90}];
  const after=insertOptic(project,1,presets.thick_lens);
  const route=benchLayout(after).routes[0];
  const right=after.system.elements[3];
  assert.equal(route.endZ,100);
  assert.deepEqual(route.shared.map(row=>row.element.kind),['space','thick_lens','space']);
  assert.deepEqual(route.path.downstream,[{target_uid:right.uid,n:1.001}]);
  assert.equal(after.observation_planes[0],project.observation_planes[0]);
  assert.equal(after.observation_planes[1].target_uid,right.uid);
  assert.equal(route.shared[2].start+after.observation_planes[1].depth_mm,90);
});

test('end mirror can change total length with locked curvature intact', () => {
  const base = starterProject('standing_wave');
  base.parts.push({element_index:2,ownership:'owned',locked_parameters:['radius_mm']});
  const moved = moveAlongAxis(base, positionControl(base, 2), 240);
  assert.equal(benchPositions(moved).at(-1).end, 240);
  assert.deepEqual(moved.parts, base.parts);
  assert.equal(moved.system.elements[2].radius_mm, 200);
});

test('moving one reference plane in a segmented crystal moves the assembly as a rigid body', () => {
  const project = starterProject('single_pass');
  const space = (uid, length_mm, n) => ({ uid, length_mm, n, name: uid, kind: 'space' });
  project.system.elements = [space('before',25,1),space('cap',2,1.8),space('active',5,1.8),space('after',18,1)];
  project.parts = [1,2].map(element_index => ({element_index,role:'crystal',inventory_id:4,locked_parameters:['length_mm']}));
  project.assemblies = [{uid:'piece',name:'双段实物',path_uid:'primary',element_uids:['cap','active']}];
  const control = positionControl(project, 2);
  const moved = moveAlongAxis(project, control, 30);
  assert.match(control.hint,/整体移动/);
  assert.deepEqual(moved.system.elements.map(e=>e.length_mm),[28,2,5,15]);
});

test('an internal air interval moves with its physical component on an incident path', () => {
  const project = starterProject('single_pass');
  const host = project.system.elements[0];
  const space = (uid, length_mm) => ({uid, name:uid, kind:'space', length_mm, n:1});
  const lens = uid => ({uid, name:'lens', kind:'lens', focal_mm:25});
  const path = {uid:'arm',target_uid:host.uid,elements:[space('pre',10),lens('a'),space('inner',4),lens('b'),space('post',9)]};
  project.assemblies = [{uid:'tube',name:'barrel',path_uid:'arm',element_uids:['a','inner','b']}];
  project.incident_paths = [path];
  const route = benchLayout(project).routes[0];
  const motion = incidentControl(route,2);
  const moved = moveIncidentElement(project,motion,motion.value+3);
  assert.deepEqual(moved.incident_paths[0].elements.map(e=>e.length_mm || 0),[13,0,4,0,6]);
  assert.deepEqual(moved.assemblies,project.assemblies);
});

test('the same primitives can be free, one physical segment, or two rigid objects on separate paths', () => {
  const project = starterProject('single_pass');
  const host = project.system.elements[0].uid;
  const elements = Array.from({length: 8}, (_, i) => ({uid:String(i), name:'segment', kind:'space', n:1, length_mm:10}));
  project.incident_paths = ['free','mounted'].map(uid=>({uid, target_uid:host, elements}));
  project.assemblies = [
    {uid:'one',name:'sample',path_uid:'mounted',element_uids:['1']},
    {uid:'two',name:'relay',path_uid:'mounted',element_uids:['3','4','5']},
    {uid:'three',name:'cell',path_uid:'mounted',element_uids:['6','7']},
  ];
  const [free, mounted] = benchLayout(project).routes;
  assert.equal(adjustableGap(free.arm,1), true);
  assert.equal(adjustableGap(mounted.arm,1), false);
  const motion = incidentControl(mounted,1);
  const moved = moveIncidentElement(project, motion, motion.value+4);
  assert.deepEqual(moved.incident_paths[1].elements.map(e=>e.length_mm),[14,10,6,10,10,10,10,10]);
  assert.equal(moved.incident_paths[0],project.incident_paths[0]);
  const ids = (path, i, dir) => reorderedElements(path.arm, i, dir).map(e=>e.uid).join('');
  assert.equal(ids(free,4,1),'01235467');
  for (const member of [3,4,5]) assert.equal(ids(mounted,member,1),'01267345');
  assert.equal(ids(mounted,7,-1),'01267345');
  assert.equal(reorderedElements(mounted.arm,6,1),elements);
});


test('a new cavity grows downstream until both end mirrors exist', () => {
  let project = {system:{mode:'standing_wave',elements:[]},parts:[]};
  for(const kind of ['mirror','gap','mirror']) project = appendOptic(project,presets[kind]);
  const before = project.system.elements;
  assert.deepEqual(before.map(el=>el.kind), ['mirror','space','mirror']);
  const one = {...project,system:{...project.system,elements:before.slice(0,1)}};
  assert.deepEqual(removeOptic(one,0).system.elements,[]);
  const after = appendOptic(project,presets.lens).system.elements;
  assert.deepEqual(after.map(el=>el.uid), [before[0].uid,before[1].uid,after[2].uid,before[2].uid]);
  assert.equal(after[2].kind,'lens');
});

test('placing any catalog preset uses identical physical parameters on every route and independent identities', () => {
  const base = starterProject('standing_wave');
  for (const template of Object.values(presets)) {
    const inGap = insertOptic(base, 1, template).system.elements[2];
    const beforeMirror = appendOptic(base, template).system.elements[2];
    const onArm = instantiateOptic(template);
    assert.equal(new Set([inGap.uid, beforeMirror.uid, onArm.uid]).size, 3);
    for (const copy of [inGap, beforeMirror, onArm]) {
      const {uid, ...physics} = copy;
      assert.ok(uid);
      assert.deepEqual(physics, template);
    }
  }
  const matrix = instantiateOptic(presets.matrix);
  matrix.matrix_t[2] = -0.03;
  assert.equal(presets.matrix.matrix_t[2], 0);
});

test('suggested scan axes follow model values across geometries without inventing a radius for a flat surface', () => {
  for (const elements of [
    [presets.mirror, {...presets.gap, length_mm: 850}, {...presets.mirror, radius_mm: -300}],
    [presets.grin, presets.lens], [presets.thick_lens], [presets.matrix], [],
  ]) {
    const [x,y] = initialScanAxes(catalog,{system:{elements},parts:[]});
    if (!x) { assert.equal(y,null); continue; }
    assert.ok(validScanPair(x,y));
    for (const a of [x,y]) {
      assert.ok(a.start <= elements[a.element_index][a.parameter] && elements[a.element_index][a.parameter] <= a.stop);
    }
  }
  const flat = scanAxis(catalog,[presets.mirror],0,'radius_mm');
  assert.equal(flat.start,'');
  assert.equal(flat.stop,'');
  assert.equal(Object.hasOwn(scanFields(catalog,presets.gap),'n'),false);
  const constant={...presets.gap,index_reference:{model:null,wavelength_nm:1064},n:1};
  assert.equal(scanAxis(catalog,[constant],0,'n').start,1);
  assert.equal(validScanPair(flat,flat),false);
});
