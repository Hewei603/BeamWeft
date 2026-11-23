import React from 'react';

export default function SourceConditions({source,target}) {
  const conditions=Object.entries(source?.condition_values || {}).flatMap(([key,value])=>{
    const label=target.conditions?.[key]?.label || source.conditions?.[key]?.label;
    return label?[label+' · '+value]:[];
  });
  return conditions.length ? <small>来源工况 · {conditions.join('，')}</small>:null;
}
