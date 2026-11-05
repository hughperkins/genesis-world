// Validate the calling repository's PR before any code from its wheel runs on the cluster.
module.exports = async function checkCallingWorkflow({ github, context, core }) {
  const run = context.payload.workflow_run;
  if (run?.event !== 'pull_request') {
    throw new Error(`Started by '${run?.event || context.eventName}', where a pull request alone is served.`);
  }
  const repository = context.payload.repository;
  const defaultBranch = repository.default_branch;
  const params = { owner: context.repo.owner, repo: context.repo.repo };
  const repositoryName = `${params.owner}/${params.repo}`;

  async function listPulls(head, state) {
    try {
      return await github.paginate(github.rest.pulls.list, { ...params, head, state });
    } catch (error) {
      throw new Error(`Stack validation failed: GitHub API lookup for '${head}' failed: ${error.message}`);
    }
  }

  const pulls = await listPulls(`${run.head_repository.owner.login}:${run.head_branch}`, 'all');
  const matching = pulls.filter((pull) => pull.head.sha === run.head_sha
    && pull.head.ref === run.head_branch && pull.head.repo?.id === run.head_repository.id);
  const open = matching.filter((pull) => pull.state === 'open');
  if (open.length > 1) {
    throw new Error(`Stack validation failed: multiple open PRs head commit ${run.head_sha}: `
      + `${open.map((pull) => `#${pull.number}`).join(', ')}.`);
  }
  const pull = open[0] || matching[0];
  if (!pull) {
    throw new Error(`Commit ${run.head_sha} heads no pull request in ${repositoryName}.`);
  }
  if (pull.state !== 'open') {
    const status = pull.merged_at ? 'merged' : 'closed without merging';
    core.notice(`Pull request #${pull.number} is ${status}, so its commit ${run.head_sha} is left untested.`);
    core.setOutput('is_open', false);
    return;
  }

  // Fork PRs targeting the default branch remain eligible under the existing workflow approval policy.
  if (pull.base.ref !== defaultBranch) {
    const visited = new Set();
    let current = pull;
    while (true) {
      if (visited.has(current.number)) {
        throw new Error(`Stack validation failed: cycle detected at PR #${current.number}.`);
      }
      visited.add(current.number);
      if (current.head.repo?.id !== repository.id) {
        throw new Error(`Stack rejected: PR #${current.number} originates from `
          + `${current.head.repo?.full_name || 'a missing repository'}; expected ${repositoryName}.`);
      }
      if (current.base.repo?.id !== repository.id) {
        throw new Error(`Stack rejected: PR #${current.number} targets a repository other than ${repositoryName}.`);
      }
      core.info(`Stack PR #${current.number}: ${current.head.ref} -> ${current.base.ref}`);
      if (current.base.ref === defaultBranch) {
        break;
      }
      const parents = (await listPulls(`${params.owner}:${current.base.ref}`, 'open'))
        .filter((parent) => parent.head.ref === current.base.ref && parent.state === 'open');
      if (parents.length !== 1) {
        const reason = parents.length === 0 ? 'no open parent PR found' : 'multiple open parent PRs found';
        throw new Error(`Stack validation failed: ${reason} for base branch '${current.base.ref}' `
          + `of PR #${current.number}; cannot determine the path to '${defaultBranch}'.`);
      }
      const parent = parents[0];
      if (parent.head.sha !== current.base.sha) {
        throw new Error(`Stack validation failed: base branch '${current.base.ref}' changed while checking `
          + `PR #${current.number}; parent PR #${parent.number} has a different commit. Retry the workflow.`);
      }
      current = parent;
    }
  }
  core.setOutput('is_open', true);
};
