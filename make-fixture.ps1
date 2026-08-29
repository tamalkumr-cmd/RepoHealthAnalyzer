# make-fixture.ps1
# Builds a throwaway Git repo containing every defect RepoHealth is meant to find:
#   - a 3-file circular import chain (A -> B -> C -> A)
#   - a 2-file circular import chain (D -> E -> D)
#   - a deeply nested, high-complexity function
#   - an oversized class
#   - a syntactically broken file (tests graceful AST failure)
#   - a high-blast-radius module that many files import
#   - fake commit history so churn values differ per file
#
# Usage:  .\make-fixture.ps1  -Path E:\rh-fixture

param([string]$Path = "E:\rh-fixture")

if (Test-Path $Path) { Remove-Item $Path -Recurse -Force }
New-Item $Path -ItemType Directory -Force | Out-Null
Set-Location $Path

git init -q
git config user.email "fixture@test.local"
git config user.name  "Fixture Bot"

New-Item "pkg" -ItemType Directory -Force | Out-Null
New-Item "pkg\core" -ItemType Directory -Force | Out-Null
New-Item "pkg\__init__.py" -ItemType File -Force | Out-Null
New-Item "pkg\core\__init__.py" -ItemType File -Force | Out-Null

# ---- CYCLE 1: auth -> user -> db -> auth (matches the SRS diagram) ----

@'
"""Deliberately part of a 3-file import cycle, and deliberately complex."""
from pkg.user_service import get_user
from pkg.core.settings import TIMEOUT


def login(username, password, retries=3, strict=False, audit=None):
    """Cyclomatic complexity here is high on purpose."""
    if not username:
        return False
    if not password:
        return False
    for attempt in range(retries):
        if strict:
            while True:
                if get_user(username):
                    if audit:
                        if attempt > 1:
                            audit.warn("slow login")
                        else:
                            audit.info("ok")
                    return True
                elif attempt >= retries - 1:
                    break
                else:
                    continue
        else:
            if get_user(username):
                return True
    return False


def logout(session, force=False):
    if session is None:
        return
    if force or session.expired:
        session.close()
'@ | Set-Content "pkg\auth_controller.py" -Encoding utf8

@'
from pkg.db_connector import query


def get_user(username):
    return query(f"SELECT * FROM users WHERE name = {username}")


def list_users():
    return query("SELECT * FROM users")
'@ | Set-Content "pkg\user_service.py" -Encoding utf8

@'
from pkg.auth_controller import login
from pkg.core.settings import TIMEOUT


def query(sql):
    if not login("system", "internal"):
        return None
    return {"sql": sql, "timeout": TIMEOUT}
'@ | Set-Content "pkg\db_connector.py" -Encoding utf8

# ---- CYCLE 2: a shorter two-file loop ----

@'
from pkg.report_writer import write_report


def build_report(data):
    return write_report(data)
'@ | Set-Content "pkg\report_builder.py" -Encoding utf8

@'
from pkg.report_builder import build_report


def write_report(data):
    if not data:
        return build_report({})
    return str(data)
'@ | Set-Content "pkg\report_writer.py" -Encoding utf8

# ---- HIGH BLAST RADIUS: everything imports settings ----

@'
"""Imported by most modules -- should show the highest blast radius."""
TIMEOUT = 30
DEBUG = False
MAX_RETRIES = 5
'@ | Set-Content "pkg\core\settings.py" -Encoding utf8

# ---- OVERSIZED CLASS + DEEP NESTING ----

$body = @()
$body += '"""Contains an oversized class -- trips the max_class_loc threshold."""'
$body += 'from pkg.core.settings import DEBUG, MAX_RETRIES'
$body += ''
$body += ''
$body += 'class GodObject:'
$body += '    """This class is far too large on purpose."""'
$body += ''
for ($i = 1; $i -le 60; $i++) {
    $body += "    def method_$i(self, value):"
    $body += "        if DEBUG:"
    $body += "            return value * $i"
    $body += "        return value"
    $body += ''
}
$body += ''
$body += 'def deeply_nested(items, flag, limit):'
$body += '    """Nesting depth of 6 -- trips max_nesting_depth."""'
$body += '    total = 0'
$body += '    for a in items:'
$body += '        if flag:'
$body += '            for b in a:'
$body += '                while b > 0:'
$body += '                    try:'
$body += '                        if b % 2 == 0:'
$body += '                            total += b'
$body += '                    except ValueError:'
$body += '                        pass'
$body += '                    b -= 1'
$body += '    return total'
$body -join "`n" | Set-Content "pkg\god_module.py" -Encoding utf8

# ---- BROKEN FILE: must be skipped, not crash the run ----

@'
def broken_function(
    this file has a syntax error on purpose
'@ | Set-Content "pkg\broken_parser.py" -Encoding utf8

# ---- CLEAN FILES: should rank low ----

@'
from pkg.core.settings import TIMEOUT


def add(a, b):
    return a + b


def multiply(a, b):
    return a * b
'@ | Set-Content "pkg\utils.py" -Encoding utf8

@'
"""Imports a third-party package -- should NOT become a graph node."""
import json
import os
from pkg.utils import add


def load(path):
    with open(path) as fh:
        return add(len(json.load(fh)), len(os.sep))
'@ | Set-Content "pkg\loader.py" -Encoding utf8

# ---- FAKE HISTORY: different churn per file ----

git add -A
git commit -qm "Initial project structure"

# auth_controller gets edited 6 more times -> highest churn
for ($i = 1; $i -le 6; $i++) {
    Add-Content "pkg\auth_controller.py" "`n# revision $i"
    git add -A
    git commit -qm "Refactor auth flow (pass $i)"
}

# god_module edited 3 times
for ($i = 1; $i -le 3; $i++) {
    Add-Content "pkg\god_module.py" "`n# tweak $i"
    git add -A
    git commit -qm "Extend GodObject ($i)"
}

# utils edited once
Add-Content "pkg\utils.py" "`n# minor fix"
git add -A
git commit -qm "Fix utils rounding"

Write-Host ""
Write-Host "Fixture repo built at $Path" -ForegroundColor Green
Write-Host "Commits: $(git rev-list --count HEAD)"
Write-Host ""
Write-Host "Now run:" -ForegroundColor Cyan
Write-Host "  python -m repohealth.cli scan   $Path"
Write-Host "  python -m repohealth.cli report $Path --top 12"
Write-Host "  python -m repohealth.cli cycles $Path"