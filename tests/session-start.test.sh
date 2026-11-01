#!/usr/bin/env bash
# Runs scripts/session-start.sh against a stubbed gh and checks the context it prints.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
failures=0
sandboxes=()
trap 'rm -rf "${sandboxes[@]}"' EXIT

REPOS_JSON='[
  {"name": "pumpkit", "visibility": "PUBLIC", "description": "An AI writing tool for X", "isArchived": false},
  {"name": "retired", "visibility": "PRIVATE", "description": "Old work", "isArchived": true},
  {"name": "bare", "visibility": "PUBLIC", "description": null, "isArchived": false}
]'
PRS_JSON='[
  {"repository": {"name": "pumpkit"}, "number": 12, "title": "Fix the scheduler", "isDraft": false},
  {"repository": {"name": "sdlc"}, "number": 3, "title": "Try a new skill", "isDraft": true}
]'

make_sandbox() {
  local dir
  dir="$(mktemp -d)"
  sandboxes+=("$dir")
  mkdir -p "$dir/repo/scripts" "$dir/bin"
  cp "$ROOT/scripts/session-start.sh" "$dir/repo/scripts/"
  SANDBOX="$dir"
}

# The stub logs its arguments and applies the real --jq filter to fixture JSON with jq.
stub_gh() {
  local repos="$2" prs="$3"
  cat >"$1/bin/gh" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$1/gh.log"
filter=""
for ((i = 1; i <= \$#; i++)); do [[ "\${!i}" == "--jq" ]] && { j=\$((i + 1)); filter="\${!j}"; }; done
case "\$1 \$2" in
  "repo list") printf '%s' '$repos' | jq -r "\$filter" ;;
  "search prs") printf '%s' '$prs' | jq -r "\$filter" ;;
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

refute() {
  local name="$1" output="$2" needle="$3"
  if [[ "$output" == *"$needle"* ]]; then
    echo "FAIL: $name: expected not to find: $needle"
    failures=$((failures + 1))
  fi
}

make_sandbox
stub_gh "$SANDBOX" "$REPOS_JSON" "$PRS_JSON"
printf 'JARVIS_USER_NAME="Ada Lovelace"\nJARVIS_GITHUB_ORG=acme\n' >"$SANDBOX/repo/.env"
out="$(run_hook "$SANDBOX")"
expect "name from .env" "$out" "User name: Ada Lovelace"
expect "org from .env" "$out" "GitHub org: acme"
expect "org passed to gh" "$(cat "$SANDBOX/gh.log")" "repo list acme"
expect "live repo with lowercased visibility" "$out" "- pumpkit (public): An AI writing tool for X"
expect "repo without description" "$out" "- bare (public): "
refute "no null description" "$out" "null"
refute "archived repos left out" "$out" "retired"
expect "open PR" "$out" "- pumpkit#12 Fix the scheduler"
expect "draft PR marked" "$out" "- sdlc#3 Try a new skill (draft)"

make_sandbox
stub_gh "$SANDBOX" "[]" "[]"
out="$(run_hook "$SANDBOX")"
expect "unknown name without .env" "$out" "User name: unknown"
expect "default org" "$out" "GitHub org: pumpkit-io"
expect "org repos folder is the parent of the checkout" "$out" "Org repos folder: $SANDBOX"
expect "empty lists say so" "$out" "## Open PRs in the org
None."

make_sandbox
stub_gh "$SANDBOX" "[]" "[]"
cat >"$SANDBOX/repo/.env" <<'EOF'
# Local settings
JARVIS_USER_NAME=Old Name
export JARVIS_USER_NAME = "Ada" # me
JARVIS_GITHUB_ORG=acme # my org
EOF
out="$(run_hook "$SANDBOX")"
expect "export, spaces and quoted value with comment" "$out" "User name: Ada"
refute "comment left out of quoted value" "$out" "# me"
expect "inline comment stripped" "$out" "GitHub org: acme"
refute "comment left out of unquoted value" "$out" "my org"

make_sandbox
stub_gh_failing "$SANDBOX"
printf 'JARVIS_USER_NAME=Ada\n' >"$SANDBOX/repo/.env"
out="$(run_hook "$SANDBOX")"
status=$?
expect "name survives gh failure" "$out" "User name: Ada"
expect "repo list failure reported" "$out" "Live repo list unavailable"
expect "PR list failure reported" "$out" "Open PRs unavailable"
if [[ $status -ne 0 ]]; then
  echo "FAIL: hook exited $status when gh failed"
  failures=$((failures + 1))
fi

if [[ $failures -eq 0 ]]; then echo "PASS"; else echo "$failures failure(s)"; exit 1; fi
