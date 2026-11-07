import { spawnSync } from 'node:child_process';
import { secureExport } from './netlify-security.mjs';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const appDirectory = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outputDirectory = join(appDirectory, 'out');
const build = spawnSync(process.execPath, [join(appDirectory, 'node_modules/next/dist/bin/next'), 'build'], {
  cwd: appDirectory,
  env: { ...process.env, SMF_NETLIFY_EXPORT: 'true' },
  stdio: 'inherit',
});
if (build.error) throw build.error;
if (build.status !== 0) process.exit(build.status ?? 1);
if (!existsSync(join(outputDirectory, 'index.html'))) throw new Error('Static export index.html is missing.');

// Native rate limits live only in repository netlify.toml. Do not emit _redirects.
const generated = secureExport(outputDirectory);
console.log(`CSP generated with ${generated.hashes} approved inline script hashes.`);

const secretPatterns = [
  /sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}/,
  /GOCSPX-[A-Za-z0-9_-]{20,}/,
  /-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----/,
  /(?:postgres(?:ql)?|redis):\/\/[^\s"'<>]*:[^\s"'<>]+@/,
];
const environmentSecrets = Object.entries(process.env)
  .filter(([name, value]) => /(?:API_KEY|SECRET|PASSWORD|TOKEN|DATABASE_URL|REDIS_URL)$/.test(name) && value?.length >= 12)
  .map(([, value]) => value);
let files = 0;
let bytes = 0;
const inspect = (directory) => {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isSymbolicLink()) throw new Error('Static export must not contain symlinks.');
    if (entry.isDirectory()) { inspect(path); continue; }
    if (!entry.isFile()) throw new Error('Unexpected static export entry.');
    if (/^\.env(?:\.|$)/.test(entry.name) || /client_secret|credentials|\.pem$|\.key$/i.test(entry.name)) {
      throw new Error(`Sensitive filename in static export: ${relative(outputDirectory, path)}`);
    }
    const content = readFileSync(path).toString('utf8');
    if (secretPatterns.some((pattern) => pattern.test(content)) || environmentSecrets.some((value) => content.includes(value))) {
      // Report the filename only. Never log a matching credential.
      throw new Error(`Potential credential in static export: ${relative(outputDirectory, path)}`);
    }
    files += 1;
    bytes += statSync(path).size;
  }
};
inspect(outputDirectory);
console.log(`Netlify static export ready: ${outputDirectory}`);
console.log(`Verified ${files} files (${bytes} bytes); CSP and source-deploy routing checked; credential scan passed.`);
