import test from 'node:test';
import assert from 'node:assert/strict';
import {StudyDraftShelf, storedStudyForm, studyDraftOrigin, studyFormSlot} from '../src/components/studyDrafts.js';
import {clearDraft, draftStorageKey, previousDraftKey, readDraft} from '../src/components/projectDraft.js';
import {inputWidget, isBuiltInProvider, previousBuiltInProvider, providerLabel, sameProvider} from '../src/brand.js';

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

test('saved built-in forms resume under their current provider while other labs and accounts remain distinct',t=>{
  storage(t);
  const currentSlot = studyFormSlot({...method,provider:'BeamWeft'},slot.origin);
  const archivedSlot = studyFormSlot({...method,provider:previousBuiltInProvider},slot.origin);
  const prefix=draftStorageKey('bench',1,'analyst','study-inputs');
  const archivedKey=previousDraftKey(prefix)+':'+JSON.stringify(archivedSlot);
  const currentKey=prefix+':'+JSON.stringify(currentSlot);
  const entry=form('saved');
  globalThis.window.sessionStorage.setItem(archivedKey,JSON.stringify(entry));
  const shelf = new StudyDraftShelf('bench',1,'analyst',()=>assert.fail('unexpected storage warning'));
  assert.equal(new StudyDraftShelf('bench',1,'other',()=>{}).read(currentSlot),null);
  assert.equal(shelf.read(studyFormSlot({...method,provider:previousBuiltInProvider+' Lab'},slot.origin)),null);
  assert.deepEqual(shelf.read(currentSlot),entry);
  assert.equal(globalThis.window.sessionStorage.getItem(archivedKey),null);
  assert.deepEqual(JSON.parse(globalThis.window.sessionStorage.getItem(currentKey)),entry);
  assert(shelf.remove(archivedSlot,'saved'));
  assert.equal(shelf.read(currentSlot),null);
});

test('a newer scoped canvas wins over its old copy and clearing it cannot revive stale edits',t=>{
  storage(t);
  const key=draftStorageKey('bench',1,'analyst'), storageArea=globalThis.window.sessionStorage;
  storageArea.setItem(previousDraftKey(key),JSON.stringify({title:previousBuiltInProvider}));
  storageArea.setItem(key,JSON.stringify({title:'updated'}));
  assert.deepEqual(readDraft(key),{title:'updated'});
  clearDraft(key);
  assert.equal(readDraft(key),null);
  storageArea.setItem(previousDraftKey(key),JSON.stringify({title:previousBuiltInProvider+' lab optics'}));
  storageArea.setItem=()=>{throw Error('quota')};
  assert.equal(readDraft(key).title,previousBuiltInProvider+' lab optics');
});

test('historical scientific input markers are understood and third-party names stay exact',()=>{
  assert(isBuiltInProvider('BeamWeft') && sameProvider('BeamWeft', previousBuiltInProvider));
  assert(!isBuiltInProvider(previousBuiltInProvider+' Lab'));
  assert.equal(providerLabel(previousBuiltInProvider+' Lab'),previousBuiltInProvider+' Lab');
  assert.equal(inputWidget({'x-laserclaw-input':'path-uid'}),'path-uid');
  assert.equal(inputWidget({'x-beamweft-input':'plane-uid','x-laserclaw-input':'path-uid'}),'plane-uid');
});

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
