#!/usr/bin/env bash
# SessionStart hook: prints the context Jarvis needs before the first word. Claude Code adds stdout to the session.
set -u

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
# .env and the Org clones belong to the main checkout, which a worktree's own path would hide.
common_git_dir="$(git -C "$repo_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
main_checkout="$(dirname "${common_git_dir:-$repo_dir/.git}")"

# Parsed rather than sourced, so a stray line in .env never runs as a command.
env_value() {
  [[ -f "$main_checkout/.env" ]] || return 0
  grep -E "^[[:space:]]*$1=" "$main_checkout/.env" | tail -n 1 | cut -d= -f2- |
    sed -E 's/^[[:space:]]+|[[:space:]]+$//g; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
}

user_name="$(env_value JARVIS_USER_NAME)"
org="$(env_value JARVIS_GITHUB_ORG)"
org="${org:-pumpkit-io}"

echo "# Jarvis session context"
echo
if [[ -n "$user_name" ]]; then
  echo "User name: $user_name"
else
  echo "User name: unknown. JARVIS_USER_NAME is not set in .env: offer to set JARVIS_USER_NAME for the User."
fi
echo "GitHub org: $org"
echo "Org repos folder: $(dirname "$main_checkout")"

echo
echo "## Org repos (live)"
if ! gh repo list "$org" --limit 100 --json name,visibility,description,isArchived \
  --jq '.[] | select(.isArchived | not) | "- \(.name) (\(.visibility | ascii_downcase)): \(.description)"' 2>/dev/null; then
  echo "Live repo list unavailable: gh failed. Work from the Repo catalog and mention the failure once."
fi

echo
echo "## Open PRs in the org"
if ! prs="$(gh search prs --owner "$org" --state open --limit 50 --json repository,number,title,isDraft \
  --jq '.[] | "- \(.repository.name)#\(.number) \(.title)\(if .isDraft then " (draft)" else "" end)"' 2>/dev/null)"; then
  echo "Open PRs unavailable: gh failed."
elif [[ -z "$prs" ]]; then
  echo "None."
else
  echo "$prs"
fi
