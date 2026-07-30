#!/usr/bin/env bash
# Runs ON the droplet. Idempotent: safe to re-run to update.
#
#   sudo bash install.sh <SCRAPECREATORS_KEY> <TG_TOKEN> <TG_CHAT> [ANTHROPIC_API_KEY]
#
# Installs the watcher as two systemd units:
#   marketplace-listen.service  persistent, answers Telegram replies instantly
#   marketplace-watch.timer     hourly search + alerts
set -euo pipefail

REPO="https://github.com/Reflex08/marketplace-watch.git"
DIR="/opt/marketplace-watch"
# State is kept OUT of the checkout: this script does `git reset --hard` on redeploy,
# which would otherwise replace live seen.json / rules.json with the committed copies.
STATE_DIR="/var/lib/marketplace-watch"
ENVFILE="/etc/marketplace-watch.env"
USER_NAME="mwatch"

[ "$#" -ge 3 ] || { echo "need at least 3 args: scrapekey tgtoken tgchat [anthropickey]"; exit 2; }

echo "== packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null

echo "== service user =="
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$USER_NAME"

echo "== code =="
if [ -d "$DIR/.git" ]; then
    git -C "$DIR" fetch --quiet origin main
    git -C "$DIR" reset --hard --quiet origin/main
else
    rm -rf "$DIR"
    git clone --quiet "$REPO" "$DIR"
fi

echo "== state dir =="
mkdir -p "$STATE_DIR"
# Seed from the repo's copies on first install only, so an existing droplet keeps its
# history and its learned rules across redeploys.
for f in seen.json pending.json rules.json state.json; do
    [ -e "$STATE_DIR/$f" ] || cp "$DIR/$f" "$STATE_DIR/$f" 2>/dev/null || echo '{}' > "$STATE_DIR/$f"
done

echo "== venv =="
[ -x "$DIR/.venv/bin/python" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip requests

echo "== self-test =="
"$DIR/.venv/bin/python" "$DIR/watch.py" --selftest

echo "== secrets =="
# Kept out of the repo, which is public. Root-only, service user reads it via systemd.
umask 077
cat > "$ENVFILE" <<EOF
SCRAPECREATORS_KEY=$1
TG_TOKEN=$2
TG_CHAT=$3
ANTHROPIC_API_KEY=${4:-}
STATE_DIR=$STATE_DIR
EOF
chmod 600 "$ENVFILE"

# The droplet is the single source of truth once it runs; nothing is committed back.
chown -R "$USER_NAME":"$USER_NAME" "$DIR" "$STATE_DIR"

echo "== systemd =="
install -m 644 "$DIR/deploy/marketplace-listen.service" /etc/systemd/system/
install -m 644 "$DIR/deploy/marketplace-watch.service" /etc/systemd/system/
install -m 644 "$DIR/deploy/marketplace-watch.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now marketplace-watch.timer
if [ -n "${4:-}" ]; then
    systemctl enable --now marketplace-listen.service
else
    echo "no Anthropic key given: reply-tuning disabled, not starting the listener"
    systemctl disable --now marketplace-listen.service 2>/dev/null || true
fi

echo
echo "== status =="
systemctl --no-pager status marketplace-watch.timer  | head -4 || true
systemctl --no-pager status marketplace-listen.service | head -4 || true
echo
echo "next search:"; systemctl list-timers marketplace-watch.timer --no-pager | head -3
echo
echo "logs:  journalctl -u marketplace-watch -u marketplace-listen -f"
echo "config: $DIR/.venv/bin/python $DIR/watch.py --show"
