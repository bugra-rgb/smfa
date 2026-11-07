import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { scriptHashes, contentPolicy, secureExport } from '../scripts/netlify-security.mjs';

test('CSP hashes the exact inline script bytes and never allows arbitrary inline scripts', () => {
  const body = 'self.__next_f.push([1,"hello"]);\n';
  const hashes = scriptHashes(`<script>${body}</script><script src="/safe.js"></script>`);
  assert.deepEqual(hashes, [`'sha256-${createHash('sha256').update(body).digest('base64')}'`]);
  const policy = contentPolicy(hashes);
  const scripts = policy.split('; ').find((part) => part.startsWith('script-src '));
  assert(!scripts.includes('unsafe-inline') && !scripts.includes('unsafe-eval'));
  assert(policy.includes("frame-ancestors 'none'") && policy.includes("connect-src 'self'"));
});

test('exports keep rate limited TOML routing authoritative', () => {
  const directory = mkdtempSync(join(tmpdir(), 'smf-csp-'));
  try {
    writeFileSync(join(directory, 'index.html'), '<script>self.__next_f=[];</script>');
    writeFileSync(join(directory, '_redirects'), '/api/* https://example.test/:splat 200');
    secureExport(directory);
    assert(!existsSync(join(directory, '_redirects')));
    assert(readFileSync(join(directory, '_headers'), 'utf8').includes('Content-Security-Policy:'));
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
