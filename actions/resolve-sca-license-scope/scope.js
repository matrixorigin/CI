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

const fs = require('fs');
const path = require('path');

const sourcePattern = /\.(?:go|c|s|h|cpp|proto)$/;
const globPattern = /[*?[\]{}\\]/;
const maxPullFiles = 3000;
const headerRuleInputs = new Set(['.gitignore', '.licenserc.yml', 'Makefile']);
const dependencyInputs = new Set(['.licenserc.yml', 'Makefile', 'go.mod', 'go.sum']);

function select(files, changedFiles) {
  const paths = files
    .filter((file) => file.status !== 'removed')
    .map((file) => file.filename)
    // Command-line paths replace, rather than intersect with, the configured
    // header paths. Keep this extension set aligned with MatrixOne's
    // .licenserc.yml; a config change forces a complete check below.
    .filter((file) => sourcePattern.test(file));
  const names = new Set(files.flatMap((file) =>
    [file.filename, file.previous_filename].filter(Boolean)));
  // GitHub caps this endpoint at 3000 files. Treat the ceiling itself as
  // incomplete because a capped changed-files count cannot prove completeness.
  const incomplete = files.length !== changedFiles || files.length >= maxPullFiles;
  // license-eye treats path arguments as globs. A legal filename containing
  // glob metacharacters must never be interpreted as a different file set.
  const hasGlobPath = paths.some((file) => globPattern.test(file));
  const headerRulesChanged = [...headerRuleInputs].some((file) => names.has(file));
  const dependenciesChanged = [...dependencyInputs].some((file) => names.has(file));
  const forceFull = incomplete || hasGlobPath;

  return {
    paths,
    headerFull: forceFull || headerRulesChanged,
    dependencyCheck: forceFull || dependenciesChanged,
  };
}

function publish(core, output, result) {
  fs.writeFileSync(output, result.paths.length ? `${result.paths.join('\0')}\0` : '');
  core.exportVariable('SCA_LICENSE_PATHS_FILE', output);
  core.exportVariable('SCA_LICENSE_HEADER_FULL', String(result.headerFull));
  core.exportVariable('SCA_LICENSE_DEPENDENCY_CHECK', String(result.dependencyCheck));
}

async function run({ github, context, core, runnerTemp = process.env.RUNNER_TEMP }) {
  const output = path.join(runnerTemp, 'matrixone-sca-license-paths');
  const pull = context.payload.pull_request;
  if (!pull) {
    publish(core, output, { paths: [], headerFull: true, dependencyCheck: true });
    core.info('Non-PR event: use complete license checks');
    return;
  }

  const files = await github.paginate(github.rest.pulls.listFiles, {
    owner: context.repo.owner,
    repo: context.repo.repo,
    pull_number: pull.number,
    per_page: 100,
  });
  const result = select(files, pull.changed_files);
  publish(core, output, result);
  core.info(`PR files=${files.length}/${pull.changed_files}, header paths=${result.paths.length}, full=${result.headerFull}, dependency=${result.dependencyCheck}`);
}

module.exports = { run, select };
