#!/usr/bin/env bash
# SessionStart hook: prints the context Jarvis needs before the first word. Claude Code adds stdout to the session.
set -u

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
# .env and the Org clones belong to the main checkout, which a worktree's own path would hide.
common_git_dir="$(git -C "$repo_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
main_checkout="$(dirname "${common_git_dir:-$repo_dir/.git}")"

# Parsed rather than sourced, so a stray line in .env never runs as a command. The last assignment wins.
env_value() {
  local name="$1" line value="" found=""
  local assignment='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$'
  local double_quoted='^"([^"]*)"' single_quoted="^'([^']*)'"
  [[ -f "$main_checkout/.env" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ $assignment && "${BASH_REMATCH[2]}" == "$name" ]] || continue
    value="${BASH_REMATCH[3]}"
    if [[ "$value" =~ $double_quoted || "$value" =~ $single_quoted ]]; then
      value="${BASH_REMATCH[1]}"
    else
      [[ "$value" == "#"* ]] && value=""
      value="${value%%[[:space:]]#*}"
      value="${value%"${value##*[![:space:]]}"}"
    fi
    found="$value"
  done <"$main_checkout/.env"
  printf '%s' "$found"
}

# Prints the gh output, "None." when it is empty, or the fallback when gh fails.
gh_list() {
  local fallback="$1" output
  shift
  if ! output="$(gh "$@" 2>/dev/null)"; then
    echo "$fallback"
  elif [[ -z "$output" ]]; then
    echo "None."
  else
    echo "$output"
  fi
}

user_name="$(env_value JARVIS_USER_NAME)"
org="$(env_value JARVIS_GITHUB_ORG)"
org="${org:-pumpkit-io}"

echo "# Jarvis session context"
echo
echo "User name: ${user_name:-unknown (JARVIS_USER_NAME is not set in .env)}"
echo "GitHub org: $org"
echo "Org repos folder: $(dirname "$main_checkout")"

echo
echo "## Org repos (live)"
gh_list "Live repo list unavailable: gh failed. Work from the Repo catalog and mention the failure once." \
  repo list "$org" --limit 100 --json name,visibility,description,isArchived \
  --jq '.[] | select(.isArchived | not) | "- \(.name) (\(.visibility | ascii_downcase)): \(.description // "")"'

echo
echo "## Open PRs in the org"
gh_list "Open PRs unavailable: gh failed." \
  search prs --owner "$org" --state open --limit 50 --json repository,number,title,isDraft \
  --jq '.[] | "- \(.repository.name)#\(.number) \(.title)\(if .isDraft then " (draft)" else "" end)"'
