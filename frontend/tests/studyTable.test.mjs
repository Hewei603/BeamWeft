import test from 'node:test';
import assert from 'node:assert/strict';
import {sourceRowGroups, sourceRowLabel, tableRows} from '../src/components/studyTable.js';

test('grouped instrument readings keep their acquisition identity, missing values and unmeasured acquisitions', () => {
  const runs = [
    {label:'evening', channels:[{id:'A', voltage:0}, {id:'C'}]},
    {label:'morning', channels:[], reason:'acquisition interrupted'},
  ];
  const columns = {label:'Run', 'channels.*.id':'Channel', 'channels.*.voltage':'Voltage / V', reason:'Note'};
  const result = tableRows(runs, columns, 3);
  assert.deepEqual(result, {rows:[
    ['evening', 'A', 0, undefined],
    ['evening', 'C', undefined, undefined],
    ['morning', undefined, undefined, 'acquisition interrupted'],
  ], more:false});
  assert.deepEqual(tableRows(runs, columns, 1), {rows:[result.rows[0]], more:true});
});

test('independent collections inside one result never acquire an invented pairwise correspondence', () => {
  const rows = [{displacements:[{mm:3}, {mm:4}], exposures:[{ms:200}]}];
  const columns = {'displacements.*.mm':'Displacement', 'exposures.*.ms':'Exposure'};
  assert.deepEqual(tableRows(rows, columns, 2), {rows:[[[3,4],[200]]], more:false});
  assert.deepEqual(tableRows([{a:{b:7}}], {'a.b':'Value'}, 1), {rows:[[7]], more:false});
});

test('declarations resolve input positions against filtered rows in each original worksheet', () => {
  const definition={table_input_rows:{time:'sample',voltage:'sample'},input_ports:{time:{label:'时间'},voltage:{label:'电压'}}};
  const first={fields:['time'],source:{path:'time.xlsx'},selection:{sheet:'实验 A'},row_numbers:[5,8,12]};
  const second={fields:['voltage'],source:{path:'daq.csv'},row_numbers:[16,17,21]};
  const groups=sourceRowGroups(definition,[first,second]);
  assert.deepEqual(groups.map(group=>sourceRowLabel(group,2,true)),['时间 · time.xlsx · 实验 A · 第 8 行','电压 · daq.csv · 第 17 行']);
  const shared=sourceRowGroups(definition,[{...first,fields:['voltage','time']}]);
  assert.equal(shared.length,1);
  assert.equal(sourceRowLabel(shared[0],3,false),'time.xlsx · 实验 A · 第 12 行');
  for(const index of [undefined,0,1.5,4]) assert.match(sourceRowLabel(shared[0],index,false),/无对应行号/);
  assert.deepEqual(sourceRowGroups({},[first]),[]);
  assert.deepEqual(sourceRowGroups(definition,[{...first,row_numbers:[]}]),[]);
  assert.deepEqual(sourceRowGroups(definition,undefined),[]);
});
