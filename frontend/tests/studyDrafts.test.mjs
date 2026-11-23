import test from 'node:test';
import assert from 'node:assert/strict';
import {StudyDraftShelf, storedStudyForm, studyDraftOrigin, studyFormSlot} from '../src/components/studyDrafts.js';

function storage(t,fail=false) {
  const data=new Map();
  const prior=globalThis.window;
  globalThis.window={sessionStorage:{
    getItem:key=>data.get(key) ?? null,
    setItem(key,value){if(fail)throw new Error('quota');data.set(key,value);},
    removeItem:key=>data.delete(key)
  }};
  t.after(()=>{if(prior)globalThis.window=prior;else delete globalThis.window;});
}

const method={name:'external_solver',provider:'optics-lab',version:'2'};
const form=token=>({values:{},raw:'{"reading":',revision:7,connections:{layout:{revision_id:7,port:'layout'}},choice:'',token});
const slot=studyFormSlot(method,studyDraftOrigin({reference:{run_id:17,port:'beam'}}));

test('late submission cannot clear a new edit from another instance of the same study form',t=>{
  storage(t);
  const shelf=()=>new StudyDraftShelf('bench',1,'analyst',()=>assert.fail('storage unexpectedly unavailable'));
  const old=shelf(), reopened=shelf();
  old.write(slot,form('first'));old.addEntry(slot,'光场','计算 #17');old.flushAll();
  reopened.write(slot,form('second'));
  assert.equal(old.remove(slot,'first'),false);assert.equal(reopened.list().length,1);
  assert.deepEqual(storedStudyForm(old.read(slot)),form('second'));
  assert.equal(reopened.remove(slot,'second'),true);reopened.flushAll();
  assert.equal(old.read(slot),null);assert.deepEqual(old.list(),[]);
});

test('unsaved inputs remain isolated by source, solver version, project and identity even under storage quota',t=>{
  storage(t,true);
  let warning=false;
  const source=new StudyDraftShelf('bench',3,'alice',()=>{warning=true;});
  source.write(slot,form('edit'));source.addEntry(slot,'光场','计算 #17');source.flushAll();
  assert.equal(warning,true);
  assert.deepEqual(source.read(slot),form('edit'));
  for(const other of [{name:'other_solver'},{provider:'different-lab'},{version:'3'}]){
    assert.equal(source.read(studyFormSlot({...method,...other},slot.origin)),null);
  }
  assert.equal(source.read(studyFormSlot(method,studyDraftOrigin({reference:{run_id:17,port:'other'}}))),null);
  assert.equal(new StudyDraftShelf('bench',3,'bob',()=>{}).read(slot),null);
  assert.equal(new StudyDraftShelf('bench',4,'alice',()=>{}).read(slot),null);
  source.remove(slot,'edit');source.flushAll();
});
