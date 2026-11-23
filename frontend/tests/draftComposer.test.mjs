import test from 'node:test';
import assert from 'node:assert/strict';
import { appendDraft, objectKey, objectLabel } from '../src/components/draftComposer.js';

test('supplement preserves handwritten content and existing independent evidence', () => {
  const result = appendDraft('My measured waist is 42 μm.', 'Please compare both axes.',
    { tool_call_ids: [2], attachment_ids: [7] }, { tool_call_ids: [3], attachment_ids: [7, 8] });
  assert.equal(result.message, 'My measured waist is 42 μm.\n\nPlease compare both axes.');
  assert.deepEqual(result.refs.tool_call_ids, [2, 3]);
  assert.deepEqual(result.refs.attachment_ids, [7, 8]);
});

test('a different project revision cannot silently change an existing draft context', () => {
  const before = { project_revision_id: 1, element_uids: ['lens-a'] };
  assert.throws(() => appendDraft('Keep this.', 'Other project.', before, { project_revision_id: 2 }), /另一工程版本/);
  assert.deepEqual(before, { project_revision_id: 1, element_uids: ['lens-a'] });
});

test('overlong template cannot truncate the user draft', () => {
  assert.throws(() => appendDraft('a'.repeat(24000), 'extra', {}, {}), /原输入已保留/);
});

test('object references cannot be moved between a working canvas and a pinned revision', () => {
  const draft = {element_uids:['new-lens']};
  const pinned = {project_revision_id:12};
  assert.throws(() => appendDraft('Compare.', 'Older model.', draft, pinned), /画布草稿/);
  assert.throws(() => appendDraft('Older model.', 'Compare.', pinned, draft), /画布草稿/);
  assert.deepEqual(appendDraft('Compare.', 'Camera.', draft, {attachment_ids:[2]}).refs.element_uids, ['new-lens']);
});

test('branch elements and network terminals retain their owners across quotes and draft restoration', () => {
  const element = {path_uid:'arm/a', element_uid:'L'};
  const terminal = {component:'arm/a', port:'L'};
  const restored = JSON.parse(JSON.stringify({project_objects:[element, terminal]}));
  const result = appendDraft('Compare', 'and include the source', restored,
    {project_objects:[{port:'L',component:'arm/a'}, {path_uid:'arm/a'}]});
  assert.deepEqual(result.refs.project_objects, [element, terminal, {path_uid:'arm/a'}]);
  assert.throws(() => appendDraft('Compare', 'history', result.refs, {project_revision_id:15}), /画布草稿/);
  assert.throws(() => appendDraft('history', 'Compare', {project_revision_id:15}, restored), /画布草稿/);
  const project = {incident_paths:[{uid:'arm/a',name:'支路甲',elements:[{uid:'L',name:'透镜'}]}],
    network:{components:[{uid:'arm/a',name:'检测器'}]}};
  assert.equal(objectLabel(project, element), '支路甲 / 透镜');
  assert.equal(objectLabel(project, terminal), '检测器 / 端口 L');
});

test('an explicit comparison to the working canvas cannot be changed into a comparison to another saved version', () => {
  const comparison = {followCanvas:true, artifact_ids:[12]};
  const quoted = appendDraft('Compare', 'with the working canvas', {}, comparison).refs;
  assert.throws(() => appendDraft('Compare', 'use another revision', quoted, {project_revision_id:15}), /画布草稿/);
  assert.throws(() => appendDraft('Pinned', 'compare to canvas', {project_revision_id:15}, comparison), /画布草稿/);
  assert.deepEqual(appendDraft('Compare', 'attach data', quoted, {attachment_ids:[7]}).refs.artifact_ids,[12]);
});

test('observation chips distinguish same-name planes, selected beams and whole-plane references', () => {
  const project={observation_planes:[{uid:'A',name:'探测面'},{uid:'B',name:'探测面'}],
    incident_paths:[{uid:'A',name:'参考光'},{uid:'B',name:'参考光'}]};
  const whole={plane_uid:'B'}, scoped={plane_uid:'B',beam_uid:'A'}, main={plane_uid:'B',beam_uid:'primary'};
  const other={plane_uid:'A',beam_uid:'A'}, arm={path_uid:'B',element_uid:'A'}, terminal={component:'B',port:'A'};
  const old=JSON.parse(JSON.stringify({project_objects:[whole,scoped,other,arm,terminal]}));
  const quote=appendDraft('两面','查看主光路',old,{project_objects:[{beam_uid:'A',plane_uid:'B'},main]});
  assert.deepEqual(quote.refs.project_objects,[whole,scoped,other,arm,terminal,main]);
  assert.equal(objectKey(whole),objectKey({...whole,beam_uid:null}));
  assert.equal(objectLabel(project,whole),'观察面 2 · 探测面');
  assert.equal(objectLabel(project,scoped),'观察面 2 · 探测面 / 2 · 参考光');
  assert.equal(objectLabel(project,other),'观察面 1 · 探测面 / 2 · 参考光');
  assert.equal(objectLabel(project,main),'观察面 2 · 探测面 / 主光路');
  assert.equal(objectLabel(null,scoped),'观察面 B / A');
  assert.deepEqual(quote.refs.project_objects.filter(obj=>objectKey(obj)!==objectKey(scoped)),[whole,other,arm,terminal,main]);
});
