import { useEffect, useState } from "react";
import { bytes } from "../shared/format";
import { cancelAll, dismiss, Job, JobStatus, useUploads } from "./uploadQueue";

function duration(seconds: number): string {
  if (seconds < 45) return "less than a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} min`;
  return `about ${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

const SECTIONS: { status: JobStatus; title: string }[] = [
  { status: "uploading", title: "Uploading" },
  { status: "waiting", title: "Waiting" },
  { status: "error", title: "Failed" },
  { status: "done", title: "Uploaded" },
  { status: "cancelled", title: "Cancelled" },
];
const MAX_ROWS = 100;

/**
 * The small bar at the very bottom: overall progress and the time left. A click on it lists which files are uploaded and
 * which are still to come. It goes away a few seconds after a successful batch; failures stay until dismissed.
 */
export function UploadProgress() {
  const up = useUploads();
  const [open, setOpen] = useState(false);
  const finished = !up.active && up.jobs.length > 0;
  const clean = finished && up.counts.error === 0;

  useEffect(() => {
    if (!clean || open) return;
    const t = setTimeout(dismiss, 5000);
    return () => clearTimeout(t);
  }, [clean, open, up.finishedAt]);
  useEffect(() => {
    if (up.jobs.length === 0) setOpen(false);
  }, [up.jobs.length]);

  if (up.jobs.length === 0) return null;
  const total = up.jobs.length;
  const finishedCount = up.counts.done;
  const fraction = up.totalBytes > 0 ? up.sentBytes / up.totalBytes : 1;

  let text: string;
  if (up.active) {
    const eta = up.etaSeconds === null ? "estimating time…" : `${duration(up.etaSeconds)} left`;
    text = `Uploading ${finishedCount + up.counts.uploading} of ${total} · ${bytes(up.sentBytes)} of ${bytes(up.totalBytes)} · ${eta}`;
  } else if (up.counts.error > 0) {
    text = `${up.counts.error} of ${total} failed · ${finishedCount} uploaded`;
  } else if (up.counts.cancelled > 0) {
    text = `Upload cancelled · ${finishedCount} of ${total} uploaded`;
  } else {
    text = `Uploaded ${total} file${total === 1 ? "" : "s"} (${bytes(up.totalBytes)})`;
  }

  return (
    <div className={`upload-progress ${finished ? (up.counts.error > 0 ? "has-errors" : "finished") : ""}`}>
      {open && (
        <div className="upload-progress-list">
          {SECTIONS.map(({ status, title }) => {
            const rows = up.jobs.filter((j) => j.status === status);
            if (rows.length === 0) return null;
            return (
              <div key={status} className="upload-section">
                <div className="upload-section-title">
                  {title} <span className="faint">({rows.length})</span>
                </div>
                {rows.slice(0, MAX_ROWS).map((job) => (
                  <Row key={job.id} job={job} />
                ))}
                {rows.length > MAX_ROWS && <div className="faint">and {rows.length - MAX_ROWS} more</div>}
              </div>
            );
          })}
        </div>
      )}
      <div className="upload-progress-bar" onClick={() => setOpen(!open)} role="button" tabIndex={0} title="Show the files" onKeyDown={(e) => e.key === "Enter" && setOpen(!open)}>
        <span className="upload-progress-text">{text}</span>
        <progress value={fraction} max={1} />
        {up.active ? (
          <button className="small" onClick={(e) => (e.stopPropagation(), cancelAll())} title="Stop uploading; unfinished files are not kept">
            Cancel
          </button>
        ) : (
          <button className="small" onClick={(e) => (e.stopPropagation(), dismiss())}>
            Dismiss
          </button>
        )}
      </div>
    </div>
  );
}

function Row({ job }: { job: Job }) {
  const percent = job.size > 0 ? Math.round((100 * job.sent) / job.size) : 100;
  return (
    <div className="upload-file">
      <span className="upload-file-name" title={`${job.sourceName} / ${job.path}`}>
        <span className="faint">{job.sourceName} / </span>
        {job.path}
      </span>
      <span className="faint upload-file-size">{job.status === "uploading" ? `${percent}% of ${bytes(job.size)}` : bytes(job.size)}</span>
      {job.error && <span className="error-line upload-file-error">{job.error}</span>}
    </div>
  );
}
