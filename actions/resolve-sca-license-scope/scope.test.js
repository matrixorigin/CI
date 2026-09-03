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
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { run, select } = require('./scope');

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
    context: {
      repo: { owner: 'matrixorigin', repo: 'matrixone' },
      payload: { pull_request: { number: 42, changed_files: 2 } },
    },
    github: {
      paginate: async (_method, request) => {
        assert.equal(request.pull_number, 42);
        return [
          { status: 'modified', filename: 'pkg/a.go' },
          { status: 'modified', filename: 'go.mod' },
        ];
      },
      rest: { pulls: { listFiles: Symbol('listFiles') } },
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

test('an API failure propagates before publishing partial state', async () => {
  const exported = new Map();
  await assert.rejects(run({
    runnerTemp: os.tmpdir(),
    context: {
      repo: { owner: 'matrixorigin', repo: 'matrixone' },
      payload: { pull_request: { number: 42, changed_files: 1 } },
    },
    github: {
      paginate: async () => { throw new Error('rate limited'); },
      rest: { pulls: { listFiles: Symbol('listFiles') } },
    },
    core: {
      exportVariable: (name, value) => exported.set(name, value),
      info: () => {},
    },
  }), /rate limited/);
  assert.equal(exported.size, 0);
});
