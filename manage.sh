#!/usr/bin/env bash
# Tests, runs, and deploys this bot. Run ./manage.sh without arguments to see the commands.
set -euo pipefail
cd "$(dirname "$0")"

BOT="site_watcher"
PI="raspi@raspi"
SSH_OPTIONS=(-o BatchMode=yes -o ConnectTimeout=10)

die() { echo "Error: $*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: ./manage.sh <command>

  test            Run the offline tests.
  token local     Save the token of your test bot on this computer.
  token pi        Save the token of your real bot on the Pi.
  local           Run the bot on this computer with the test token.
  deploy          Test, push to GitHub, and update the bot on the Pi.
  status          Show whether the bot is running on the Pi.
  logs            Show the bot's recent logs from the Pi.
  restart         Restart the bot on the Pi.
  stop            Stop the bot on the Pi.
EOF
}

on_pi() {
  ssh "${SSH_OPTIONS[@]}" "$PI" "export XDG_RUNTIME_DIR=/run/user/\$(id -u); $1"
}

prepare_venv() {
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
}

run_tests() {
  prepare_venv
  PYTHONPATH=. .venv/bin/python -B tests/test_bot.py
}

save_token() {
  local target="${1:-}"
  [ "$target" = "local" ] || [ "$target" = "pi" ] || die "Use: ./manage.sh token local, or ./manage.sh token pi"
  [ -t 0 ] || die "Run this command in your own terminal, so that the token stays private."

  local token
  printf 'Paste the bot token (the text stays hidden): ' >&2
  IFS= read -rs token
  echo >&2
  [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || die "That does not look like a bot token."

  if [ "$target" = "local" ]; then
    (umask 077 && printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token" > local.env)
  else
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token" | ssh "${SSH_OPTIONS[@]}" "$PI" "mkdir -p ~/projects && umask 077 && cat > ~/projects/$BOT.env"
  fi
  echo "Saved the token for: $target"
}

run_local() {
  [ -f local.env ] || die "Run ./manage.sh token local first."
  prepare_venv
  set -a
  # shellcheck source=/dev/null
  . ./local.env
  set +a
  echo "The bot is running. It prints nothing unless a problem occurs. Press Ctrl+C to stop it."
  exec .venv/bin/python -m "$BOT.telegram_bot"
}

deploy() {
  [ -z "$(git status --porcelain)" ] || die "You have uncommitted changes. Commit them first."
  git rev-parse --verify --quiet HEAD >/dev/null || die "Make a first commit before you deploy."
  git remote get-url origin >/dev/null 2>&1 || die "Add a GitHub repository first: git remote add origin <url>"

  run_tests
  git push --quiet -u origin main
  local repo
  repo="$(git remote get-url origin | sed -E 's#^git@github\.com:#https://github.com/#')"

  ssh "${SSH_OPTIONS[@]}" "$PI" bash -s -- "$BOT" "$repo" <<'REMOTE'
set -euo pipefail
bot="$1"
repo="$2"
dir="$HOME/projects/$bot"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export GIT_TERMINAL_PROMPT=0

if [ ! -f "$HOME/projects/$bot.env" ]; then
  echo "Error: the Pi has no token for this bot. Run ./manage.sh token pi in your own terminal." >&2
  exit 1
fi

[ -d "$dir/.git" ] || git clone --quiet "$repo" "$dir"
cd "$dir"
git pull --quiet --ff-only
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

mkdir -p "$HOME/.config/systemd/user"
cp "$bot.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --quiet "$bot"
systemctl --user restart "$bot"
sleep 5
systemctl --user status "$bot" --no-pager -n 10
REMOTE
}

case "${1:-}" in
  test) run_tests ;;
  token) save_token "${2:-}" ;;
  local) run_local ;;
  deploy) deploy ;;
  status) on_pi "systemctl --user status $BOT --no-pager -n 10" ;;
  logs) on_pi "journalctl --user-unit $BOT.service -n 100 --no-pager" ;;
  restart) on_pi "systemctl --user restart $BOT && sleep 5 && systemctl --user status $BOT --no-pager -n 10" ;;
  stop) on_pi "systemctl --user stop $BOT" ;;
  *) usage ;;
esac
