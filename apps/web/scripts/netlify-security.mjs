import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export function scriptHashes(html) {
  return [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)]
    .filter(([, attributes, body]) => !/\bsrc\s*=/i.test(attributes) && body.trim())
    .map(([, , body]) => `'sha256-${createHash('sha256').update(body).digest('base64')}'`);
}

export function contentPolicy(hashes) {
  return [
    "default-src 'self'", `script-src 'self' ${[...new Set(hashes)].sort().join(' ')}`,
    "script-src-attr 'none'", "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:", "font-src 'self'", "connect-src 'self'",
    "media-src 'self' blob: https://smf-5-223-44-216.sslip.io",
    "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'",
    "form-action 'self'", 'upgrade-insecure-requests',
  ].join('; ');
}

export function secureExport(directory) {
  const hashes = [];
  const scan = (current) => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const path = join(current, entry.name);
      if (entry.isDirectory()) scan(path);
      else if (entry.isFile() && entry.name.endsWith('.html')) hashes.push(...scriptHashes(readFileSync(path, 'utf8')));
    }
  };
  scan(directory);
  if (!hashes.length) throw new Error('No Next hydration scripts found; CSP generation stopped.');
  const policy = contentPolicy(hashes);
  // _redirects takes priority over TOML and cannot represent rate_limit rules.
  // Requiring source deployment prevents an unprotected duplicate proxy rule.
  rmSync(join(directory, '_redirects'), { force: true });
  writeFileSync(join(directory, '_headers'), `/*\n  Content-Security-Policy: ${policy}\n`);
  return { hashes: new Set(hashes).size, policy };
}
