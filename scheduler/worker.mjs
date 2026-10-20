// Independent wakeups only. Research/mail credentials never leave GitHub Actions.
const REPO = 'r4ph426/miki-job-crawler';
const WORKFLOW = 'preparation.yml';
const API = `https://api.github.com/repos/${REPO}`;

export function deliveryTargets(now) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', hourCycle: 'h23',
  }).formatToParts(now).map(p => [p.type, p.value]));
  const day = `${parts.year}-${parts.month}-${parts.day}`;
  const calendar = new Date(`${day}T12:00:00Z`);
  const hour = Number(parts.hour);
  const weekday = calendar.getUTCDay() !== 0 && calendar.getUTCDay() !== 6;
  const result = [];
  if (weekday && hour >= 6) result.push(day);
  if (hour >= 9) {
    do { calendar.setUTCDate(calendar.getUTCDate() + 1); }
    while (calendar.getUTCDay() === 0 || calendar.getUTCDay() === 6);
    result.push(calendar.toISOString().slice(0, 10));
  }
  return result;
}

export function needsWake(feed, now) {
  const targets = deliveryTargets(now);
  if (!targets.length) return false;
  if (!feed || !Number.isFinite(Date.parse(feed.updated_at)) ||
      now.getTime() - Date.parse(feed.updated_at) > 60 * 60 * 1000) return true;
  return targets.some(day => {
    const record = feed.records?.[day];
    if (!record) return true;
    if (record.delivered_at) return false;
    if (record.provider_check_failed && record.provider_checked_at) {
      const due = Date.parse(record.scheduled_at);
      const future = due > now.getTime();
      let next = Date.parse(record.provider_checked_at) + (future ? 60 : 15) * 60 * 1000;
      if (future) next = Math.min(next, due);
      else if (Date.parse(record.provider_checked_at) < due) next = due;
      if (Number.isFinite(next) && now.getTime() < next) return false;
    }
    if (record.status === 'scheduled' && record.schedule_verified_at &&
        record.provider_status === 'queued' && Date.parse(record.scheduled_at) > now.getTime()) return false;
    return true;
  });
}

export async function wake(env, now = new Date(), request = fetch) {
  if (env.ENABLED !== 'true') return { status: 'disabled' };
  if (!deliveryTargets(now).length) return { status: 'outside_window' };
  if (!env.GITHUB_ACTIONS_TOKEN) throw new Error('scheduler_credential_missing');
  let feed;
  try {
    const response = await request(`https://raw.githubusercontent.com/${REPO}/main/state/status-feed.json`,
      { signal: AbortSignal.timeout(15000), redirect: 'manual', headers: { 'Cache-Control': 'no-cache' } });
    if (response.ok) feed = await response.json();
  } catch { /* A missing/unavailable feed must wake recovery, not silently suppress it. */ }
  if (!needsWake(feed, now)) return { status: 'healthy' };
  const headers = { Authorization: `Bearer ${env.GITHUB_ACTIONS_TOKEN}`, Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'miki-independent-scheduler' };
  // Never fill GitHub's shared pending slot while an earlier repair is active.
  const running = await request(`${API}/actions/workflows/${WORKFLOW}/runs?branch=main&per_page=10`,
    { headers, signal: AbortSignal.timeout(15000), redirect: 'manual' });
  if (!running.ok) throw new Error(`workflow_status_http_${running.status}`);
  const runs = (await running.json()).workflow_runs;
  if (!Array.isArray(runs)) throw new Error('workflow_status_invalid');
  if (runs.some(run => ['queued', 'in_progress', 'waiting', 'pending', 'requested'].includes(run.status))) {
    return { status: 'already_running' };
  }
  // Independent of feed freshness: a delayed/missing state write cannot create a
  // new failure notification on every tick. Infrastructure failures back off.
  const recent = runs.filter(run => Number.isFinite(Date.parse(run.created_at)))
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0];
  if (recent) {
    const delay = recent.conclusion === 'failure' ? 60 : 15;
    const since = recent.conclusion === 'failure' ? (recent.updated_at || recent.created_at) : recent.created_at;
    if (now.getTime() - Date.parse(since) < delay * 60 * 1000) {
      return { status: 'dispatch_cooldown' };
    }
  }
  const response = await request(`${API}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ ref: 'main', inputs: { task: 'recover' } }),
    signal: AbortSignal.timeout(15000), redirect: 'manual',
  });
  if (response.status !== 204) throw new Error(`workflow_dispatch_http_${response.status}`);
  return { status: 'dispatched', at: now.toISOString() };
}

export default {
  async scheduled(controller, env) {
    // Throw on failure so Cloudflare records a failed invocation. Never log token or response bodies.
    console.log(JSON.stringify(await wake(env)));
  },
  async fetch() { return new Response('Not found', { status: 404 }); },
};
