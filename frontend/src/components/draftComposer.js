import {componentLabels, pathsOf} from './layoutSelections.js';

export function objectKey(object) {
  if ('plane_uid' in object) return JSON.stringify(['observation', object.plane_uid, object.beam_uid ?? null]);
  return JSON.stringify('path_uid' in object
    ? ['path', object.path_uid, object.element_uid ?? null]
    : ['port', object.component, object.port]);
}

export function distinctObjects(objects = []) {
  return [...new Map(objects.map(object => [objectKey(object), object])).values()];
}

export function objectLabel(project, object) {
  if ('plane_uid' in object) {
    const plane=componentLabels(project?.observation_planes || []).get(object.plane_uid) || object.plane_uid;
    if (!object.beam_uid) return '观察面 '+plane;
    const beam=componentLabels(pathsOf(project || {})).get(object.beam_uid) || object.beam_uid;
    return '观察面 '+plane+' / '+beam;
  }
  if ('path_uid' in object) {
    const path = object.path_uid === 'primary' ? project?.system : project?.incident_paths?.find(p => p.uid === object.path_uid);
    const name = componentLabels(pathsOf(project || {})).get(object.path_uid) || object.path_uid;
    const element = componentLabels(path?.elements || []).get(object.element_uid);
    return name + ' / ' + (element || object.element_uid || '光源');
  }
  const component = componentLabels(project?.network?.components || []).get(object.component);
  return (component || object.component) + ' / 端口 ' + object.port;
}

export function appendDraft(message, text, current, incoming) {
  const followsCanvas = refs => !refs.project_revision_id && (refs.followCanvas || refs.element_uids?.length || refs.project_objects?.length);
  if ((current.startEmpty && (incoming.project_revision_id || followsCanvas(incoming))) ||
      (incoming.startEmpty && (current.project_revision_id || followsCanvas(current)))) {
    throw new Error('草稿已选择另一个工程起点。请先移除原工程引用或发送现有草稿，再选择新的起点。');
  }
  if ((followsCanvas(current) && incoming.project_revision_id) || (followsCanvas(incoming) && current.project_revision_id)) {
    throw new Error('当前引用跟随画布草稿，另一项引用指定了已存版本。请先发送当前问题，或移除其中一项工程引用。');
  }
  if (current.project_revision_id && incoming.project_revision_id && current.project_revision_id !== incoming.project_revision_id) {
    throw new Error('草稿已引用另一工程版本。请先发送草稿或移除原工程引用，再选择新版本；草稿已保留。');
  }
  const combined = message.trim() ? `${message}\n\n${text}` : text;
  if (combined.length > 24000) throw new Error('追加后超过24000字限制，请先整理草稿；原输入已保留。');
  const refs = { ...current, project_revision_id: incoming.project_revision_id || current.project_revision_id };
  if (incoming.followCanvas) refs.followCanvas = true;
  if (incoming.startEmpty) refs.startEmpty = true;
  refs.project_objects = distinctObjects([...(current.project_objects || []), ...(incoming.project_objects || [])]);
  for (const key of ['element_uids', 'inventory_ids', 'tool_call_ids', 'artifact_ids', 'attachment_ids']) {
    refs[key] = [...new Set([...(current[key] || []), ...(incoming[key] || [])])];
  }
  return { message: combined, refs };
}
