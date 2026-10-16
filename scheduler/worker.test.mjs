import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { deliveryTargets, needsWake, wake } from './worker.mjs';
const now = new Date('2026-10-19T06:40:00Z');
const env = { ENABLED: 'true', GITHUB_ACTIONS_TOKEN: 'test-only-secret' };

test('Berlin weekdays, weekend preparation and DST are independent of UTC offset', () => {
  assert.deepEqual(deliveryTargets(new Date('2026-10-19T03:59:00Z')), []);
  assert.deepEqual(deliveryTargets(new Date('2026-10-26T05:00:00Z')), ['2026-10-26']);
  assert.deepEqual(deliveryTargets(new Date('2026-10-16T07:00:00Z')), ['2026-10-16', '2026-10-19']);
  assert.deepEqual(deliveryTargets(new Date('2026-10-18T20:00:00Z')), ['2026-10-19']);
});
test('healthy future queue skips; missing, stale and accepted-only records wake recovery', () => {
  const feed = { updated_at: now.toISOString(), records: { '2026-10-19': {
    status: 'scheduled', scheduled_at: '2026-10-19T06:30:00Z', schedule_verified_at: now.toISOString(), provider_status: 'queued',
  } } };
  assert.equal(needsWake(feed, now), true);
  assert.equal(needsWake(feed, new Date('2026-10-19T06:00:00Z')), false);
  feed.records['2026-10-19'] = { status: 'sent', accepted_at: now.toISOString() };
  assert.equal(needsWake(feed, now), true);
  feed.records['2026-10-19'].delivered_at = now.toISOString();
  assert.equal(needsWake(feed, now), false);
  feed.updated_at = '2026-10-18T00:00:00Z';
  assert.equal(needsWake(feed, now), true);
});
test('only fixed recovery workflow receives token; no mail content or credentials are sent', async () => {
  const calls = [];
  const request = async (url, options) => {
    calls.push([url, options]);
    if (calls.length === 1) return new Response('{}');
    if (calls.length === 2) return Response.json({ workflow_runs: [] });
    return new Response(null, { status: 204 });
  };
  assert.equal((await wake(env, now, request)).status, 'dispatched');
  assert.equal(calls[0][1].headers.Authorization, undefined);
  assert.equal(calls[2][0], 'https://api.github.com/repos/r4ph426/miki-job-crawler/actions/workflows/preparation.yml/dispatches');
  assert.deepEqual(JSON.parse(calls[2][1].body), { ref: 'main', inputs: { task: 'recover' } });
  // Workers supports manual/follow only. Manual still prevents forwarding credentials on redirects.
  assert.equal(calls.every(([, options]) => options.redirect === 'manual'), true);
});
test('active run suppresses repeated dispatch', async () => {
  let count = 0;
  const request = async () => ++count === 1 ? Response.json({}) : Response.json({ workflow_runs: [{ status: 'queued' }] });
  assert.equal((await wake(env, now, request)).status, 'already_running');
  assert.equal(count, 2);
});
test('unavailable status feed wakes repair; rejected dispatch fails visibly without leaking body', async () => {
  let count = 0;
  const request = async () => {
    count++;
    if (count === 1) throw new Error('temporary');
    if (count === 2) return Response.json({ workflow_runs: [] });
    return new Response('private-provider-body', { status: 403 });
  };
  await assert.rejects(wake(env, now, request), { message: 'workflow_dispatch_http_403' });
});
test('disabled worker and public HTTP requests cannot dispatch', async () => {
  const request = () => { throw new Error('must not call'); };
  assert.equal((await wake({ ...env, ENABLED: 'false' }, now, request)).status, 'disabled');
  assert.equal((await worker.fetch()).status, 404);
});
