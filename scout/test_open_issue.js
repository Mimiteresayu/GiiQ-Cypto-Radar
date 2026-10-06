'use strict';

const assert = require('assert');
const {openIssue, findSameDay} = require('./open_issue');

function mockCore() {
  return {info() {}, warning() {}};
}

function mockGithub() {
  const calls = [];
  const labels = new Set();
  const issues = [];
  const github = {
    calls,
    rest: {
      issues: {
        async getLabel({name}) {
          calls.push(['getLabel', name]);
          if (!labels.has(name)) {
            const err = new Error('missing');
            err.status = 404;
            throw err;
          }
          return {data: {name}};
        },
        async createLabel({name}) {
          calls.push(['createLabel', name]);
          labels.add(name);
          return {data: {name}};
        },
        async listForRepo({labels: label}) {
          calls.push(['list', label]);
          return {data: issues.filter((issue) => (issue.labels || []).some((item) => item.name === label || item === label))};
        },
        async create(args) {
          calls.push(['create', args.title, args.labels, args.body]);
          const issue = {number: 40 + issues.length, title: args.title, labels: args.labels, body: args.body};
          issues.push(issue);
          return {data: {number: issue.number}};
        },
        async createComment(args) {
          calls.push(['comment', args.issue_number, args.body]);
          return {data: {id: 1}};
        },
        async update(args) {
          calls.push(['update', args.issue_number, args.title]);
          return {data: {number: args.issue_number}};
        },
      },
    },
  };
  return {github, calls, issues};
}

function passPayload(date) {
  return {
    action: 'pass',
    label: 'scout-pass',
    label_color: '0E8A16',
    label_description: 'Gate-1 PASS candidates for Cove',
    date,
    title: `Scout PASS ${date}: 1 candidate`,
    title_prefix: `Scout PASS ${date}:`,
    body: '1. **Northwind Carry** (`0xabc`)\n   - Copy-route PF: 1.48\n',
  };
}

async function main() {
  assert.strictEqual(findSameDay([
    {number: 3, title: 'Scout PASS 2026-10-06: 1 candidate'},
    {number: 4, title: 'Scout PASS 2026-10-07: 2 candidates'},
  ], 'Scout PASS 2026-10-06:').number, 3);
  assert.strictEqual(findSameDay([
    {number: 9, title: 'Scout PASS 2026-10-06: 1 candidate', pull_request: {}},
  ], 'Scout PASS 2026-10-06:'), undefined);

  const context = {repo: {owner: 'acme', repo: 'radar'}};

  {
    const {github, calls} = mockGithub();
    const result = await openIssue({github, context, core: mockCore(), payload: {action: 'none', pass_count: 0}});
    assert.strictEqual(result.action, 'none');
    assert.deepStrictEqual(calls, []);
  }

  {
    const {github, calls} = mockGithub();
    const result = await openIssue({github, context, core: mockCore(), payload: passPayload('2026-10-06')});
    assert.strictEqual(result.action, 'create');
    assert.ok(calls.some((call) => call[0] === 'createLabel' && call[1] === 'scout-pass'));
    const created = calls.find((call) => call[0] === 'create');
    assert.strictEqual(created[1], 'Scout PASS 2026-10-06: 1 candidate');
    assert.ok(created[3].includes('Northwind Carry'));
    assert.ok(created[3].includes('Copy-route PF: 1.48'));
    assert.ok(!calls.some((call) => call[0] === 'comment'));
  }

  {
    const {github, calls, issues} = mockGithub();
    issues.push({
      number: 7,
      title: 'Scout PASS 2026-10-06: 1 candidate',
      labels: ['scout-pass'],
    });
    const result = await openIssue({github, context, core: mockCore(), payload: passPayload('2026-10-06')});
    assert.strictEqual(result.action, 'comment');
    assert.strictEqual(result.issue_number, 7);
    assert.ok(!calls.some((call) => call[0] === 'create'));
    const comment = calls.find((call) => call[0] === 'comment');
    assert.strictEqual(comment[1], 7);
    assert.ok(comment[2].includes('instead of opening a duplicate'));
    assert.ok(comment[2].includes('Northwind Carry'));
  }

  {
    const {github, calls, issues} = mockGithub();
    issues.push({
      number: 8,
      title: 'Scout PASS 2026-10-05: 1 candidate',
      labels: ['scout-pass'],
    });
    const result = await openIssue({github, context, core: mockCore(), payload: passPayload('2026-10-06')});
    assert.strictEqual(result.action, 'create');
    assert.ok(calls.some((call) => call[0] === 'create'));
  }

  console.log('open_issue tests passed');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
