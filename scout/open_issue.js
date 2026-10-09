'use strict';

/**
 * Open or refresh a Scout issue. Called from actions/github-script.
 * No extra tokens: `github` is the built-in GITHUB_TOKEN client.
 */

function httpStatus(err) {
  if (!err) return 0;
  if (typeof err.status === 'number') return err.status;
  if (err.response && typeof err.response.status === 'number') return err.response.status;
  return 0;
}

async function ensureLabel(github, owner, repo, name, color, description) {
  try {
    await github.rest.issues.getLabel({owner, repo, name});
  } catch (err) {
    if (httpStatus(err) !== 404) throw err;
    try {
      await github.rest.issues.createLabel({owner, repo, name, color, description});
    } catch (createErr) {
      if (httpStatus(createErr) !== 422) throw createErr;
    }
  }
}

async function listOpenWithLabel(github, owner, repo, label) {
  const out = [];
  for (let page = 1; page <= 10; page++) {
    const {data} = await github.rest.issues.listForRepo({
      owner,
      repo,
      state: 'open',
      labels: label,
      per_page: 100,
      page,
    });
    out.push(...data);
    if (!data || data.length < 100) break;
  }
  return out;
}

function findSameDay(issues, titlePrefix) {
  if (!titlePrefix) return undefined;
  return (issues || []).find((issue) => {
    return issue && !issue.pull_request && typeof issue.title === 'string' && issue.title.startsWith(titlePrefix);
  });
}

async function openIssue({github, context, core, payload}) {
  const log = core || console;
  if (!payload || payload.action === 'none') {
    log.info('No PASS candidates; no issue opened');
    return {action: 'none'};
  }
  if (payload.action !== 'pass' && payload.action !== 'stale') {
    throw new Error(`unknown notify action: ${payload.action}`);
  }
  const owner = context.repo.owner;
  const repo = context.repo.repo;
  await ensureLabel(
    github,
    owner,
    repo,
    payload.label,
    payload.label_color,
    payload.label_description,
  );
  const open = await listOpenWithLabel(github, owner, repo, payload.label);
  const existing = findSameDay(open, payload.title_prefix);
  if (existing) {
    const body = `Re-run for ${payload.date}. Commenting on this open \`${payload.label}\` issue instead of opening a duplicate.\n\n${payload.body}`;
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number: existing.number,
      body,
    });
    try {
      await github.rest.issues.update({
        owner,
        repo,
        issue_number: existing.number,
        title: payload.title,
        body: payload.body,
      });
    } catch (err) {
      const warning = log.warning || log.info;
      warning(`Commented on #${existing.number} but could not refresh the title/body: ${err.message}`);
    }
    log.info(`Commented on #${existing.number} for ${payload.date}`);
    return {action: 'comment', issue_number: existing.number};
  }
  const created = await github.rest.issues.create({
    owner,
    repo,
    title: payload.title,
    body: payload.body,
    labels: [payload.label],
  });
  log.info(`Opened #${created.data.number}: ${payload.title}`);
  return {action: 'create', issue_number: created.data.number};
}

module.exports = {openIssue, findSameDay, ensureLabel};
