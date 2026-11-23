import {useLayoutEffect, useRef, useSyncExternalStore} from 'react';

// Each mounted editor supplies the actions it can perform in this window.
const commands = new Map();
const listeners = new Set();
function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
function notify() {
  for (const listener of listeners) listener();
}
export function useMenuAction(name, enabled, callback) {
  const latest = useRef(callback);
  useLayoutEffect(() => { latest.current = callback; });
  useLayoutEffect(() => {
    if (!enabled) return;
    const run = () => latest.current();
    commands.set(name, run);
    notify();
    return () => {
      if (commands.get(name) === run) {
        commands.delete(name);
        notify();
      }
    };
  }, [name, enabled]);
}
export function useCurrentAction(name) {
  return useSyncExternalStore(subscribe, () => commands.get(name));
}
