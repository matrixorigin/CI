import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizePullContent } from '../src/pull-content.js';

test('normalizePullContent preserves a PR body string', () => {
    assert.equal(normalizePullContent('## Issue\n#27416'), '## Issue\n#27416');
});

for (const body of [null, undefined, '']) {
    test(`normalizePullContent treats ${String(body)} as empty content`, () => {
        assert.equal(normalizePullContent(body), '');
    });
}
