#!/usr/bin/env bash
# What a synaps-dev room session can do with GitHub (#26). Run in the session's clone, with the session's env.
# Prints names and exit codes, never a secret. The push is a --dry-run to another repo: nothing is written even
# if it were allowed (it is not: the deploy key only has the acme-shop fork).
other=git@github.com:HaseebKhalid1507/synaps-switch.git
echo "GH_CONFIG_DIR=$GH_CONFIG_DIR (files: $(ls -A "$GH_CONFIG_DIR" 2>/dev/null | wc -l))"
echo "SSH_AUTH_SOCK=${SSH_AUTH_SOCK:-<unset>}"
echo "GIT_SSH_COMMAND=$GIT_SSH_COMMAND"
echo "credential-like env names: $(env | cut -d= -f1 | grep -iE 'token|secret|password|api_key|^github_' | tr '\n' ' ')<end>"
gh auth status 2>&1 | head -3; echo "gh auth status exit=${PIPESTATUS[0]}"
gh api user >/dev/null 2>&1; echo "gh api user exit=$?"
ssh-add -l >/dev/null 2>&1; echo "ssh-add -l exit=$? (2 = no agent)"
echo "origin: $(git remote get-url origin)"
git ls-remote origin >/dev/null 2>&1; echo "ls-remote origin exit=$?"
git push --dry-run "$other" HEAD:refs/heads/m26-probe-should-be-denied 2>&1 | grep -iE 'denied|error|fatal' | head -2
echo "push other repo exit=${PIPESTATUS[0]}"
