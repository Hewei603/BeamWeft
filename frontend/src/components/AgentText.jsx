import React, {useEffect, useRef, useState} from 'react';

let Format, request;
function loadFormatter() {
  request ??= import('./FormattedAgentText').then(module => Format = module.default).catch(error => {
    request = null;
    throw error;
  });
  return request;
}

export default function AgentText({children}) {
  const placeholder = useRef(null);
  const [Formatter, setFormatter] = useState(() => Format);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (Formatter) return;
    let active = true, observer;
    const format = () => {
      observer?.disconnect();
      loadFormatter().then(component => { if (active) setFormatter(() => component); },
        () => { if (active) setFailed(true); });
    };
    if (typeof IntersectionObserver === 'undefined' || attempt || Format) format();
    else {
      observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) format(); });
      observer.observe(placeholder.current);
    }
    return () => { active = false; observer?.disconnect(); };
  }, [Formatter, attempt]);
  if (Formatter) return <Formatter>{children}</Formatter>;
  return <div className="wb-markdown" ref={placeholder}>
    <p style={{whiteSpace:'pre-wrap'}}>{children}</p>
    {failed && <button type="button" onClick={() => {setFailed(false); setAttempt(value => value + 1);}}>重新加载文本排版</button>}
  </div>;
}
