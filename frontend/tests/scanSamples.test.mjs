import test from 'node:test';
import assert from 'node:assert/strict';
import {scanSamples,scanCsv} from '../src/components/scanSamples.js';

test('archived null cells stay distinct from computed unstable cavities and true zeros in CSV',()=>{
  const scan={status:'completed',grid_points:3,invalid_count:1,x:[-1,0,1],y:[2],
    x_axis:{parameter:'a'},y_axis:{parameter:'b'},maps:{stable_both:[[null,0,1]],tangential:[[null,-1,0]],sagittal:[[null,-1,.2]]}};
  const legacy=scanSamples(scan);
  assert.equal(legacy[0].status,'invalid');
  assert.match(legacy[0].reason,/旧扫描/);
  assert.equal(legacy[1].status,'completed');assert.equal(legacy[1].stable,0);
  const reason='镜架, "A"\n行程';
  const current=scanSamples({...scan,invalid_groups:[{message:reason,cells:[[0,0]]}]});
  assert.equal(current[0].reason,reason);
  assert.equal(scanCsv(scan,current),'x_a,y_b,m_T,m_S,stable_both,status,reason\r\n-1,2,,,,invalid,"镜架, ""A""\n行程"\r\n0,2,-1,-1,0,completed,\r\n1,2,0,0.2,1,completed,');
});

test('partially determined single-pass outputs retain a known axis and the other axis reason',()=>{
  const scan={mode:'single_pass',parameter:'length_mm',points:[
    {value:0,status:'invalid',message:'检查光程'}, {value:1,status:'partial',tangential:{output:{radius_mm:.24}},
      sagittal:{message:'缺少波前'}}],valid_count:0};
  const rows=scanSamples(scan);
  assert.equal(rows[1].status,'partial');assert.match(rows[1].reason,/S · 缺少波前/);
  assert.match(scanCsv(scan,rows),/1,,,0.24,,,partial,S · 缺少波前/);
});
