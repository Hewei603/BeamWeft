import {inputWidget} from '../brand.js';
const scalarTypes = new Set(['string', 'integer', 'number', 'boolean']);
const composites = ['allOf','oneOf','not','if','then','else','dependentSchemas','patternProperties','discriminator'];
const plainObject = value => value != null && typeof value === 'object' && !Array.isArray(value);

// Leave conditional schemas, polymorphic records, and plugin-specific widgets in JSON.
export function resolveInputField(field, root) {
  if (!plainObject(field) || composites.some(key=>Object.hasOwn(field,key))) return null;
  if (field.anyOf) {
    const alternatives = field.anyOf.filter(option=>option.type !== 'null');
    if (alternatives.length !== 1) return null;
    const {anyOf,...annotations} = field;
    const resolved = resolveInputField(alternatives[0],root);
    return resolved && {...resolved,...annotations,nullable:anyOf.some(option=>option.type==='null')};
  }
  if (!field.$ref) return field;
  const ref = /^#\/\$defs\/([^/]+)$/.exec(field.$ref);
  const definition = ref && root.$defs?.[ref[1].replace(/~1/g,'/').replace(/~0/g,'~')];
  if (!definition || definition.$ref || definition.anyOf || composites.some(key=>Object.hasOwn(definition,key))) return null;
  return {...definition,...field};
}

export function objectInputSchema(field, root) {
  const outer = resolveInputField(field,root);
  if (!outer || inputWidget(outer)) return null;
  const list = outer.type === 'array';
  const object = list ? resolveInputField(outer.items,root) : outer;
  if (object?.type !== 'object' || !object.properties || inputWidget(object) ||
      plainObject(object.additionalProperties) || object.propertyNames || object.dependentRequired) return null;
  const properties = Object.create(null);
  for (const [key, declaration] of Object.entries(object.properties)) {
    const property = resolveInputField(declaration,root);
    if (!property || property.readOnly) return null;
    const widget=inputWidget(property);
    if(widget?widget!=='original-file':!scalarTypes.has(property.type))return null;
    properties[key] = {...property,required:object.required?.includes(key) ?? false};
  }
  if (!Object.keys(properties).length) return null;
  return {list,properties,min:outer.minItems,max:outer.maxItems,nullable:outer.nullable ?? false};
}

function matches(value, field) {
  if (value === null) return field.nullable;
  if(inputWidget(field)==='original-file')return plainObject(value) && Number.isSafeInteger(value.attachment_id)
    && value.attachment_id>0 && Object.keys(value).every(key=>key==='attachment_id' || key==='sha256')
    && (value.sha256==null || typeof value.sha256==='string');
  if (Object.hasOwn(field,'const') && value !== field.const) return false;
  if (field.enum && !field.enum.includes(value)) return false;
  if (field.type === 'integer') return Number.isSafeInteger(value);
  if (field.type === 'number') return Number.isFinite(value);
  return typeof value === field.type;
}

export function objectInputValue(config, raw) {
  if (raw == null || raw === '') return {value:undefined};
  let value;
  try {value=JSON.parse(raw);} catch {return {error:'请检查 JSON 的括号、引号和逗号。'};}
  if (value === null && config.nullable) return {value};
  const records = config.list ? value : [value];
  if (!Array.isArray(records) || !records.every(plainObject)) {
    return {error:config.list ? '逐项编辑需要一个对象数组。' : '逐项编辑需要一个对象。'};
  }
  for (const row of records) {
    for (const [key,entry] of Object.entries(row)) {
      if (!Object.hasOwn(config.properties,key) || !matches(entry,config.properties[key])) {
        return {error:'字段 '+key+' 的现有内容需要在 JSON 中编辑。'};
      }
    }
  }
  return {value};
}

export function newInputRecord(properties) {
  return Object.fromEntries(Object.entries(properties).filter(([,p])=>p.required && Object.hasOwn(p,'const'))
    .map(([key,p])=>[key,p.const]));
}
export function choiceLabel(field, value) {
  const labels=field['x-enum-labels'];
  if (labels && Object.hasOwn(labels, value) && typeof labels[value]==='string') return labels[value];
  return typeof value==='boolean' ? (value ? '是' : '否') : String(value);
}
