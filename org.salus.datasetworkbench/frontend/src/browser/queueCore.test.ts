import assert from "node:assert/strict";
import { test } from "node:test";
import { createQueue } from "./queueCore.ts";
import type { Job } from "./queueCore.ts";

const file = (size: number) => ({ size }) as File;
const items = (n: number, size = 100) => Array.from({ length: n }, (_, i) => ({ file: file(size), path: `f${i}.bin` }));
const source = { id: "s1", name: "slides" };
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/** An upload that finishes when the test says so (or rejects when aborted). */
function controlled() {
  const pending = new Map<string, { finish: () => void; fail: (e: Error) => void }>();
  const upload = (job: Job, signal: AbortSignal, onProgress: (sent: number) => void) =>
    new Promise<void>((resolve, reject) => {
      pending.set(job.path, { finish: () => (onProgress(job.size), resolve()), fail: reject });
      signal.addEventListener("abort", () => reject(new Error("cancelled")));
    });
  return { upload, pending };
}

test("runs every file, at most four at a time, and ends with a finished snapshot", async () => {
  const c = controlled();
  let batches = 0;
  const q = createQueue({ upload: c.upload, onBatchDone: () => batches++, schedule: (f) => f() });
  q.enqueue(source, items(6), false);
  await tick();
  assert.equal(q.getSnapshot().counts.uploading, 4);
  assert.equal(q.getSnapshot().counts.waiting, 2);
  assert.equal(q.getSnapshot().active, true);
  for (const name of ["f0.bin", "f1.bin", "f2.bin", "f3.bin"]) c.pending.get(name)!.finish();
  await tick();
  assert.equal(q.getSnapshot().counts.done, 4);
  assert.equal(q.getSnapshot().counts.uploading, 2);
  for (const name of ["f4.bin", "f5.bin"]) c.pending.get(name)!.finish();
  await tick();
  const s = q.getSnapshot();
  assert.deepEqual([s.counts.done, s.active, s.finishedAt !== null, batches], [6, false, true, 1]);
  assert.equal(s.sentBytes, 600);
});

test("a failing file is marked failed and the others carry on", async () => {
  const c = controlled();
  const q = createQueue({ upload: c.upload, schedule: (f) => f() });
  q.enqueue(source, items(2), false);
  await tick();
  c.pending.get("f0.bin")!.fail(new Error("disk full"));
  c.pending.get("f1.bin")!.finish();
  await tick();
  const jobs = q.getSnapshot().jobs;
  assert.deepEqual(jobs.map((j) => j.status), ["error", "done"]);
  assert.equal(jobs[0].error, "disk full");
});

test("cancelling stops running and waiting files, and files queued afterwards still run", async () => {
  const c = controlled();
  const q = createQueue({ upload: c.upload, schedule: (f) => f() });
  q.enqueue(source, items(6), false);
  await tick();
  q.cancelAll();
  // New files arrive before the aborted transfers have wound down; they must not inherit the cancellation.
  q.enqueue(source, [{ file: file(50), path: "later.bin" }], false);
  await tick();
  await tick();
  assert.equal(c.pending.has("later.bin"), true, "the new file started");
  c.pending.get("later.bin")!.finish();
  await tick();
  const s = q.getSnapshot();
  assert.equal(s.counts.cancelled, 6);
  assert.equal(s.counts.done, 1);
  assert.equal(s.active, false);
});

test("cancelling while only waiting ends the batch", async () => {
  const c = controlled();
  const q = createQueue({ upload: c.upload, schedule: (f) => f(), concurrency: 1 });
  q.enqueue(source, items(3), false);
  await tick();
  q.cancelAll(); // one is running (and is aborted), two are waiting
  await tick();
  const s = q.getSnapshot();
  assert.equal(s.active, false);
  assert.equal(s.counts.cancelled, 3);
  assert.notEqual(s.finishedAt, null);
});

test("a new batch after a finished one starts a fresh list; during a batch it adds to it", async () => {
  const c = controlled();
  const q = createQueue({ upload: c.upload, schedule: (f) => f() });
  q.enqueue(source, items(1), false);
  await tick();
  q.enqueue(source, [{ file: file(1), path: "extra.bin" }], false);
  assert.equal(q.getSnapshot().jobs.length, 2);
  await tick();
  c.pending.get("f0.bin")!.finish();
  c.pending.get("extra.bin")!.finish();
  await tick();
  assert.equal(q.getSnapshot().active, false);
  q.enqueue(source, [{ file: file(1), path: "fresh.bin" }], true);
  assert.deepEqual(q.getSnapshot().jobs.map((j) => j.path), ["fresh.bin"]);
  q.dismiss(); // ignored while active
  assert.equal(q.getSnapshot().jobs.length, 1);
});

test("the time left is estimated from the recent speed", async () => {
  let t = 0;
  let progress!: (sent: number) => void;
  const q = createQueue({
    upload: (_job, _signal, onProgress) => {
      progress = onProgress;
      return new Promise<void>(() => undefined);
    },
    now: () => t,
    schedule: (f) => f(),
    concurrency: 1,
  });
  q.enqueue(source, [{ file: file(1000), path: "big.bin" }], false);
  await tick();
  assert.equal(q.getSnapshot().etaSeconds, null, "nothing to base an estimate on yet");
  t = 4000;
  progress(250); // 250 bytes in 4 seconds = 62.5 bytes/s, 750 bytes left
  assert.equal(Math.round(q.getSnapshot().etaSeconds!), 12);
});

test("ignores empty batches and sources without an id", () => {
  const q = createQueue({ upload: async () => undefined, schedule: (f) => f() });
  q.enqueue(source, [], false);
  q.enqueue({ id: null, name: "x" }, items(1), false);
  assert.equal(q.getSnapshot().jobs.length, 0);
});
