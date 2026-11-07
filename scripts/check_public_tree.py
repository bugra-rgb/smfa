"""Scan the tracked tree without printing potential secret values.

Use after git add. This is a small release guardrail, not a full security audit.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----'),
    'provider token': re.compile(r'(?:sk-(?:proj-|svcacct-)?|GOCSPX-|github_pat_|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{24,}'),
    'Google API key': re.compile(r'AIza[A-Za-z0-9_-]{30,}'),
}


def check(paths):
    errors = []
    for path in paths:
        p = ROOT / path
        parts = Path(path).parts
        if (p.name.startswith('.env') and p.name != '.env.example') or p.suffix.lower() in ('.pem', '.key', '.db', '.sqlite', '.sqlite3', '.zip', '.log'):
            errors.append((path, 'private/artifact filename'))
        if any(part in ('.venv', 'node_modules', '.next', 'data', 'backups') for part in parts):
            errors.append((path, 'generated or runtime directory'))
        if p.is_symlink():
            errors.append((path, 'symlink requires manual review'))
            continue
        try:
            text = p.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            errors.append((path, 'binary requires manual review'))
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                errors.append((path, label))
    return errors


if __name__ == '__main__':
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True)
    paths = [p for p in result.stdout.decode('utf-8').split('\0') if p]
    if not paths:
        raise SystemExit('No tracked files; stage the intended release first.')
    errors = check(paths)
    for path, label in errors:
        print(f'{path}: {label}')
    print(f'Checked {len(paths)} tracked files; findings: {len(errors)}')
    sys.exit(bool(errors))
