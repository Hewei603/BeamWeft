import {test} from 'node:test';
import assert from 'node:assert/strict';
import {completeSectionLength, crystalDescription, independentLength, independentMaterial, makeCrystalRecord, sectionForm} from '../src/components/crystalRecord.js';

const blank = () => ({
  material:'', width_mm:'', height_mm:'', overall_length_mm:'', coating_note:'未知', notes:'',
  recordedSections:false, segments:[sectionForm()],
});

test('specimen records preserve empty sections only when their existence is known', () => {
  const untouched = blank();
  assert.equal(makeCrystalRecord(untouched), null);
  const measured = makeCrystalRecord({...untouched, width_mm:'2.3', overall_length_mm:'5.1'});
  assert.deepEqual(measured.segments, []);
  assert.equal(measured.width_mm, 2.3);
  assert.equal(measured.overall_length_mm, 5.1);
  const unknownSections = makeCrystalRecord({...untouched, segments:[sectionForm(), sectionForm()]});
  assert.equal(unknownSections.segments.length, 2);
  assert.equal(makeCrystalRecord({...untouched, recordedSections:true}).segments.length, 1);
  assert.equal(completeSectionLength(unknownSections.segments), null);
});

test('independent whole-part evidence, section evidence and incomplete lengths stay distinct', () => {
  const draft = {...blank(), overall_length_mm:'8.25', material:'Maker grade L3', segments:[
    sectionForm({length_mm:4.7, note:'measured'}), sectionForm({material:'Custom glass', doping_pct:0}),
  ]};
  const spec = makeCrystalRecord(draft);
  const [a,b] = spec.segments;
  assert.deepEqual([a.length_mm,a.material,a.role,a.note],[4.7,null,null,'measured']);
  assert.deepEqual([b.length_mm,b.material,b.role,b.doping_pct],[null,'Custom glass',null,0]);
  assert.equal(completeSectionLength(spec.segments), null);
  assert.equal(spec.overall_length_mm, 8.25);
  assert.equal(spec.material_label,'Maker grade L3');
  const description = crystalDescription({crystal_spec:spec});
  for (const fact of ['材料待确认', '4.7 mm', 'Custom glass', '段长待确认', 'Maker grade L3', '8.25 mm']) assert.ok(description.includes(fact),fact);
  assert.doesNotMatch(description,/null|undefined/);
});

test('historical section sums and names do not become new independent measurements', () => {
  const item = {material:'A + B', thickness_mm:3.7, crystal_spec:{segments:[{material:'A',length_mm:1.1},{material:'B',length_mm:2.6}]}};
  assert.equal(independentLength(item),'');
  assert.equal(independentMaterial(item),'');
  assert.equal(independentLength({...item,thickness_mm:3.9}),3.9);
  assert.equal(independentMaterial({...item,material:'Unresolved marking'}),'Unresolved marking');
  const marked = {...item, crystal_spec:{...item.crystal_spec,overall_length_mm:3.7,material_label:'A + B'}};
  assert.equal(independentMaterial(marked),'A + B');
  assert.equal(independentLength(marked),3.7);
});
