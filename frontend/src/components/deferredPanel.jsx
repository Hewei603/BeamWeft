import React, { useEffect, useState } from 'react';

// Keep the editor and its draft mounted while another panel downloads or retries.
export function deferredPanel(load, label, exportName = 'default') {
  let Component, request;
  function fetchComponent() {
    request ??= load().then(module => {
      Component = module[exportName];
    }).catch(error => {
      request = null;
      throw error;
    });
    return request;
  }
  return function DeferredPanel(props) {
    const [attempt, setAttempt] = useState(0);
    const [error, setError] = useState(null);
    const [Loaded, setLoaded] = useState(() => Component);
    useEffect(() => {
      if (Loaded) return;
      let active = true;
      fetchComponent().then(() => { if (active) setLoaded(() => Component); },
        cause => { if (active) setError(cause); });
      return () => { active = false; };
    }, [attempt, Loaded]);
    if (Loaded) return <Loaded {...props}/>;
    if (error) return <div className="error" role="alert">{label}加载失败。{error.message}
      {' '}<button type="button" className="btn btn-secondary"
        onClick={() => {setError(null); setAttempt(n => n + 1);}}>重新加载{label}</button></div>;
    return <p role="status" className="field-note">正在加载{label}…</p>;
  };
}
