export type DataRow = Record<string, unknown>;
function object(value: unknown): DataRow { return value && typeof value === 'object' && !Array.isArray(value) ? value as DataRow : {}; }

/** A video can only enter manual approval after successful QA, once. */
export function canApprove(video: DataRow): boolean {
  return ['AWAITING_APPROVAL', 'DRAFT', 'READY', 'PENDING_APPROVAL'].includes(String(video.status))
    && object(video.qa).passed === true && object(video.metadata_json).approved !== true;
}

/** Unknown publication outcomes require reconciliation, never a blind retry. */
export function canRetry(job: DataRow): boolean {
  return ['FAILED', 'DLQ', 'BLOCKED'].includes(String(job.status))
    && !(job.status === 'BLOCKED' && String(job.kind).toUpperCase() === 'PUBLISH');
}

/** Provider counters are cumulative: count only the newest snapshot of each post. */
export function latestPostMetrics(rows: DataRow[]): DataRow[] {
  const latest = new Map<string, DataRow>();
  for (const [index, row] of rows.entries()) {
    const key = String(row.post_id || row.id || `unidentified:${index}`);
    const current = latest.get(key);
    const time = Date.parse(String(row.bucket_at || row.created_at || ''));
    const currentTime = current ? Date.parse(String(current.bucket_at || current.created_at || '')) : -Infinity;
    if (!current || (Number.isFinite(time) && (!Number.isFinite(currentTime) || time > currentTime))) latest.set(key, row);
  }
  return [...latest.values()].map(row => object(row.metrics));
}
