import test from 'node:test';
import assert from 'node:assert/strict';
import { draftStorageKey, planDraftMerge } from '../src/components/projectDraft.js';

const project = () => ({title:'调试', system:{mode:'single_pass', wavelength_nm:808,
  elements:[{uid:'a',name:'空气',kind:'space',length_mm:20},{uid:'l',name:'透镜',kind:'lens',focal_mm:100}]},
  parts:[], assumptions:[], incident_paths:[], observation_planes:[]});

test('independent component edits can both be previewed without changing either original', () => {
  const base=project(), mine=project(), other=project();
  mine.system.elements[0].length_mm=37; other.system.elements[1].focal_mm=120;
  const plan=planDraftMerge(base,mine,other);
  assert.deepEqual(plan.groups.map(g=>g.kind),['local','remote']);
  const result=plan.build();
  assert.equal(result.system.elements[0].length_mm,37);
  assert.equal(result.system.elements[1].focal_mm,120);
  assert.deepEqual(base,project()); assert.equal(other.system.elements[0].length_mm,20);
});

test('competing prescriptions require a decision, including explicit zero values', () => {
  const base=project(), mine=project(), other=project();
  mine.system.elements[0].length_mm=0;other.system.elements[0].length_mm=35;
  const plan=planDraftMerge(base,mine,other);
  assert.throws(()=>plan.build(),/请选择/);
  assert.deepEqual(plan.build({'element:a':'local'}),mine);
  assert.deepEqual(plan.build({'element:a':'remote'}),other);
});

test('structural edits keep stock-index bindings, attachments to observation planes and topology together', () => {
  const base=project(), mine=project(), other=project();
  other.system.elements.unshift({uid:'x',name:'镜',kind:'mirror'});
  other.parts=[{element_index:2,inventory_id:9,locked_parameters:['focal_mm']}];
  mine.system.elements[1].focal_mm=37; mine.notes='手动记录';
  const plan=planDraftMerge(base,mine,other);
  assert.throws(()=>plan.build(),/关联/);
  assert.deepEqual(plan.build({optics:'remote'}),{...other,notes:'手动记录'});
  assert.deepEqual(plan.build({optics:'local'}),mine);
});

test('identical edits in both copies require no duplicate save, storage keys scope tab drafts by server case and account', () => {
  const base=project(), mine=project();mine.title='新名称';
  const plan=planDraftMerge(base,mine,{...mine});assert.deepEqual(plan.groups.map(x=>x.kind),['same']);
  assert.deepEqual(plan.build(),mine);
  assert.equal(new Set([draftStorageKey('a',1,'u'),draftStorageKey('b',1,'u'),draftStorageKey('a',1,'v'),draftStorageKey('a',2,'u')]).size,4);
  assert.equal(draftStorageKey('a',1,null),null);
});
