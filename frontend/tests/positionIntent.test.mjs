import {test} from 'node:test';
import assert from 'node:assert/strict';
import {benchLayout} from '../src/components/benchGeometry.js';
import {applyPositionEntry, enteredPosition, recoverPositionEntries, resolvePositionEntry, setPositionEntry} from '../src/components/positionIntent.js';

const gap = (uid, length_mm) => ({uid, name:uid, kind:'space', length_mm, n:1});
const lens = () => ({uid:'L1', name:'透镜', kind:'lens', focal_mm:60});
const entry = (pathUid, text) => ({pathUid, elementUid:'L1', text});
function bench() {
  return {system:{elements:[gap('a',60),lens(),gap('b',80)]},parts:[],
    incident_paths:[{uid:'pump',name:'泵浦',target_uid:'b',through_uid:'b',elements:[gap('in',18),lens(),gap('out',12)]}]};
}

test('a pending position preserves its original text, observing the normal numeric and geometry bounds only at apply time', () => {
  const control = {min:-30, max:50};
  for (const [text, value] of [[' +5.e0 ',5],['-.75',-.75],['1.4e+1',14],['-3E1',-30],['50',50]])
    assert.equal(enteredPosition(text,control),value);
  for (const text of ['','  ','1e-','0x10','0b11','Infinity','NaN','1e800','1,3','--1'])
    assert.throws(()=>enteredPosition(text,control), /有效数值/);
  for (const text of ['-30.1','50.1']) assert.throws(()=>enteredPosition(text,control), /超出.*-30.*50 mm/);
});

test('duplicate names and UIDs on different paths produce independent position drafts; cancel removes only its own value', () => {
  let entries = setPositionEntry([], 'pump', 'L1', '1e-');
  const previous = entries;
  entries = setPositionEntry(entries, 'primary', 'L1', '');
  assert.deepEqual(previous,[entry('pump','1e-')]);
  const updated = setPositionEntry(entries, 'pump', 'L1', '42');
  assert.deepEqual(entries,[entry('pump','1e-'),entry('primary','')]);
  assert.deepEqual(updated,[entry('pump','42'),entry('primary','')]);
  assert.equal(setPositionEntry(updated,'pump','L1','42'),updated);
  assert.deepEqual(setPositionEntry(updated,'primary','L1',null),[entry('pump','42')]);
  assert.throws(()=>setPositionEntry([], 'primary', undefined, '4'), /稳定标识/);
});

test('host and incident edits follow the same coordinate convention as dragging and respect each path’s physical constraints', () => {
  const project = bench();
  const geometry = benchLayout(project);
  assert.equal(resolvePositionEntry(project,entry('primary','63'),geometry).control.value,60);
  const primary = applyPositionEntry(project,entry('primary','63'),geometry);
  assert.deepEqual(primary.system.elements.map(e=>e.length_mm),[63,undefined,77]);
  assert.equal(primary.incident_paths,project.incident_paths);
  const incident = resolvePositionEntry(project,entry('pump','43'),geometry);
  assert.equal(incident.name,'泵浦 · 透镜');
  assert.deepEqual([incident.control.value,incident.control.min,incident.control.max],[48,30,60]);
  const moved = applyPositionEntry(project,entry('pump','43'),geometry);
  assert.deepEqual(moved.incident_paths[0].elements.map(e=>e.length_mm),[13,undefined,17]);
  assert.equal(moved.system,project.system);
  assert.equal(applyPositionEntry(project,entry('pump','48')),project);
  assert.throws(()=>applyPositionEntry(project,entry('pump','61')),/超出.*30.*60/);
});

test('a pending move follows its stable identity after reordering and checks the new neighbors before applying', () => {
  const project = bench();
  const [a,l,b] = project.system.elements;
  const reordered = {...project,system:{elements:[gap('leading',13),a,l,b]}};
  const request = entry('primary','80');
  const {row,control} = resolvePositionEntry(reordered,request);
  assert.equal(row.index,2);
  assert.equal(control.value,73);
  assert.deepEqual(applyPositionEntry(reordered,request).system.elements.map(e=>e.length_mm),[13,67,undefined,73]);
  const removed = {...project,system:{elements:[a,{...l,uid:'different'},b]}};
  assert.throws(()=>applyPositionEntry(removed,request),/元件.*已移除/);
  const anonymous = {...project,system:{elements:[a,{...l,uid:''},b]}};
  assert.throws(()=>applyPositionEntry(anonymous,{...request,elementUid:'index:1'}),/元件.*已移除/);
  const immovable = {...project,parts:[{element_index:0,locked_parameters:['length_mm']}]};
  assert.throws(()=>applyPositionEntry(immovable,request),/当前布局中无法轴向移动/);
  assert.throws(()=>applyPositionEntry({...project,incident_paths:[]},entry('pump','43')),/支路.*已移除/);
});

test('damaged position records preserve unambiguous entries and incomplete user text on another path', () => {
  const good = [entry('primary','1e-'),entry('pump','-12')];
  assert.equal(recoverPositionEntries(good),good);
  const mixed = [null, ...good, entry('primary','5'), {pathUid:'third',elementUid:'L2',text:6}];
  assert.deepEqual(recoverPositionEntries(mixed),[entry('pump','-12')]);
  assert.deepEqual(recoverPositionEntries('damaged'),[]);
});

test('deleted elements and paths retain a readable original location without using it to locate a replacement', () => {
  const project = bench();
  const pathUid = project.incident_paths[0].uid;
  const origin = {pathName:'泵浦',elementName:'透镜',ordinal:2,positionMm:48};
  const entries = setPositionEntry([],pathUid,'L1','1e-',origin);
  const edited = setPositionEntry(entries,pathUid,'L1','-7',{...origin,ordinal:3,positionMm:50});
  assert.deepEqual(edited[0].origin,origin);
  const lost = {...project,incident_paths:[]};
  assert.throws(()=>applyPositionEntry(lost,edited[0]),/支路已移除。.*泵浦的第 2 个元件「透镜」.*48 mm/);
  const replacement = globalThis.structuredClone(project);
  replacement.incident_paths[0].elements[1].uid = 'new';
  assert.throws(()=>applyPositionEntry(replacement,edited[0]),/元件已移除。.*48 mm/);
  const anonymous = {...edited[0],origin:{...origin,pathName:'',elementName:null}};
  assert.throws(()=>applyPositionEntry(lost,anonymous),/未命名支路的第 2 个元件「未命名元件」/);
  assert.throws(()=>applyPositionEntry(lost,entry(pathUid,'4')),error=>!error.message.includes(pathUid)&&error.message.includes('支路已移除'));
});
