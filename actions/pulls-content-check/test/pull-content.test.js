import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizePullContent } from '../src/pull-content.js';

test('normalizePullContent preserves a PR body string', () => {
    assert.equal(normalizePullContent('## Issue\n#27416'), '## Issue\n#27416');
});

for (const [name, body] of [['null', null], ['undefined', undefined], ['an empty string', '']]) {
    test(`normalizePullContent treats ${name} as empty content`, () => {
        assert.equal(normalizePullContent(body), '');
    });
}
