// Run with: node --test .github/workflows/scripts/check_calling_workflow.test.cjs
const assert = require('node:assert/strict');
const { test } = require('node:test');
const check = require('./check_calling_workflow.cjs');

const upstream = { id: 1, full_name: 'org/quadrants', owner: { login: 'org' }, default_branch: 'main' };
const fork = { id: 2, full_name: 'alice/quadrants', owner: { login: 'alice' } };
function pr(number, branch, base, options = {}) {
  return {
    number, state: 'open',
    head: { ref: branch, sha: `${branch}-sha`, repo: upstream },
    base: { ref: base, sha: `${base}-sha`, repo: upstream },
    ...options,
  };
}
function fixture(top, others = []) {
  const outputs = {};
  const calls = [];
  const notices = [];
  const pulls = [top, ...others];
  const context = {
    repo: { owner: 'org', repo: 'quadrants' }, eventName: 'workflow_run',
    payload: {
      repository: upstream,
      workflow_run: {
        event: 'pull_request', head_branch: top.head.ref,
        head_sha: top.head.sha, head_repository: top.head.repo,
      },
    },
  };
  const github = {
    rest: { pulls: { list: Symbol('pulls.list') } },
    async paginate(method, params) {
      assert.equal(method, github.rest.pulls.list);
      assert.equal(params.owner, 'org');
      assert.equal(params.repo, 'quadrants');
      calls.push(params);
      return pulls.filter((pull) => `${pull.head.repo?.owner.login}:${pull.head.ref}` === params.head
        && (params.state === 'all' || pull.state === params.state));
    },
  };
  const core = {
    setOutput: (key, value) => { outputs[key] = value; },
    info: () => {}, notice: (message) => notices.push(message),
  };
  return { github, context, core, outputs, calls, notices };
}
async function rejected(f, message) {
  await assert.rejects(check(f), message);
  assert.equal(f.outputs.is_open, undefined, 'Validation errors must not become successful skips.');
}

test('internal and fork PRs targeting main remain eligible', async () => {
  for (const repo of [upstream, fork]) {
    const top = pr(1, 'topic', 'main');
    top.head.repo = repo;
    const f = fixture(top);
    await check(f);
    assert.equal(f.outputs.is_open, true);
    assert.equal(f.calls.length, 1);
  }
});

test('valid three-level internal stack reaches main', async () => {
  const f = fixture(pr(3, 'top', 'middle'), [pr(2, 'middle', 'bottom'), pr(1, 'bottom', 'main')]);
  await check(f);
  assert.equal(f.outputs.is_open, true);
  assert.deepEqual(f.calls.map((call) => call.head), ['org:top', 'org:middle', 'org:bottom']);
});

test('fork at the top is rejected even when its parent is upstream', async () => {
  const top = pr(2, 'top', 'bottom');
  top.head.repo = fork;
  await rejected(fixture(top, [pr(1, 'bottom', 'main')]), /PR #2 originates from alice\/quadrants/);
});

test('same-owner fork in a parent is rejected by repository identity', async () => {
  const parent = pr(1, 'bottom', 'main');
  parent.head.repo = { ...fork, owner: upstream.owner, full_name: 'org/quadrants-fork' };
  await rejected(fixture(pr(2, 'top', 'bottom'), [parent]), /PR #1 originates from org\/quadrants-fork/);
});

test('unresolved or closed parents fail', async () => {
  for (const others of [[], [pr(1, 'bottom', 'main', { state: 'closed' })]]) {
    await rejected(fixture(pr(2, 'top', 'bottom'), others), /no open parent PR found.*bottom/);
  }
});

test('ambiguous parents fail', async () => {
  const f = fixture(pr(3, 'top', 'bottom'), [pr(1, 'bottom', 'main'), pr(2, 'bottom', 'other')]);
  await rejected(f, /multiple open parent PRs found.*bottom/);
});

test('cycles fail', async () => {
  await rejected(fixture(pr(2, 'top', 'bottom'), [pr(1, 'bottom', 'top')]), /cycle detected at PR #2/);
});

test('a parent changing commits during validation fails', async () => {
  const parent = pr(1, 'bottom', 'main');
  parent.head.sha = 'new-sha';
  await rejected(fixture(pr(2, 'top', 'bottom'), [parent]), /base branch 'bottom' changed/);
});

test('a parent targeting another repository fails', async () => {
  const parent = pr(1, 'bottom', 'main');
  parent.base.repo = fork;
  await rejected(fixture(pr(2, 'top', 'bottom'), [parent]), /PR #1 targets a repository other than org\/quadrants/);
});

test('API failures include branch context and fail', async () => {
  const f = fixture(pr(2, 'top', 'bottom'));
  const paginate = f.github.paginate;
  f.github.paginate = async (method, params) => {
    if (params.head === 'org:bottom') throw new Error('API rate limit exceeded');
    return paginate(method, params);
  };
  await rejected(f, /GitHub API lookup for 'org:bottom' failed: API rate limit exceeded/);
});

test('closed and merged top PRs retain intentional skipping', async () => {
  for (const merged_at of [null, '2026-11-05T00:00:00Z']) {
    const f = fixture(pr(1, 'topic', 'main', { state: 'closed', merged_at }));
    await check(f);
    assert.equal(f.outputs.is_open, false);
    assert.equal(f.notices.length, 1);
  }
});

test('an open match takes precedence over a closed match', async () => {
  const f = fixture(pr(1, 'topic', 'main', { state: 'closed' }), [pr(2, 'topic', 'main')]);
  await check(f);
  assert.equal(f.outputs.is_open, true);
});

test('ambiguous triggering PRs fail', async () => {
  await rejected(fixture(pr(1, 'topic', 'main'), [pr(2, 'topic', 'other')]), /multiple open PRs head commit/);
});

test('stale commits and non-PR events fail', async () => {
  const stale = fixture(pr(1, 'topic', 'main'));
  stale.context.payload.workflow_run.head_sha = 'old-sha';
  await rejected(stale, /Commit old-sha heads no pull request/);
  const push = fixture(pr(1, 'topic', 'main'));
  push.context.payload.workflow_run.event = 'push';
  await rejected(push, /Started by 'push'/);
});
