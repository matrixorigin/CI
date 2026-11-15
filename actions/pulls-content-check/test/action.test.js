import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const actionDir = join(dirname(fileURLToPath(import.meta.url)), '..');

test('bundled action rejects a null PR body without crashing', async () => {
    const tempDir = await mkdtemp(join(tmpdir(), 'pulls-content-check-'));
    const eventPath = join(tempDir, 'event.json');
    const outputPath = join(tempDir, 'output');
    let requestUrl;

    const server = createServer((request, response) => {
        requestUrl = request.url;
        response.writeHead(200, {'content-type': 'application/json'});
        response.end(JSON.stringify({body: null}));
    });

    try {
        server.listen(0, '127.0.0.1');
        await once(server, 'listening');
        const address = server.address();
        assert.equal(typeof address, 'object');

        await writeFile(eventPath, JSON.stringify({pull_request: {number: 27416}}));
        await writeFile(outputPath, '');

        const child = spawn(process.execPath, ['dist/index.js'], {
            cwd: actionDir,
            env: {
                ...process.env,
                GITHUB_API_URL: `http://127.0.0.1:${address.port}`,
                GITHUB_EVENT_NAME: 'pull_request_target',
                GITHUB_EVENT_PATH: eventPath,
                GITHUB_OUTPUT: outputPath,
                GITHUB_REPOSITORY: 'matrixorigin/matrixone',
                INPUT_GITHUB_TOKEN: 'test-token',
                INPUT_THIS_REPO: 'matrixorigin/matrixone',
                NO_PROXY: '127.0.0.1,localhost',
            },
        });

        let stdout = '';
        let stderr = '';
        child.stdout.setEncoding('utf8');
        child.stderr.setEncoding('utf8');
        child.stdout.on('data', chunk => stdout += chunk);
        child.stderr.on('data', chunk => stderr += chunk);

        const [exitCode, signal] = await once(child, 'exit');
        const logs = `${stdout}\n${stderr}`;
        assert.equal(signal, null, logs);
        assert.equal(exitCode, 1, logs);
        assert.doesNotMatch(logs, /TypeError|Cannot read properties of null/);
        assert.match(logs, /body of this pull_request is empty/);
        assert.equal(requestUrl, '/repos/matrixorigin/matrixone/pulls/27416');

        const output = await readFile(outputPath, 'utf8');
        assert.match(output, /pull_valid<<ghadelimiter_/);
        assert.match(output, /\nfalse\n/);
    } finally {
        if (server.listening) {
            await new Promise(resolve => server.close(resolve));
        }
        await rm(tempDir, {recursive: true, force: true});
    }
});
