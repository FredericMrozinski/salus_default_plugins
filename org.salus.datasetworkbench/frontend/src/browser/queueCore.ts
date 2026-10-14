// The upload queue without React or the network: the work of uploading one file is passed in. Kept separate so it can be tested.

export type JobStatus = "waiting" | "uploading" | "done" | "error" | "cancelled";

export type Job = {
  id: number;
  sourceId: string;
  sourceName: string;
  path: string; // inside the source
  file: File;
  size: number;
  overwrite: boolean;
  status: JobStatus;
  sent: number;
  error?: string;
};

export type Snapshot = {
  jobs: Job[];
  active: boolean;
  totalBytes: number;
  sentBytes: number;
  counts: Record<JobStatus, number>;
  etaSeconds: number | null; // null while it cannot be estimated yet
  finishedAt: number | null;
};

export type QueueDeps = {
  /** Uploads one file; reports bytes sent; must reject when `signal` is aborted. */
  upload: (job: Job, signal: AbortSignal, onProgress: (sent: number) => void) => Promise<void>;
  onBatchDone?: () => void;
  concurrency?: number;
  now?: () => number;
  /** Runs `flush` soon (views are refreshed at most that often). Default: after 150 ms. */
  schedule?: (flush: () => void) => void;
  messageOf?: (error: unknown) => string;
};

const SPEED_WINDOW_MS = 12_000;

export function createQueue(deps: QueueDeps) {
  const concurrency = deps.concurrency ?? 4;
  const now = deps.now ?? Date.now;
  const schedule = deps.schedule ?? ((flush: () => void) => void setTimeout(flush, 150));
  const messageOf = deps.messageOf ?? ((e: unknown) => (e instanceof Error ? e.message : String(e)));

  let jobs: Job[] = [];
  let nextId = 1;
  let running = 0;
  let finishedAt: number | null = null;
  let samples: { t: number; sent: number }[] = [];
  const controllers = new Map<number, AbortController>();
  const listeners = new Set<() => void>();
  let scheduled = false;
  let snapshot = compute();

  function compute(): Snapshot {
    const counts: Record<JobStatus, number> = { waiting: 0, uploading: 0, done: 0, error: 0, cancelled: 0 };
    let totalBytes = 0;
    let sentBytes = 0;
    for (const job of jobs) {
      counts[job.status]++;
      totalBytes += job.size;
      sentBytes += job.status === "done" ? job.size : job.sent;
    }
    const active = counts.waiting + counts.uploading > 0;
    let etaSeconds: number | null = null;
    if (active && samples.length > 1) {
      const first = samples[0];
      const last = samples[samples.length - 1];
      const seconds = (last.t - first.t) / 1000;
      const rate = seconds >= 2 ? (last.sent - first.sent) / seconds : 0;
      if (rate > 0) etaSeconds = (totalBytes - sentBytes) / rate;
    }
    return { jobs, active, totalBytes, sentBytes, counts, etaSeconds, finishedAt };
  }

  function flush() {
    scheduled = false;
    snapshot = compute();
    listeners.forEach((l) => l());
  }

  /** Progress arrives for every chunk; views are refreshed only now and then. */
  function changed(immediately = false) {
    if (immediately) return flush();
    if (!scheduled) {
      scheduled = true;
      schedule(flush);
    }
  }

  function sample() {
    const t = now();
    samples.push({ t, sent: jobs.reduce((n, j) => n + (j.status === "done" ? j.size : j.sent), 0) });
    while (samples.length > 2 && t - samples[0].t > SPEED_WINDOW_MS) samples.shift();
  }

  async function worker() {
    for (;;) {
      const job = jobs.find((j) => j.status === "waiting");
      if (!job) return;
      const controller = new AbortController();
      controllers.set(job.id, controller);
      job.status = "uploading";
      changed();
      try {
        await deps.upload(job, controller.signal, (sent) => ((job.sent = sent), sample(), changed()));
        job.status = "done";
        job.sent = job.size;
      } catch (error) {
        // Cancelling a file is a decision about that file only; it never affects files queued later.
        job.status = controller.signal.aborted ? "cancelled" : "error";
        job.error = controller.signal.aborted ? undefined : messageOf(error);
      } finally {
        controllers.delete(job.id);
      }
      sample();
      changed();
    }
  }

  function startWorkers() {
    while (running < concurrency && jobs.some((j) => j.status === "waiting")) {
      running++;
      void worker().finally(() => {
        running--;
        if (running === 0 && !jobs.some((j) => j.status === "waiting")) finish();
      });
    }
  }

  function finish() {
    finishedAt = now();
    changed(true);
    deps.onBatchDone?.();
  }

  return {
    /** Puts files into the queue (all into the same source; `overwrite` was decided for the whole batch). */
    enqueue(source: { id: string | null; name: string }, items: { file: File; path: string }[], overwrite: boolean): void {
      if (!source.id || items.length === 0) return;
      if (finishedAt !== null) {
        // The previous batch is over: start a fresh list.
        jobs = [];
        samples = [];
        finishedAt = null;
      }
      for (const { file, path } of items) {
        jobs.push({ id: nextId++, sourceId: source.id, sourceName: source.name, path, file, size: file.size, overwrite, status: "waiting", sent: 0 });
      }
      if (samples.length === 0) samples.push({ t: now(), sent: 0 });
      changed(true);
      startWorkers();
    },

    /** Stops what is running (partial files are deleted by the upload) and drops what is waiting. */
    cancelAll(): void {
      for (const job of jobs) if (job.status === "waiting") job.status = "cancelled";
      for (const controller of controllers.values()) controller.abort();
      changed(true);
      if (running === 0 && finishedAt === null && jobs.length > 0) finish();
    },

    /** Hides a finished batch. */
    dismiss(): void {
      if (snapshot.active) return;
      jobs = [];
      samples = [];
      finishedAt = null;
      changed(true);
    },

    getSnapshot: () => snapshot,
    subscribe: (listener: () => void) => (listeners.add(listener), () => void listeners.delete(listener)),
  };
}
