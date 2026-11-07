import test from 'node:test';
import assert from 'node:assert/strict';
import { canApprove, canRetry, latestPostMetrics } from '../lib/domain.ts';

test('Manual approval requires QA and does not offer a second approval', () => {
  assert.equal(canApprove({status: 'READY', qa: {passed: true}, metadata_json: {}}), true);
  assert.equal(canApprove({status: 'READY', qa: {passed: false}}), false);
  assert.equal(canApprove({status: 'READY'}), false);
  assert.equal(canApprove({status: 'READY', qa: {passed: true}, metadata_json: {approved: true}}), false);
  assert.equal(canApprove({status: 'PUBLISHED', qa: {passed: true}}), false);
});

test('An uncertain or active publish cannot expose a blind retry', () => {
  assert.equal(canRetry({status: 'UNKNOWN', kind: 'publish'}), false);
  assert.equal(canRetry({status: 'BLOCKED', kind: 'publish'}), false);
  assert.equal(canRetry({status: 'RUNNING', kind: 'render'}), false);
  assert.equal(canRetry({status: 'DLQ', kind: 'render'}), true);
  assert.equal(canRetry({status: 'BLOCKED', kind: 'drive_sync'}), true);
});

test('Cumulative analytics snapshots are counted once per post, across timezone offsets', () => {
  const result = latestPostMetrics([
    {post_id: 'a', bucket_at: '2026-09-14T14:00:00+03:00', metrics: {views: 100}},
    {post_id: 'a', bucket_at: '2026-09-14T12:00:00Z', metrics: {views: 150}},
    {post_id: 'b', bucket_at: '2026-09-14T12:00:00Z', metrics: {views: 30}},
  ]);
  assert.equal(result.length, 2);
  assert.equal(result.reduce((total, metrics) => total + metrics.views, 0), 180);
});

test('Unavailable analytics values remain unavailable', () => {
  assert.deepEqual(latestPostMetrics([]), []);
  assert.deepEqual(latestPostMetrics([{id:'a', metrics:null}]), [{}]);
});
