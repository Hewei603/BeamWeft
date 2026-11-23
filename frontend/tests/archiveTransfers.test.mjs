import assert from 'node:assert/strict';
import {File} from 'node:buffer';
import test from 'node:test';
import {ArchiveTransfers} from '../src/components/archiveTransfers.js';
import {draftStorageKey,previousDraftKey} from '../src/components/projectDraft.js';

class MemoryStorage {
  data = new Map();
  get length() { return this.data.size; }
  key(n) { return [...this.data.keys()][n]; }
  getItem(k) { return this.data.get(k); }
  setItem(k,v) { this.data.set(k,v); }
  removeItem(k) { this.data.delete(k); }
}
const scope = (id,account) => draftStorageKey('https://example.test', id, account, 'uploads');
const failed = async () => { throw Error('offline'); };

test('unconfirmed uploads survive an upgrade without mixing project or user scopes',()=>{
  const storage=new MemoryStorage(), prefix=scope(1,'alice');
  const entry={key:'attempt',name:'S1.csv',label:'run/S1.csv',size:3,message:'offline'};
  storage.setItem(previousDraftKey(prefix)+'/attempt', JSON.stringify(entry));
  storage.setItem(previousDraftKey(scope(2,'alice'))+'/attempt', JSON.stringify({...entry,label:'other'}));
  const recovered=new ArchiveTransfers(prefix,storage);
  assert.deepEqual(recovered.getSnapshot().missing.map(row=>row.label),['run/S1.csv']);
  assert.equal(storage.getItem(previousDraftKey(prefix)+'/attempt'),undefined);
  recovered.discard('attempt');
  assert.equal(new ArchiveTransfers(prefix,storage).getSnapshot().missing.length,0);
  assert.equal(new ArchiveTransfers(scope(2,'alice'),storage).getSnapshot().missing[0].label,'other');
});

test('unconfirmed receipts survive reload only in their project and principal scope', async () => {
  const storage = new MemoryStorage(), key = scope(3,'A');
  const uploader = new ArchiveTransfers(key,storage);
  const file = new File(['12'],'scan.tsv');
  await uploader.upload([{file,key:'attempt1'}], failed);
  for (const other of [scope(3,'B'),scope(4,'A')]) assert.deepEqual(await new ArchiveTransfers(other,storage).wait(),[]);
  const restored = new ArchiveTransfers(key,storage);
  assert.equal(restored.getSnapshot().missing[0].file,undefined);
  let calls=0;
  const send = entry => { calls++; return {id:5,upload_id:entry.key,relative_path:entry.label,content_hash:entry.hash}; };
  await restored.upload([{file:new File(['34'],'scan.tsv'),key:'attempt1',reselected:true}],send);
  assert.equal(calls,0);assert.match(restored.getSnapshot().missing[0].message,/内容.*不同/);
  await restored.upload([{file:new File(['12'],'renamed.tsv'),key:'attempt1',reselected:true}],send);
  assert.equal(calls,1);assert.equal(storage.length,0);
});

test('server confirmation needs the same unique upload, original path and bytes', async () => {
  const storage = new MemoryStorage(), store = new ArchiveTransfers('scope',storage);
  await store.upload([{file:new File(['instrument'],'raw.dat'),key:'alpha',composer:true}],failed);
  const entry = store.getSnapshot().missing[0];
  const receipt = {id:7,upload_id:'alpha',relative_path:'raw.dat',content_hash:entry.hash};
  for (const change of [{upload_id:'beta'},{relative_path:'other/raw.dat'},{content_hash:'00'}]) {
    store.reconcile([{...receipt,...change}]);assert.equal((await store.wait()).length,1);
  }
  let selected;store.onArchived((file,composer)=>{selected=[file.id,composer];});
  store.reconcile([receipt]);
  assert.deepEqual(selected,[7,true]);assert.equal(storage.length,0);assert.deepEqual(await store.wait(),[]);
});

test('reopening before the first fingerprint completes needs a fresh upload identity', async () => {
  const storage=new MemoryStorage();
  storage.setItem('scope/interrupted',JSON.stringify({key:'interrupted',name:'scan.txt',label:'scan.txt',size:2}));
  const store=new ArchiveTransfers('scope',storage);
  let posted=0;
  const send=entry=>{posted++; return {id:4,upload_id:entry.key,relative_path:entry.label,content_hash:entry.hash};};
  const file=new File(['26'],'scan.txt');
  await store.upload([{file,key:'interrupted',reselected:true}],send);
  assert.equal(posted,0);assert.equal((await store.wait()).length,1);
  store.discard('interrupted');
  await store.upload([{file,key:'new'}],send);
  assert.equal(posted,1);assert.deepEqual(await store.wait(),[]);
});
