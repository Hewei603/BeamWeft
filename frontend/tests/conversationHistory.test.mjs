import assert from 'node:assert/strict';
import test from 'node:test';
import {mergeLatestMessages,prependMessages} from '../src/components/conversationHistory.js';

const page = (ids, next, session=1) => ({messages:ids.map(id=>({id})), message_page:{session_id:session,next_before_id:next}});

test('older pages can arrive while new messages arrive without duplication or gaps',()=>{
  const initial=page([21,22,23],21), earlier=page([17,18,19,20],17), updated=page([22,23,24],22);
  const headFirst=prependMessages(mergeLatestMessages(initial,updated),21,earlier);
  const historyFirst=mergeLatestMessages(prependMessages(initial,21,earlier),updated);
  assert.deepEqual(historyFirst, page([17,18,19,20,21,22,23,24],17));
  assert.deepEqual(headFirst,historyFirst);
  assert.equal(prependMessages(headFirst,21,earlier),headFirst);
  const skipped=page([42,43,44],42);
  assert.equal(mergeLatestMessages(headFirst,skipped),skipped);
  assert.equal(mergeLatestMessages(headFirst,page([23],null,2)).messages.length,1);
  assert.equal(prependMessages(headFirst,17,page([5],null,2)),headFirst);
});
