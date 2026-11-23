import {benchLayout, incidentControl, moveAlongAxis, moveIncidentElement, positionControl} from './benchGeometry.js';
import {positionText} from './plotViewport.js';

const decimal = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
const identifier = value => typeof value === 'string' && value.length > 0;

export function enteredPosition(text, control) {
  const value = typeof text === 'string' ? text.trim() : '';
  const position = decimal.test(value) ? Number(value) : NaN;
  if (Number.isFinite(position) && position >= control.min && position <= control.max) return position;
  const reason = Number.isFinite(position) ? '位置超出可调范围。' : '位置需要一个有效数值。';
  throw Error(reason + '允许的位置为 ' + positionText(control.min) + ' 至 ' + positionText(control.max) + ' mm。按 Esc 可恢复。');
}

export function setPositionEntry(entries, pathUid, elementUid, text, origin) {
  if (!identifier(pathUid) || !identifier(elementUid)) throw Error('请先为这条光路和元件指定稳定标识，再编辑它的位置。');
  const index = entries.findIndex(entry => entry.pathUid === pathUid && entry.elementUid === elementUid);
  if (text == null) return index < 0 ? entries : entries.filter((_, i) => i !== index);
  if (typeof text !== 'string') throw Error('位置输入需要保留原始文本。');
  const previous = entries[index];
  if (previous?.text === text && (previous.origin || !origin)) return entries;
  const firstLocation = previous?.origin || origin;
  const entry = {pathUid, elementUid, text, ...(firstLocation && {origin:firstLocation})};
  return index < 0 ? [...entries, entry] : entries.map((previous, i) => i === index ? entry : previous);
}

function originalLocation(entry) {
  const origin = entry.origin;
  if (!origin || !Number.isSafeInteger(origin.ordinal) || origin.ordinal <= 0) return '';
  const path = entry.pathUid === 'primary' ? '主光路' : (typeof origin.pathName === 'string' && origin.pathName.trim()) || '未命名支路';
  const element = (typeof origin.elementName === 'string' && origin.elementName.trim()) || '未命名元件';
  const position = Number.isFinite(origin.positionMm) ? '，轴向位置为 ' + positionText(origin.positionMm) + ' mm' : '';
  return '输入时选中的是' + path + '的第 ' + origin.ordinal + ' 个元件「' + element + '」' + position + '。';
}

export function resolvePositionEntry(project, entry, geometry = benchLayout(project)) {
  const primary = entry.pathUid === 'primary';
  const route = primary ? null : geometry.routes.find(candidate => candidate.path.uid === entry.pathUid);
  const recovery = () => originalLocation(entry) + '可撤销光路修改，或舍弃此位置输入。';
  if (!primary && !route) throw Error('这条位置输入所属的支路已移除。' + recovery());
  const rows = primary ? geometry.positions : route.rows;
  // The same uid can be used on two paths. An index or a display name can also change on reorder.
  const row = identifier(entry.elementUid) && rows.find(candidate => candidate.element.uid === entry.elementUid);
  const pathName = primary ? '主光路' : route.path.name || '未命名支路';
  if (!row) throw Error(pathName + '中与这项输入对应的元件已移除。' + recovery());
  const name = pathName + ' · ' + (row.element.name || '第 ' + (row.index + 1) + ' 个元件');
  const control = primary ? positionControl(project, row.index, rows) : incidentControl(route, row.index);
  if (!control) throw Error('元件「' + name + '」在当前布局中无法轴向移动。请核对接入位置和可调空气间距，或舍弃此位置输入。');
  return {row, control, name};
}

export function applyPositionEntry(project, entry, geometry) {
  const {control} = resolvePositionEntry(project, entry, geometry);
  const position = enteredPosition(entry.text, control);
  return entry.pathUid === 'primary' ? moveAlongAxis(project, control, position) : moveIncidentElement(project, control, position);
}

export function recoverPositionEntries(entries) {
  if (!Array.isArray(entries)) return [];
  const owners = new Map();
  const usable = [];
  for (const entry of entries) {
    if (!identifier(entry?.pathUid) || !identifier(entry?.elementUid) || typeof entry.text !== 'string') continue;
    if (!owners.has(entry.pathUid)) owners.set(entry.pathUid, new Map());
    const counts = owners.get(entry.pathUid);
    counts.set(entry.elementUid, (counts.get(entry.elementUid) || 0) + 1);
    usable.push(entry);
  }
  // Conflicting entries have no reliable order or value. Independent inputs are still recoverable.
  const unique = usable.filter(entry => owners.get(entry.pathUid).get(entry.elementUid) === 1);
  return unique.length === entries.length ? entries : unique;
}
