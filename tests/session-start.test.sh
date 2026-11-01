#!/usr/bin/env bash
# Runs scripts/session-start.sh against a stubbed gh and checks the context it prints.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
failures=0

make_sandbox() {
  local dir
  dir="$(mktemp -d)"
  mkdir -p "$dir/repo/scripts" "$dir/bin"
  cp "$ROOT/scripts/session-start.sh" "$dir/repo/scripts/"
  echo "$dir"
}

stub_gh_ok() {
  cat >"$1/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "repo list") echo "org=$3"; echo "- pumpkit (public): An AI writing tool for X" ;;
  "search prs") echo "- pumpkit#12 Fix the scheduler" ;;
esac
EOF
  chmod +x "$1/bin/gh"
}

stub_gh_failing() {
  printf '#!/usr/bin/env bash\nexit 1\n' >"$1/bin/gh"
  chmod +x "$1/bin/gh"
}

run_hook() {
  env -i HOME="$1" PATH="$1/bin:/usr/bin:/bin" bash "$1/repo/scripts/session-start.sh"
}

expect() {
  local name="$1" output="$2" needle="$3"
  if [[ "$output" != *"$needle"* ]]; then
    echo "FAIL: $name: expected to find: $needle"
    echo "--- output ---"; echo "$output"; echo "--------------"
    failures=$((failures + 1))
  fi
}

sandbox="$(make_sandbox)"
stub_gh_ok "$sandbox"
printf 'JARVIS_USER_NAME="Ada Lovelace"\nJARVIS_GITHUB_ORG=acme\n' >"$sandbox/repo/.env"
out="$(run_hook "$sandbox")"
expect "name from .env" "$out" "User name: Ada Lovelace"
expect "org from .env" "$out" "GitHub org: acme"
expect "org passed to gh" "$out" "org=acme"
expect "live repo list" "$out" "- pumpkit (public): An AI writing tool for X"
expect "open PRs" "$out" "- pumpkit#12 Fix the scheduler"

sandbox="$(make_sandbox)"
stub_gh_ok "$sandbox"
out="$(run_hook "$sandbox")"
expect "unknown name without .env" "$out" "User name: unknown"
expect "offer to set the name" "$out" "offer to set JARVIS_USER_NAME"
expect "default org" "$out" "GitHub org: pumpkit-io"
expect "org repos folder is the parent of the checkout" "$out" "Org repos folder: $sandbox"

sandbox="$(make_sandbox)"
stub_gh_failing "$sandbox"
printf 'JARVIS_USER_NAME=Ada\n' >"$sandbox/repo/.env"
out="$(run_hook "$sandbox")"
status=$?
expect "name survives gh failure" "$out" "User name: Ada"
expect "repo list failure reported" "$out" "Live repo list unavailable"
expect "PR list failure reported" "$out" "Open PRs unavailable"
if [[ $status -ne 0 ]]; then
  echo "FAIL: hook exited $status when gh failed"
  failures=$((failures + 1))
fi

if [[ $failures -eq 0 ]]; then echo "PASS"; else echo "$failures failure(s)"; exit 1; fi
