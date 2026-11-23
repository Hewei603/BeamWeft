import {useLayoutEffect, useRef} from 'react';
import {localId} from '../api/localId.js';

// SVG groups need explicit keyboard activation. Captured gestures deliver real
// taps on release; assistive technologies can still invoke their click action.
export function svgButton(activate, captured=false) {
  return {
    role:'button', tabIndex:0,
    onClick(event) {
      if (!captured || event.detail === 0) activate(event.currentTarget);
    },
    onKeyDown(event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        activate(event.currentTarget);
      }
    },
  };
}

// Each interaction owns its capture and callbacks until it commits or cancels.
export function usePointerGesture(disabled) {
  const active = useRef(null);
  useLayoutEffect(() => {
    if (disabled) active.current?.cancel();
    return () => active.current?.cancel();
  }, [disabled]);

  function begin(event, target, handlers, {readOnly=false}={}) {
    if ((disabled && !readOnly) || active.current || event.button !== 0 || event.isPrimary === false) return false;
    const id = event.pointerId;
    const doc = target.ownerDocument;
    const win = doc.defaultView;
    const controller = new AbortController();
    const options = {signal:controller.signal};
    function close(cancelled) {
      if (active.current !== session) return;
      active.current = null;
      controller.abort();
      if (target.hasPointerCapture(id)) target.releasePointerCapture(id);
      if (cancelled) handlers.cancel();
      else handlers.finish();
    }
    const cancel = () => close(true);
    const session = {cancel, finish:() => close(false)};
    target.setPointerCapture(id);
    active.current = session;
    // Chromium can start scrolling over SVG graphics even when touch-action is
    // set on their <g>. Reserve just the active touch, leaving the background scrollable.
    if (event.pointerType === 'touch' && target instanceof SVGElement) {
      target.addEventListener('touchstart', e => {
        if (e.cancelable && e.touches.length === 1) e.preventDefault();
      }, {...options, passive:false, once:true});
    }
    target.addEventListener('pointermove', e => {
      if (e.pointerId === id) handlers.move?.(e);
    }, options);
    target.addEventListener('pointerup', e => {
      if (e.pointerId !== id) return;
      handlers.move?.(e);
      close(false);
    }, options);
    target.addEventListener('pointercancel', e => { if (e.pointerId === id) cancel(); }, options);
    target.addEventListener('lostpointercapture', e => {
      if (e.target === target && e.pointerId === id) cancel();
    }, options);
    doc.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      cancel();
    }, {...options, capture:true});
    win.addEventListener('blur', cancel, options);
    doc.addEventListener('visibilitychange', () => { if (doc.hidden) cancel(); }, options);
    return true;
  }
  // A tap can drift slightly in screen coordinates. Open the editor on release;
  // start a single undoable movement only once the pointer leaves that radius.
  function beginDrag(event, target, handlers, canMove=true) {
    const movable = canMove && !disabled;
    if (!movable && !handlers.press) return false;
    const trigger = event.currentTarget;
    const startX = event.clientX, startY = event.clientY;
    const slop = event.pointerType === 'touch' ? 7 : 4;
    let moved = false;
    return begin(event, target, {
      move(e) {
        if (!moved) {
          if (Math.hypot(e.clientX-startX, e.clientY-startY) < slop) return;
          moved = true;
          if (movable) handlers.start?.();
        }
        if (movable) handlers.move(e);
      },
      finish() {
        if (!moved) handlers.press?.(trigger);
        else if (movable) handlers.finish();
        handlers.end?.();
      },
      cancel() {
        if (moved && movable) handlers.cancel();
        handlers.end?.();
      },
    }, {readOnly:!movable});
  }
  return {active, begin, beginDrag};
}

export function useAxialGesture(disabled, project, beginGesture) {
  const pointer = usePointerGesture(disabled);
  function beginAxis(event, svg, control, scale, apply, onEnd, onPress) {
    const inverse = control && scale > 0 && svg.getScreenCTM()?.inverse();
    const x = e => new DOMPoint(e.clientX,e.clientY).matrixTransform(inverse).x;
    const start = inverse ? x(event) : null;
    let last = control?.value;
    let transaction;
    let gapUid;
    const accepted = pointer.beginDrag(event, svg, {
      press: onPress,
      start() {
        if (control.openAt != null || control.local?.openAt != null) gapUid = localId();
        transaction = beginGesture(project, control.label || '移动元件');
      },
      move(e) {
        const value = Math.max(control.min, Math.min(control.max, control.value + (x(e)-start)*scale));
        if (value === last) return;
        last = value;
        transaction.update(value === control.value ? project : apply(project, control, value, gapUid));
      },
      finish() { transaction.finish(); },
      cancel() { transaction.cancel(); },
      end: onEnd,
    }, Boolean(inverse));
    if (accepted) {
      event.preventDefault();
      event.currentTarget.focus({preventScroll:true});
      if (svg.ownerDocument.activeElement !== event.currentTarget) svg.focus({preventScroll:true});
    }
    return accepted;
  }
  return {...pointer, beginAxis};
}
