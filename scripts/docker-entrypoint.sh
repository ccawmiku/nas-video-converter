#!/bin/sh
set -eu

PUID="${PUID:-1026}"
PGID="${PGID:-100}"
QSV_DEVICE="${QSV_DEVICE:-/dev/dri/renderD128}"

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

if [ -e "$QSV_DEVICE" ]; then
  QSV_GID="${QSV_GID:-$(stat -c '%g' "$QSV_DEVICE")}"
  case "$QSV_GID" in
    *[!0-9]*|'') echo "QSV_GID 必须是非负整数" >&2; exit 64 ;;
  esac
  if [ "$QSV_GID" != "0" ]; then
    if getent group "$QSV_GID" >/dev/null 2>&1; then
      QSV_GROUP="$(getent group "$QSV_GID" | cut -d: -f1)"
    else
      QSV_GROUP="nvc-qsv"
      groupadd --gid "$QSV_GID" "$QSV_GROUP"
    fi
    usermod -aG "$QSV_GROUP" "$NVC_USER"
  fi
fi

mkdir -p /config
chown -R "$PUID:$PGID" /config
if ! gosu "$NVC_USER:$PGID" test -w /config; then
  echo "/config 对 PUID=$PUID PGID=$PGID 不可写；请检查群晖挂载路径、rw 模式和 ACL" >&2
  exit 73
fi
exec gosu "$NVC_USER:$PGID" "$@"
