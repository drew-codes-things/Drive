#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_ENV="${SITE_ENV:-$REPO/deploy/site.env}"
SITE_NAMES=(PROXMOX CORE_CT PROXY_CT DRIVE_DATA CORE_ADDRESS HOST_PORT PROXY_ADDRESS SITE_HOST SITE_DOMAIN OTHER_SITE_NAMES SIGNED_IN_USER)
SITE_VALUE_PATTERN='^[A-Za-z0-9][A-Za-z0-9._:-]*$'
SITE_PATH_PATTERN='^/[A-Za-z0-9._/-]+$'
SITE_LIST_PATTERN='^[a-z0-9-]+( [a-z0-9-]+)*$'
DEPLOY_FILES=(compose.yaml drive.env ufw-after.rules.drive Caddyfile.drive)
SITE_SIGNIN="${SITE_SIGNIN:-$HOME/Projects/site-signin}"
CORE_SCRIPT=/root/drive-deploy-core.sh
PROXY_SCRIPT=/root/drive-deploy-proxy.sh

usage() {
  cat <<'USAGE'
usage: deploy.sh backup | apply [commit] | check | undo | google | print-core-script | print-proxy-script
       | print-deploy-file <name>

Runs from the laptop and reaches the core container (Docker, data) and the proxy
container (Caddy) through ssh to the Proxmox host and `pct`. The host, both containers,
the data folder, the addresses, the port, the site names and the signed-in user come
from deploy/site.env (copy deploy/site.env.example). NOT RUN YET: written for the move
to the server, see the README.

  backup          core: /opt/drive, /etc/drive, /etc/ufw/after.rules and a consistent copy of
                  drive.db into /root/drive-before-<stamp>.tgz; proxy: the Caddyfile and
                  /srv/signin/drive into /root/drive-before-<stamp>.tgz (both mode 600)
  apply [commit]  core: code from the commit (default HEAD), with the site values filled into
                  deploy/, into /opt/drive, env file kept if present, the data folder, the
                  marked ufw rule that only lets the proxy reach the port,
                  `docker compose up -d --build`, health;
                  proxy: sign-in page and the marked Caddy block, validated before reload
  check           container, health, ufw rule, Caddy answers (303 without a session, the /s/ path
                  reaches the app), the other sites still answer
  undo            remove the Caddy block, stop the container and put the earlier code back behind
                  the ufw rule (the rule is removed only when there is no earlier code to run);
                  files in the data folder are never touched
  google          asks here for the Google OAuth client ID and secret (the secret is not echoed),
                  sends them over ssh to the core container, which backs up /etc/drive/drive.env, puts them
                  in it and restarts drive; the values are never printed
USAGE
}

site_value() {
  sed -n "s/^$1=//p" "$SITE_ENV" | tail -n 1
}

site_pattern() {
  case "$1" in
    DRIVE_DATA) printf '%s' "$SITE_PATH_PATTERN" ;;
    OTHER_SITE_NAMES) printf '%s' "$SITE_LIST_PATTERN" ;;
    *) printf '%s' "$SITE_VALUE_PATTERN" ;;
  esac
}

load_site() {
  local name value pattern
  if [ ! -f "$SITE_ENV" ]; then
    echo "refusing: $SITE_ENV is missing; copy deploy/site.env.example to deploy/site.env and fill it in"
    exit 1
  fi
  for name in "${SITE_NAMES[@]}"; do
    value="${!name:-$(site_value "$name")}"
    pattern="$(site_pattern "$name")"
    if [[ ! "$value" =~ $pattern ]]; then
      echo "refusing: $name in $SITE_ENV must be a plain host name, address, number, path or user name, got '$value'"
      exit 1
    fi
    printf -v "$name" '%s' "$value"
  done
}

fill_in_site_values() {
  sed -e "s|__PROXY_CT__|$PROXY_CT|g" -e "s|__CORE_CT__|$CORE_CT|g" -e "s|__DRIVE_DATA__|$DRIVE_DATA|g" \
    -e "s|__CORE_ADDRESS__|$CORE_ADDRESS|g" -e "s|__HOST_PORT__|$HOST_PORT|g" -e "s|__PROXY_ADDRESS__|$PROXY_ADDRESS|g" \
    -e "s|__SITE_HOST__|$SITE_HOST|g" -e "s|__SITE_DOMAIN__|$SITE_DOMAIN|g" \
    -e "s|__OTHER_SITE_NAMES__|$OTHER_SITE_NAMES|g" -e "s|__SIGNED_IN_USER__|$SIGNED_IN_USER|g" "$@"
}

print_deploy_file() {
  local name="$1" known
  for known in "${DEPLOY_FILES[@]}"; do
    if [ "$name" = "$known" ]; then
      fill_in_site_values "$REPO/deploy/$name"
      return
    fi
  done
  echo "refusing: $name is not one of ${DEPLOY_FILES[*]}"
  exit 2
}

write_core_script() {
  fill_in_site_values <<'CORE_SIDE'
#!/usr/bin/env bash
set -euo pipefail
APP="${APP:-/opt/drive}"
CONF="${CONF:-/etc/drive}"
DATA="${DATA:-__DRIVE_DATA__}"
AFTER_RULES="${AFTER_RULES:-/etc/ufw/after.rules}"
STATE="${STATE:-/root}"
UFW_LOCK="${UFW_LOCK:-/root/ufw-edit.lock}"
HOST_PORT=__HOST_PORT__
PROXY_ADDRESS=__PROXY_ADDRESS__
CONTAINER_USER="${CONTAINER_USER:-65532}"
RULE_BEGIN="# >>> drive rule, added and removed by the drive deploy.sh"
RULE_END="# <<< drive rule"
CHAIN_RETURN="-A DOCKER-USER -j RETURN"
BEFORE_POINTER="$STATE/drive-code-before"
BACKUP_POINTER="$STATE/drive-last-backup"

rule_state() {
  local begins
  begins="$(grep -cxF -- "$RULE_BEGIN" "$AFTER_RULES" || true)"
  case "$begins" in 0) echo none ;; 1) echo one ;; *) echo damaged ;; esac
}

after_rules_with_rule() {
  local rule_file="$1"
  awk -v begin="$RULE_BEGIN" -v end="$RULE_END" -v anchor="$CHAIN_RETURN" -v rules="$rule_file" '
    $0 == anchor && !done { print begin; while ((getline line < rules) > 0) print line; print end; done = 1 }
    { print }
    END { if (!done) exit 3 }
  ' "$AFTER_RULES"
}

after_rules_without_rule() {
  awk -v begin="$RULE_BEGIN" -v end="$RULE_END" '
    $0 == begin { inside = 1; next }
    inside { if ($0 == end) inside = 0; next }
    { print }
  ' "$AFTER_RULES"
}

take_ufw_lock() {
  for _ in $(seq 1 60); do
    if mkdir "$UFW_LOCK" 2>/dev/null; then return 0; fi
    sleep 2
  done
  echo "refusing: $UFW_LOCK has been held for 2 minutes by another edit"
  exit 1
}

ufw_is_active() {
  command -v ufw > /dev/null 2>&1 && ufw status 2>/dev/null | head -n 1 | grep -qx "Status: active"
}

reload_ufw() {
  ufw_is_active || { echo "ufw: not installed or not active, so the rule would do nothing"; return 1; }
  ufw reload > /dev/null
}

live_rule_is_loaded() {
  iptables -S DOCKER-USER 2>/dev/null | awk -v port="--ctorigdstport $HOST_PORT " -v source="! -s $PROXY_ADDRESS/32 " '
    index($0, port) && index($0, source) && / -j DROP$/ { found = 1 }
    END { exit !found }
  '
}

replace_rules_and_reload() {
  local next="$1" label="$2" saved
  saved="$AFTER_RULES.bak-drive-$label-$(date +%Y%m%d-%H%M%S)"
  cp -a "$AFTER_RULES" "$saved"
  cat "$next" > "$AFTER_RULES"
  rm -f "$next"
  if reload_ufw; then return 0; fi
  cat "$saved" > "$AFTER_RULES"
  if ufw_is_active; then ufw reload > /dev/null || echo "ufw: reloading the earlier rules failed too; check ufw by hand"; fi
  echo "ufw: reload failed, so $AFTER_RULES is back as it was (copy in $saved)"
  return 1
}

add_rule() {
  local rule_file="$1" next
  case "$(rule_state)" in
    one) echo "ufw: the drive rule is already in $AFTER_RULES"; return 0 ;;
    damaged) echo "ufw: the drive markers in $AFTER_RULES are damaged; fix them by hand"; return 1 ;;
  esac
  take_ufw_lock
  next="$(mktemp)"
  if ! after_rules_with_rule "$rule_file" > "$next"; then
    rm -f "$next"; rmdir "$UFW_LOCK"
    echo "ufw: no '$CHAIN_RETURN' line in $AFTER_RULES, so the rule has nowhere to go; nothing changed"
    return 1
  fi
  if ! replace_rules_and_reload "$next" add; then rmdir "$UFW_LOCK"; return 1; fi
  rmdir "$UFW_LOCK"
  echo "ufw: port $HOST_PORT now only answers CT __PROXY_CT__"
}

remove_rule() {
  local next
  [ "$(rule_state)" = one ] || { echo "ufw: no single drive rule to remove"; return 0; }
  take_ufw_lock
  next="$(mktemp)"
  after_rules_without_rule > "$next"
  if ! replace_rules_and_reload "$next" undo; then rmdir "$UFW_LOCK"; return 1; fi
  rmdir "$UFW_LOCK"
  echo "ufw: drive rule removed"
}

container_health() {
  docker exec drive python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).read().decode())"
}

wait_for_health() {
  for _ in $(seq 1 40); do
    if container_health > /dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "drive did not answer /api/health within 40 s"
  return 1
}

copy_database() {
  python3 -I - "$DATA/drive.db" "$1" <<'PY'
import sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
copy = sqlite3.connect(sys.argv[2])
source.backup(copy)
copy.close()
source.close()
PY
}

do_backup() {
  local stamp backup work parts=()
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="$STATE/drive-before-$stamp.tgz"
  work="$(mktemp -d)"
  if [ -f "$DATA/drive.db" ]; then copy_database "$work/drive.db"; fi
  for path in opt/drive etc/drive etc/ufw/after.rules; do
    if [ -e "/$path" ]; then parts+=("$path"); fi
  done
  ( umask 077 && tar -czf "$backup" -C / "${parts[@]}" -C "$work" . )
  rm -rf "$work"
  echo "$backup" > "$BACKUP_POINTER"
  echo "backup: $backup ($(du -h "$backup" | cut -f1))"
}

ensure_proxy_secret() {
  if grep -qE '^PROXY_SECRET=[0-9a-f]{64}$' "$CONF/drive.env"; then return 0; fi
  if grep -q '^PROXY_SECRET=' "$CONF/drive.env"; then
    echo "refusing: $CONF/drive.env has a PROXY_SECRET line that is not 64 hex characters; fix it by hand"
    return 1
  fi
  printf 'PROXY_SECRET=%s\n' "$(python3 -I -c 'import secrets; print(secrets.token_hex(32))')" >> "$CONF/drive.env"
  echo "env: added a PROXY_SECRET so only Caddy can act as the owner"
}

print_proxy_secret() {
  grep -E '^PROXY_SECRET=[0-9a-f]{64}$' "$CONF/drive.env" | cut -d= -f2
}

set_google_client() {
  local client_id client_secret next saved
  IFS= read -r client_id || true
  IFS= read -r client_secret || true
  if ! [[ "$client_id" =~ ^[A-Za-z0-9._-]{1,200}\.apps\.googleusercontent\.com$ ]]; then
    echo "refusing: the client ID must end in .apps.googleusercontent.com; nothing changed"
    return 1
  fi
  if ! [[ "$client_secret" =~ ^[A-Za-z0-9_-]{8,200}$ ]]; then
    echo "refusing: the client secret is not the letters, digits, - and _ Google gives; nothing changed"
    return 1
  fi
  [ -f "$CONF/drive.env" ] || { echo "refusing: no $CONF/drive.env yet, run apply first"; return 1; }
  require_rule || return 1
  saved="$CONF/drive.env.bak-drive-google-$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF/drive.env" "$saved"
  next="$(mktemp "$CONF/.drive.env.XXXXXX")"
  grep -vE '^GOOGLE_CLIENT_(ID|SECRET)=' "$CONF/drive.env" > "$next" || true
  printf 'GOOGLE_CLIENT_ID=%s\nGOOGLE_CLIENT_SECRET=%s\n' "$client_id" "$client_secret" >> "$next"
  chmod --reference="$CONF/drive.env" "$next"
  chown --reference="$CONF/drive.env" "$next"
  mv "$next" "$CONF/drive.env"
  echo "env: Google client saved in $CONF/drive.env (earlier file $saved)"
  docker compose -f "$APP/deploy/compose.yaml" up -d --force-recreate
  wait_for_health
  echo "drive restarted with Google sign-in set up"
}

require_rule() {
  if [ "$(rule_state)" != one ] || ! live_rule_is_loaded; then
    echo "refusing to start drive: the ufw rule for port $HOST_PORT is not in $AFTER_RULES and loaded in DOCKER-USER"
    return 1
  fi
}

start_container() {
  require_rule || return 1
  docker compose -f "$APP/deploy/compose.yaml" up -d --build
}

port_is_taken() {
  ss -ltn "sport = :$HOST_PORT" | grep -q LISTEN && ! docker ps --format '{{.Names}}' | grep -qx drive
}

do_apply() {
  local bundle="$1" stamp fresh
  [ -s "$BACKUP_POINTER" ] || { echo "refusing: run backup first"; exit 1; }
  if port_is_taken; then echo "refusing: something else listens on port $HOST_PORT"; exit 1; fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  fresh="$APP-new-$stamp"
  mkdir "$fresh"
  tar -xzf "$bundle" -C "$fresh" --no-same-owner
  rm -f "$bundle"
  if [ -d "$APP" ]; then mv "$APP" "$APP-before-$stamp"; echo "$APP-before-$stamp" > "$BEFORE_POINTER"; else echo none > "$BEFORE_POINTER"; fi
  mv "$fresh" "$APP"
  install -d -m 750 "$CONF"
  if [ ! -f "$CONF/drive.env" ]; then install -m 640 "$APP/deploy/drive.env" "$CONF/drive.env"; echo "env: wrote $CONF/drive.env"; fi
  install -d -m 700 -o "$CONTAINER_USER" -g "$CONTAINER_USER" "$DATA"
  ensure_proxy_secret
  add_rule "$APP/deploy/ufw-after.rules.drive"
  start_container
  wait_for_health
  do_check
}

do_check() {
  local failures=0
  echo "container: $(docker inspect -f '{{.State.Status}}' drive 2>/dev/null || echo missing)"
  echo "health: $(container_health 2>&1 || true)"
  echo "ufw: $(ufw_is_active && echo active || echo 'missing or inactive')"
  echo "ufw rule in $AFTER_RULES: $(rule_state)"
  echo "DOCKER-USER rule loaded: $(live_rule_is_loaded && echo yes || echo no)"
  echo "data: $(stat -c '%a %u:%g' "$DATA")"
  echo "shares: $(grep -h '^SHARES_ENABLED=' "$CONF/drive.env" || echo 'SHARES_ENABLED unset (off)')"
  container_health > /dev/null 2>&1 || failures=$((failures + 1))
  ufw_is_active || failures=$((failures + 1))
  [ "$(rule_state)" = one ] || failures=$((failures + 1))
  live_rule_is_loaded || failures=$((failures + 1))
  [ "$(stat -c '%a %u:%g' "$DATA")" = "700 $CONTAINER_USER:$CONTAINER_USER" ] || failures=$((failures + 1))
  echo "core check failures: $failures"
  [ "$failures" = 0 ]
}

do_undo() {
  local before
  before="$(cat "$BEFORE_POINTER" 2>/dev/null || true)"
  docker compose -f "$APP/deploy/compose.yaml" down || true
  if [ -n "$before" ] && [ "$before" != none ] && [ -d "$before" ]; then
    mv "$APP" "$APP-undone-$(date +%Y%m%d-%H%M%S)"
    mv "$before" "$APP"
    start_container
    wait_for_health
    echo "earlier code back and running behind the ufw rule"
  else
    remove_rule
    echo "container stopped; code left in $APP; data in $DATA untouched"
  fi
  rm -f "$BEFORE_POINTER"
}

case "${1:-}" in
  backup) do_backup ;;
  apply) do_apply "${2:?apply needs the code bundle}" ;;
  check) do_check ;;
  undo) do_undo ;;
  add-rule) add_rule "${2:?add-rule needs the rule file}" ;;
  remove-rule) remove_rule ;;
  ensure-proxy-secret) ensure_proxy_secret ;;
  proxy-secret) print_proxy_secret ;;
  google-set) set_google_client ;;
  *) echo "unknown core action"; exit 2 ;;
esac
CORE_SIDE
}

write_proxy_script() {
  fill_in_site_values <<'PROXY_SIDE'
#!/usr/bin/env bash
set -euo pipefail
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
SIGNIN="${SIGNIN:-/srv/signin/drive}"
STATE="${STATE:-/root}"
CADDY_LOCK="${CADDY_LOCK:-/root/caddy-edit.lock}"
UPSTREAM="${UPSTREAM:-__CORE_ADDRESS__:__HOST_PORT__}"
SECRET_FILE="${SECRET_FILE:-/etc/caddy/drive-proxy-secret}"
SITE_HOST=__SITE_HOST__
BLOCK_BEGIN="# >>> drive block, added and removed by the drive deploy.sh"
BLOCK_END="# <<< drive block"
SHARE_PROBE="/s/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/info"

block_state() {
  local begins
  begins="$(grep -cxF -- "$BLOCK_BEGIN" "$CADDYFILE" || true)"
  case "$begins" in 0) echo none ;; 1) echo one ;; *) echo damaged ;; esac
}

proxy_secret() {
  local value
  value="$(cat "$SECRET_FILE" 2>/dev/null || true)"
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s' "$value"
}

save_proxy_secret() {
  local value
  IFS= read -r value || true
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || { echo "refusing: the proxy secret from CT __CORE_CT__ is not 64 hex characters"; return 1; }
  ( umask 077 && printf '%s\n' "$value" > "$SECRET_FILE" )
  echo "caddy: proxy secret saved in $SECRET_FILE"
}

render_block() {
  local template="$1" secret
  secret="$(proxy_secret)" || { echo "refusing: no proxy secret in $SECRET_FILE" >&2; return 1; }
  printf '%s\n' "$BLOCK_BEGIN"
  printf '%s\n' "$template" | sed -e "s/__UPSTREAM__/$UPSTREAM/g" -e "s/__PROXY_SECRET__/$secret/g"
  printf '%s\n' "$BLOCK_END"
}

caddyfile_without_block() {
  awk -v begin="$BLOCK_BEGIN" -v end="$BLOCK_END" '
    inside { if ($0 == end) inside = 0; next }
    $0 == begin { inside = 1; blank_held = 0; next }
    blank_held { print ""; blank_held = 0 }
    $0 == "" { blank_held = 1; next }
    { print }
    END { if (blank_held) print "" }
  ' "$CADDYFILE"
}

take_caddy_lock() {
  for _ in $(seq 1 60); do
    if mkdir "$CADDY_LOCK" 2>/dev/null; then return 0; fi
    sleep 2
  done
  echo "refusing: $CADDY_LOCK has been held for 2 minutes by another edit"
  exit 1
}

switch_caddyfile() {
  local next="$1" backup
  backup="$CADDYFILE.bak-drive-$(date +%Y%m%d-%H%M%S)"
  cp -a "$CADDYFILE" "$backup"
  cat "$next" > "$CADDYFILE"
  if ! caddy validate --config "$CADDYFILE" --adapter caddyfile > /dev/null 2>&1; then
    cat "$backup" > "$CADDYFILE"
    echo "caddy: the new Caddyfile did not validate, put back from $backup"
    return 1
  fi
  if ! systemctl reload caddy; then
    cat "$backup" > "$CADDYFILE"
    echo "caddy: reload failed, Caddyfile put back from $backup"
    return 1
  fi
  echo "caddy: reloaded (earlier file $backup)"
}

add_site() {
  local source="$1" template next status=0
  template="$(cat "$source")" || { echo "refusing: cannot read the Caddy block $source"; return 1; }
  rm -f "$source"
  case "$(block_state)" in
    one) echo "caddy: the drive block is already there"; return 0 ;;
    damaged) echo "caddy: the drive markers are damaged; fix them by hand"; return 1 ;;
  esac
  if grep -qF "http://$SITE_HOST {" "$CADDYFILE"; then echo "caddy: an unmarked $SITE_HOST block exists; left as is"; return 1; fi
  proxy_secret > /dev/null || { echo "refusing: no proxy secret in $SECRET_FILE, so the block cannot be filled in"; return 1; }
  take_caddy_lock
  next="$(mktemp)"
  if ! { cat "$CADDYFILE"; printf '\n'; render_block "$template"; } > "$next"; then
    rm -f "$next"; rmdir "$CADDY_LOCK"; return 1
  fi
  switch_caddyfile "$next" || status=$?
  rm -f "$next"
  rmdir "$CADDY_LOCK"
  return "$status"
}

remove_site() {
  local next status=0
  [ "$(block_state)" = one ] || { echo "caddy: no single drive block to remove"; return 0; }
  take_caddy_lock
  next="$(mktemp)"
  caddyfile_without_block > "$next"
  switch_caddyfile "$next" || status=$?
  rm -f "$next"
  rmdir "$CADDY_LOCK"
  return "$status"
}

answer() {
  curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $1" "${@:2}"
}

do_check() {
  local failures=0 plain dummy share traversal direct forged
  direct="$(curl -s -m 5 "http://$UPSTREAM/api/health" || true)"
  forged="$(curl -s -m 5 -o /dev/null -w '%{http_code}' -H 'X-Forwarded-User: __SIGNED_IN_USER__' "http://$UPSTREAM/api/session" || true)"
  plain="$(answer "$SITE_HOST" http://127.0.0.1/)"
  dummy="$(answer "$SITE_HOST" -H 'Cookie: starfire_session=not-a-session' http://127.0.0.1/api/folders/0)"
  share="$(answer "$SITE_HOST" "http://127.0.0.1$SHARE_PROBE")"
  traversal="$(answer "$SITE_HOST" --path-as-is "http://127.0.0.1/s/../api/folders/0")"
  echo "app from CT __PROXY_CT__: $direct"
  echo "owner header without the proxy secret -> $forged (401 expected)"
  echo "no session -> $plain; made-up session on the API -> $dummy"
  echo "share path -> $share (404 from the app expected: links are off or the token is unknown)"
  echo "/s/../api/folders/0 -> $traversal (must not be 200)"
  case "$direct" in *'"ok":true'*) ;; *) failures=$((failures + 1)) ;; esac
  [ "$forged" = 401 ] || failures=$((failures + 1))
  case "$plain" in "303 "*"/signin/") ;; *) failures=$((failures + 1)) ;; esac
  case "$dummy" in "303 "*"/signin/") ;; *) failures=$((failures + 1)) ;; esac
  case "$share" in "404 "*) ;; *) failures=$((failures + 1)) ;; esac
  case "$traversal" in "200 "*) failures=$((failures + 1)) ;; esac
  for other in __OTHER_SITE_NAMES__; do
    echo "$other -> $(answer "$other.__SITE_DOMAIN__" http://127.0.0.1/)"
  done
  echo "mail -> $(answer mail.__SITE_DOMAIN__ http://127.0.0.1/)"
  echo "starfire login -> $(curl -sk -o /dev/null -w '%{http_code}' https://__PROXY_ADDRESS__/login.html)"
  echo "proxy check failures: $failures"
  [ "$failures" = 0 ]
}

do_backup() {
  local backup parts=(etc/caddy/Caddyfile)
  backup="$STATE/drive-before-$(date +%Y%m%d-%H%M%S).tgz"
  if [ -d "$SIGNIN" ]; then parts+=("${SIGNIN#/}"); fi
  ( umask 077 && tar -czf "$backup" -C / "${parts[@]}" )
  echo "backup: $backup"
}

install_signin() {
  local bundle="$1"
  rm -rf "$SIGNIN-new"
  mkdir -p "$SIGNIN-new"
  tar -xzf "$bundle" -C "$SIGNIN-new" --no-same-owner
  rm -f "$bundle"
  chmod -R u=rwX,go=rX "$SIGNIN-new"
  if [ -d "$SIGNIN" ]; then rm -rf "$SIGNIN-old"; mv "$SIGNIN" "$SIGNIN-old"; fi
  mv "$SIGNIN-new" "$SIGNIN"
}

case "${1:-}" in
  backup) do_backup ;;
  apply) install_signin "${2:?apply needs the sign-in bundle}"; add_site "${3:?apply needs the Caddy block}"; do_check ;;
  add-site) add_site "${2:?add-site needs the Caddy block}" ;;
  remove-site|undo) remove_site ;;
  save-proxy-secret) save_proxy_secret ;;
  check) do_check ;;
  *) echo "unknown proxy action"; exit 2 ;;
esac
PROXY_SIDE
}

push_to() {
  local ct="$1" source="$2" target="$3" mode="$4" name
  name="$(basename "$target")"
  scp -q "$source" "$PROXMOX:/root/$name"
  ssh -o BatchMode=yes "$PROXMOX" "pct push $ct /root/$name $target --perms $mode && rm /root/$name"
}

push_scripts() {
  local work
  work="$(mktemp -d)"
  write_core_script > "$work/core.sh"
  write_proxy_script > "$work/proxy.sh"
  push_to "$CORE_CT" "$work/core.sh" "$CORE_SCRIPT" 700
  push_to "$PROXY_CT" "$work/proxy.sh" "$PROXY_SCRIPT" 700
  rm -rf "$work"
}

in_core() {
  ssh -o BatchMode=yes "$PROXMOX" "pct exec $CORE_CT -- bash $CORE_SCRIPT $*"
}

in_proxy() {
  ssh -o BatchMode=yes "$PROXMOX" "pct exec $PROXY_CT -- bash $PROXY_SCRIPT $*"
}

build_signin_bundle() {
  local work="$1"
  [ -f "$SITE_SIGNIN/build.py" ] || { echo "refusing: no site-signin builder at $SITE_SIGNIN (set SITE_SIGNIN)"; exit 1; }
  python3 "$SITE_SIGNIN/build.py" --first Starfire --second Drive --tagline "Sign in to reach your files." \
    --icon "$REPO/drive/static/icon.svg" --out "$work/signin"
  tar -czf "$work/signin.tgz" -C "$work/signin" .
}

build_code_bundle() {
  local commit="$1" work="$2" name filled=()
  git -C "$REPO" archive --format=tar -o "$work/code.tar" "$commit"
  mkdir -p "$work/filled/deploy"
  for name in "${DEPLOY_FILES[@]}"; do
    git -C "$REPO" show "$commit:deploy/$name" | fill_in_site_values > "$work/filled/deploy/$name"
    filled+=("deploy/$name")
  done
  tar --delete -f "$work/code.tar" "${filled[@]}"
  tar -rf "$work/code.tar" -C "$work/filled" --owner=0 --group=0 --numeric-owner --mode=0664 "${filled[@]}"
  gzip -n < "$work/code.tar" > "$work/code.tgz"
  rm -f "$work/code.tar"
}

deploy() {
  local commit="$1" work
  work="$(mktemp -d)"
  build_code_bundle "$commit" "$work"
  build_signin_bundle "$work"
  push_to "$CORE_CT" "$work/code.tgz" /root/drive-code.tgz 600
  push_to "$PROXY_CT" "$work/signin.tgz" /root/drive-signin.tgz 600
  push_to "$PROXY_CT" "$work/filled/deploy/Caddyfile.drive" /root/drive-Caddyfile.block 600
  rm -rf "$work"
  in_core apply /root/drive-code.tgz
  in_core proxy-secret | in_proxy save-proxy-secret
  in_proxy apply /root/drive-signin.tgz /root/drive-Caddyfile.block
}

send_google_client() {
  local client_id client_secret
  [ -t 0 ] || { echo "refusing: run this in a terminal so the secret can be typed without echo"; exit 1; }
  IFS= read -r -p "Google OAuth client ID: " client_id
  IFS= read -r -s -p "Google OAuth client secret (not shown): " client_secret
  echo
  push_scripts
  printf '%s\n%s\n' "$client_id" "$client_secret" | in_core google-set
}

main() {
  case "${1:-}" in
    -h|--help|help|"") ;;
    *) load_site ;;
  esac
  case "${1:-}" in
    backup) push_scripts; in_core backup; in_proxy backup ;;
    apply) push_scripts; deploy "${2:-HEAD}" ;;
    check) push_scripts; in_core check; in_proxy check ;;
    undo) push_scripts; in_proxy undo; in_core undo ;;
    google) send_google_client ;;
    print-core-script) write_core_script ;;
    print-proxy-script) write_proxy_script ;;
    print-deploy-file) print_deploy_file "${2:?print-deploy-file needs a file name}" ;;
    -h|--help|help) usage ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
