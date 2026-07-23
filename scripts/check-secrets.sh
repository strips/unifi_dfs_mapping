#!/usr/bin/env bash
# Secrets scanner for this repo - no external dependencies (pure bash/git/grep).
#
# Modes:
#   --staged   scan the currently staged diff (default; used as the pre-commit hook)
#   --all      scan every tracked file's current content in the working tree
#   --history  scan every commit ever made in this repo (run this before the
#              first `git push` to a new remote, e.g. before publishing to GitHub -
#              the pre-commit hook only guards commits made *after* it's installed)
#
# HARD hits (near-certain secrets: private key blocks, cloud tokens,
# `PASSWORD=literalvalue`) fail the scan. SOFT hits (long hex strings, which
# are often just checksums/IDs) are printed as warnings only, so noisy but
# harmless matches never block a commit.
#
# Bypass (only if you've manually confirmed a HARD hit is a false positive):
#   git commit --no-verify
set -euo pipefail

mode="${1:---staged}"
self="scripts/check-secrets.sh"

# Filenames that must never be committed, regardless of content.
blocked_name_re='(^|/)(\.env(\..*)?|credentials\.env|[^/]*\.pem|[^/]*\.key|[^/]*\.p12|[^/]*\.pfx|id_rsa|id_ed25519|[^/]*\.tfstate|[^/]*\.tfstate\.backup|[^/]*\.auto\.tfvars)$'

# HARD: near-certain secret content.
hard_re='-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'
hard_re+='|AKIA[0-9A-Z]{16}'
hard_re+='|gh[pousr]_[A-Za-z0-9]{20,}'
hard_re+='|xox[baprs]-[A-Za-z0-9-]{10,}'
hard_re+='|(PASS(WORD)?|PWD|SECRET|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY)[A-Za-z0-9_]*[[:space:]]*[:=][[:space:]]*["'"'"']?[^$[:space:]"'"'"'][^[:space:]"'"'"']{7,}'

# SOFT: worth a human glance, but too noisy to hard-block (e.g. checksums).
soft_re='[0-9a-fA-F]{32,}'

fail=0

check_names() {
  local f
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    # Committed templates (e.g. .env.example, secrets/secrets.example.env,
    # config/config.example.yaml) are deliberately tracked and contain no
    # real values - don't flag the filename pattern for these.
    [[ "$f" =~ \.(example|sample)$ ]] && continue
    if [[ "$f" =~ $blocked_name_re ]]; then
      echo "BLOCKED: '$f' matches a secret-file pattern - never commit this." >&2
      fail=1
    fi
  done <<<"$1"
}

check_content() {
  local text="$1" hard_hits soft_hits
  hard_hits=$(grep -nE -e "$hard_re" <<<"$text" || true)
  if [[ -n "$hard_hits" ]]; then
    echo "BLOCKED: content looks like a real secret:" >&2
    echo "$hard_hits" >&2
    fail=1
  fi
  soft_hits=$(grep -nE -e "$soft_re" <<<"$text" || true)
  if [[ -n "$soft_hits" ]]; then
    echo "WARN: long hex string found - review manually, not auto-blocked:" >&2
    echo "$soft_hits" >&2
  fi
}

case "$mode" in
  --staged)
    check_names "$(git diff --cached --name-only --diff-filter=ACM)"
    diff_added="$(git diff --cached -U0 --no-color -- . ":!${self}" | grep -E '^\+[^+]' | sed 's/^+//' || true)"
    check_content "$diff_added"
    ;;
  --all)
    check_names "$(git ls-files)"
    all_content="$(git ls-files -z -- . ":!${self}" | xargs -0 cat 2>/dev/null || true)"
    check_content "$all_content"
    ;;
  --history)
    check_names "$(git log --all --diff-filter=A --name-only --pretty=format: | sort -u)"
    hist_added="$(git log --all -p -- . ":!${self}" | grep -E '^\+[^+]' | sed 's/^+//' || true)"
    check_content "$hist_added"
    ;;
  *)
    echo "usage: $0 [--staged|--all|--history]" >&2
    exit 2
    ;;
esac

if [[ "$fail" -eq 1 ]]; then
  echo >&2
  echo "If this is a genuine false positive: git commit --no-verify (staged mode only)." >&2
  exit 1
fi

echo "check-secrets ($mode): no HARD-confidence secrets found."
exit 0
