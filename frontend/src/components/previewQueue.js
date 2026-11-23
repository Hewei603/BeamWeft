const EDIT_INTERVAL_MS = 75;
// Wait for a settled edit before replacing a slow call. Some solvers finish on the
// server even when their HTTP request is cancelled.
const SETTLED_EDIT_MS = 1000;

export function createPreviewQueue(compute, publish) {
  let next = null;
  let running = null;
  let timer = null;
  let lastStart = -Infinity;

  function schedule() {
    if (!next || timer !== null) return;
    const delay = running ? SETTLED_EDIT_MS : Math.max(0, lastStart + EDIT_INTERVAL_MS - performance.now());
    timer = setTimeout(start, delay);
  }

  async function start() {
    timer = null;
    running?.controller.abort();
    const job = next;
    const call = { job, controller: new AbortController() };
    next = null;
    running = call;
    lastStart = performance.now();
    try {
      const data = await compute(job.system, call.controller.signal);
      if (running === call) publish({ ...job, data });
    } catch (cause) {
      if (running === call && !next) {
        const error = cause?.message || (typeof cause === 'string' && cause) || '预览求解未返回错误说明。';
        publish({ ...job, error });
      }
    } finally {
      if (running === call) {
        running = null;
        clearTimeout(timer);
        timer = null;
        schedule();
      }
    }
  }

  return {
    submit(job) {
      if (running) {
        clearTimeout(timer);
        timer = null;
      }
      if (running && running.job.system === job.system && running.job.attempt === job.attempt) {
        next = null;
        return;
      }
      next = job;
      schedule();
    },
    cancel() {
      next = null;
      clearTimeout(timer);
      timer = null;
      running?.controller.abort();
      running = null;
    },
  };
}
