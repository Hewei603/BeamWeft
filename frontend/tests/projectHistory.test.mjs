import test from 'node:test';
import assert from 'node:assert/strict';
import { ProjectHistory } from '../src/components/projectHistory.js';

const layout = length => ({system:{elements:[{uid:'rail', length_mm:length}]}});

test('save identifies the clean model across undo, redo and equivalent edits', () => {
  const history = new ProjectHistory();
  const base = layout(1), second = layout(2), third = layout(3);
  history.reset(base);
  history.edit(second, '移动');
  history.edit(third, '拉伸');
  history.step('undo');
  const saved = layout(2);
  history.saved(saved);
  assert.equal(history.getSnapshot().dirty, false);
  assert.equal(history.getSnapshot().redoLabel, '拉伸');
  history.step('redo');
  assert.equal(history.project, third);
  history.step('undo');
  assert.equal(history.project, saved);
  history.step('undo');
  assert.equal(history.project, base);
  history.edit(layout(2));
  assert.equal(history.project, saved);
  assert.equal(history.getSnapshot().canRedo, false);
});

test('returning to the starting position and cancelled gestures preserve pending redo', () => {
  const history = new ProjectHistory(), base = layout(1);
  history.reset(base);
  history.edit(layout(2));
  history.step('undo');
  const pointer = history.begin(base, '移动镜片');
  for (let step=3; step<20; step++) history.move(pointer,layout(step));
  assert.equal(history.getSnapshot().undoLabel, '移动镜片');
  history.move(pointer,layout(1));
  history.finishGesture(pointer);
  assert.equal(history.getSnapshot().canUndo,false);
  assert.equal(history.getSnapshot().canRedo,true);
  const drag = history.begin(base);
  history.move(drag,layout(9));
  history.cancelGesture(drag);
  assert.equal(history.project,base);
  history.step('redo');
  assert.deepEqual(history.project,layout(2));
});

test('a late pointer cannot overwrite a separate experiment or a subsequent edit', () => {
  const history = new ProjectHistory(), base = layout(100), middle = layout(80), final = layout(30);
  history.reset(base);
  const old = history.begin(base);
  history.move(old,middle);
  history.edit(final);
  assert.equal(history.move(old,layout(20)),false);
  assert.equal(history.cancelGesture(old),false);
  assert.equal(history.project,final);
  history.step('undo');
  assert.equal(history.project,middle);
  history.step('undo');
  assert.equal(history.project,base);
  const active = history.begin(base);
  history.reset(final);
  history.finishGesture(active);
  assert.equal(history.begin(base),null);
  assert.equal(history.getSnapshot().canUndo,false);
  assert.equal(history.getSnapshot().canRedo,false);
});

test('editing during an unfinished gesture can be undone immediately and replayed once', () => {
  const history = new ProjectHistory(2), base = layout(1), changed = layout(4);
  history.reset(base);
  const token = history.begin(base);
  history.move(token,layout(3));
  history.move(token,changed);
  history.step('undo');
  assert.equal(history.project,base);
  assert.equal(history.move(token,layout(5)),false);
  history.step('redo');
  assert.equal(history.project,changed);
  history.edit(layout(5)); history.edit(layout(6));
  history.step('undo'); history.step('undo');
  assert.equal(history.project,changed);
  assert.equal(history.step('undo'),false);
});
