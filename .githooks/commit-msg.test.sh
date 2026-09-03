#!/bin/sh
# Self-check for the commit-msg hook. Run: sh .githooks/commit-msg.test.sh
set -eu
hook="$(dirname "$0")/commit-msg"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
fails=0

check() { # check <expect: ok|no> <message...>
	want=$1; shift
	printf '%s' "$1" >"$tmp"
	if [ $# -gt 1 ]; then shift; for l in "$@"; do printf '\n%s' "$l" >>"$tmp"; done; fi
	printf '\n' >>"$tmp"          # git always writes a trailing newline
	if "$hook" "$tmp" >/dev/null 2>&1; then got=ok; else got=no; fi
	if [ "$got" != "$want" ]; then
		printf 'FAIL want=%s got=%s: %s\n' "$want" "$got" "$(sed -n 1p "$tmp")"
		fails=$((fails + 1))
	fi
}

check ok "feat(collector): add browser fallback on sign failure"
check ok "fix: guard empty search result"
check ok "chore(deps): pin playwright to 1.49"
check ok "revert: feat(collector): add browser fallback"
check ok "Merge branch 'main'"
check ok "docs(spec): document pacing rules" "" "- explain why 60s is a floor" "" "Closes #12"

check no "feature(collector): add fallback"                  # unknown type
check no "feat(scraper): add fallback"                        # unknown scope
check no "feat(collector) add fallback"                       # missing colon
check no "feat: Add fallback"                                 # uppercase subject
check no "feat: add fallback."                                # trailing period
check no "feat: add a browser based fallback collector that also normalizes prices"  # >50
check no "add fallback"                                       # no type
check no "feat: add fallback" "Body without blank line"       # missing blank line

# body must be changelog-style bullets
check ok "feat(db): add price snapshot table" "" "- store price as integer cents" "- index captured_at for history queries"
check ok "fix(api): reject sub-60s intervals" "" "- add ge=60 constraint on interval_seconds" "" "Closes #7"
check ok "chore: bump ruff" "" "- bump ruff to 0.16.6" "" "Co-Authored-By: Someone <x@y.z>"
check ok "docs(spec): note pacing floor" "" "- document the 60s floor and why it exists," "  including the jitter requirement"
check no "feat(db): add price snapshot table" "" "This commit adds a table for prices."   # prose body
check no "feat(db): add snapshots" "" "- store price as integer cents" "and index it"     # unbulleted continuation
check no "feat(db): add snapshots" "" "* store price as integer cents"                    # wrong bullet char
check no "chore: bump ruff" "" "- bump ruff" "" "Co-Authored-By: Claude <noreply@anthropic.com>"  # no AI co-author

[ "$fails" -eq 0 ] && echo "commit-msg hook: all checks pass" || { echo "$fails check(s) failed"; exit 1; }
