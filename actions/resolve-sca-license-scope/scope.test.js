// Copyright 2026 Matrix Origin
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { run, select } = require('./scope');

const head = 'a'.repeat(40), base = 'b'.repeat(40);
const listFiles = { defaults: () => Symbol('listFiles') };
const revision = changed_files => ({ number: 42, head: { sha: head }, base: { sha: base }, changed_files });
const contextFor = changedFiles => ({
  repo: { owner: 'matrixorigin', repo: 'matrixone' },
  payload: { pull_request: revision(changedFiles) },
});
const checkout = (command, args, options) => {
  assert.equal(command, 'git');
  assert.deepEqual(args, ['rev-parse', 'HEAD']);
  assert.equal(options.timeout, 5000);
  return head + '\n';
};

test('checks an ordinary changed source file only', () => {
  assert.deepEqual(select([
    { status: 'modified', filename: 'pkg/example/a.go' },
    { status: 'modified', filename: 'README.md' },
  ], 2), {
    paths: ['pkg/example/a.go'],
    headerFull: false,
    dependencyCheck: false,
  });
});

test('does not check deleted source files', () => {
  assert.deepEqual(select([
    { status: 'removed', filename: 'pkg/example/a.go' },
  ], 1), { paths: [], headerFull: false, dependencyCheck: false });
});

test('checks the destination of a source rename', () => {
  assert.deepEqual(select([
    { status: 'renamed', filename: 'pkg/new.go', previous_filename: 'pkg/old.go' },
  ], 1), {
    paths: ['pkg/new.go'],
    headerFull: false,
    dependencyCheck: false,
  });
});

test('a renamed policy input forces both complete checks', () => {
  assert.deepEqual(select([
    { status: 'renamed', filename: 'license.yml', previous_filename: '.licenserc.yml' },
  ], 1), { paths: [], headerFull: true, dependencyCheck: true });
});

test('an incomplete API result forces both complete checks', () => {
  assert.deepEqual(select([
    { status: 'modified', filename: 'README.md' },
  ], 2), { paths: [], headerFull: true, dependencyCheck: true });
});

test('the pull-files API ceiling forces both complete checks', () => {
  const files = Array.from({ length: 3000 }, (_, index) => ({
    status: 'modified',
    filename: `docs/${index}.md`,
  }));
  assert.deepEqual(select(files, 3000), {
    paths: [],
    headerFull: true,
    dependencyCheck: true,
  });
});

test('a glob-like legal filename forces both complete checks', () => {
  assert.deepEqual(select([
    { status: 'added', filename: 'pkg/a[1].go' },
  ], 1), { paths: ['pkg/a[1].go'], headerFull: true, dependencyCheck: true });
});

test('dependency inputs do not force an unrelated full header scan', () => {
  assert.deepEqual(select([
    { status: 'modified', filename: 'go.mod' },
  ], 1), { paths: [], headerFull: false, dependencyCheck: true });
});

test('run publishes a NUL-delimited path file and conservative flags', async (t) => {
  const runnerTemp = fs.mkdtempSync(path.join(os.tmpdir(), 'sca-license-scope-'));
  t.after(() => fs.rmSync(runnerTemp, { recursive: true, force: true }));
  const exported = new Map();
  await run({
    runnerTemp,
    // Completeness comes from the revision-checked API, not an optional event count.
    context: contextFor(undefined),
    git: checkout,
    github: {
      paginate: async (_method, request) => {
        assert.equal(request.pull_number, 42);
        return [
          { status: 'modified', filename: 'pkg/a.go' },
          { status: 'modified', filename: 'go.mod' },
        ];
      },
      rest: { pulls: { listFiles, get: async () => ({ data: revision(2) }) } },
    },
    core: {
      exportVariable: (name, value) => exported.set(name, value),
      info: () => {},
    },
  });

  assert.equal(
    fs.readFileSync(exported.get('SCA_LICENSE_PATHS_FILE'), 'utf8'),
    'pkg/a.go\0',
  );
  assert.equal(exported.get('SCA_LICENSE_HEADER_FULL'), 'false');
  assert.equal(exported.get('SCA_LICENSE_DEPENDENCY_CHECK'), 'true');
});

test('non-PR events publish complete-check defaults', async (t) => {
  const runnerTemp = fs.mkdtempSync(path.join(os.tmpdir(), 'sca-license-scope-'));
  t.after(() => fs.rmSync(runnerTemp, { recursive: true, force: true }));
  const exported = new Map();
  await run({
    runnerTemp,
    context: { payload: {} },
    github: {},
    core: {
      exportVariable: (name, value) => exported.set(name, value),
      info: () => {},
    },
  });

  assert.equal(exported.get('SCA_LICENSE_HEADER_FULL'), 'true');
  assert.equal(exported.get('SCA_LICENSE_DEPENDENCY_CHECK'), 'true');
  assert.equal(fs.readFileSync(exported.get('SCA_LICENSE_PATHS_FILE'), 'utf8'), '');
});

test('an API failure propagates before publishing partial state', async t => {
  const runnerTemp = fs.mkdtempSync(path.join(os.tmpdir(), 'sca-license-api-'));
  t.after(() => fs.rmSync(runnerTemp, { recursive: true, force: true }));
  const exported = new Map();
  await assert.rejects(run({
    runnerTemp,
    context: contextFor(1),
    git: checkout,
    github: {
      paginate: async () => { throw new Error('rate limited'); },
      rest: { pulls: { listFiles, get: async () => ({ data: revision(1) }) } },
    },
    core: {
      exportVariable: (name, value) => exported.set(name, value),
      info: () => {},
    },
  }), /rate limited/);
  assert.equal(exported.size, 0);
});


test('all configured source extensions participate in header selection', () => {
  const files = ['go', 'c', 's', 'h', 'cpp', 'proto'].map(extension => ({ status: 'added', filename: `pkg/source.${extension}` }));
  assert.deepEqual(select(files, files.length).paths, files.map(file => file.filename));
});

test('revision, checkout and API failures publish no partial exemption', async t => {
  const runnerTemp = fs.mkdtempSync(path.join(os.tmpdir(), 'sca-license-revision-'));
  t.after(() => fs.rmSync(runnerTemp, { recursive: true, force: true }));
  for (const failure of ['invalid-event', 'checkout', 'git-error', 'before-head', 'before-base', 'after-head', 'after-base', 'get-before', 'get-after']) {
    await t.test(failure, async () => {
      const exported = new Map();
      const context = contextFor(1);
      if (failure === 'invalid-event') context.payload.pull_request.head.sha = 'not-a-sha';
      let gets = 0, transactionSignal;
      const checkRequest = request => {
        assert.equal(request.pull_number, 42);
        assert.ok(request.request.signal instanceof AbortSignal);
        transactionSignal ||= request.request.signal;
        assert.equal(request.request.signal, transactionSignal, 'all calls share one transaction deadline');
      };
      await assert.rejects(run({
        runnerTemp, context,
        git: (...args) => {
          if (failure === 'git-error') throw new Error('git unavailable');
          return failure === 'checkout' ? base : checkout(...args);
        },
        github: {
          paginate: async (_method, request) => { checkRequest(request); return [{ status: 'modified', filename: 'pkg/a.go' }]; },
          rest: { pulls: {
            listFiles,
            get: async request => {
              checkRequest(request);
              const phase = ++gets === 1 ? 'before' : 'after';
              if (failure === `get-${phase}`) throw new Error('metadata unavailable');
              const data = revision(1);
              if (failure === `${phase}-head`) data.head.sha = 'c'.repeat(40);
              if (failure === `${phase}-base`) data.base.sha = 'c'.repeat(40);
              return { data };
            },
          } },
        },
        core: { exportVariable: (name, value) => exported.set(name, value), info: () => {} },
      }));
      assert.equal(exported.size, 0);
      assert.equal(fs.existsSync(path.join(runnerTemp, 'matrixone-sca-license-paths')), false);
    });
  }
});

// CI supplies the client from the same pinned github-script action that runs
// the selector. Unit mocks cannot prove that its pagination retains a signal.
module.exports.testPagination = Octokit => test('the real client aborts an in-flight later page without publishing exemptions', { timeout: 5000 }, async t => {
  const runnerTemp = fs.mkdtempSync(path.join(os.tmpdir(), 'sca-license-pagination-'));
  t.after(() => fs.rmSync(runnerTemp, { recursive: true, force: true }));
  const controller = new AbortController();
  t.mock.method(AbortSignal, 'timeout', () => controller.signal);
  const requests = [], exported = new Map();
  const server = http.createServer((request, response) => {
    requests.push(request.url);
    if (request.url.endsWith('page=2')) {
      // Expire only once a later page is actually in flight; no timing sleeps.
      controller.abort();
      return;
    }
    response.setHeader('Content-Type', 'application/json');
    if (request.url.includes('/files?')) {
      response.setHeader('Link', `<http://127.0.0.1:${server.address().port}/repos/matrixorigin/matrixone/pulls/42/files?page=2>; rel="next"`);
      response.end(JSON.stringify([{ status: 'modified', filename: 'pkg/a.go' }]));
    } else {
      response.end(JSON.stringify(revision(2)));
    }
  });
  t.after(() => new Promise(resolve => {
    server.closeAllConnections();
    server.close(resolve);
  }));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  await assert.rejects(run({
    runnerTemp, context: contextFor(2), git: checkout,
    github: new Octokit({
      baseUrl: `http://127.0.0.1:${server.address().port}`, auth: 'local-test-only',
      retry: { enabled: false }, // github-script defaults to zero retries.
      // github-script's dispatcher is bound to the GitHub proxy. Keep this
      // local fixture direct while retaining its real pagination client.
      request: { fetch: globalThis.fetch },
    }),
    core: { exportVariable: (name, value) => exported.set(name, value), info: () => {} },
  }), /abort/i);
  assert.equal(controller.signal.aborted, true);
  assert.deepEqual(requests, [
    '/repos/matrixorigin/matrixone/pulls/42',
    '/repos/matrixorigin/matrixone/pulls/42/files?per_page=100',
    '/repos/matrixorigin/matrixone/pulls/42/files?page=2',
  ]);
  assert.equal(exported.size, 0);
  assert.equal(fs.existsSync(path.join(runnerTemp, 'matrixone-sca-license-paths')), false);
});
