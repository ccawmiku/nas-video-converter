#!/bin/sh
set -eu

PUID="${PUID:-1024}"
PGID="${PGID:-100}"

case "$PUID:$PGID" in
  *[!0-9:]*|:*|*:) echo "PUID 和 PGID 必须是正整数" >&2; exit 64 ;;
esac

if getent group "$PGID" >/dev/null 2>&1; then
  NVC_GROUP="$(getent group "$PGID" | cut -d: -f1)"
else
  NVC_GROUP="nvc"
  groupadd --gid "$PGID" "$NVC_GROUP"
fi

if getent passwd "$PUID" >/dev/null 2>&1; then
  NVC_USER="$(getent passwd "$PUID" | cut -d: -f1)"
else
  NVC_USER="nvc"
  useradd --uid "$PUID" --gid "$PGID" --no-create-home --home-dir /config --shell /usr/sbin/nologin "$NVC_USER"
fi

mkdir -p /config
chown "$PUID:$PGID" /config
exec gosu "$PUID:$PGID" "$@"

