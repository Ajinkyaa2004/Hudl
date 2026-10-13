# PuckTrace Doorway Check: a DSM scheduled task (user root, EVERY MINUTE).
# Checks that the NAS can be reached from the internet through Tailscale Funnel. When the server
# answers at home but not from outside (the Funnel stopped passing traffic: 12 October 15:58-19:40,
# 13 October 16:00 and 16:27-17:08), it restarts Tailscale: at once, then again every 10 minutes
# while it stays broken. It never restarts anything until it has seen the outside check succeed
# once, and never while the home internet itself is down.
# Log: /volume2/docker/pucktrace/doorway.log (public IP changes and Tailscale's view are noted too).
H=home-nas.tailfff30a.ts.net
TS=/var/packages/Tailscale/target/bin/tailscale
DIR=/volume2/docker/pucktrace
LOG=$DIR/doorway.log
LOCK=$DIR/.doorway-lock
now() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "$(now) $*" >> "$LOG"; }
# one run at a time (a run can take a minute or two while it repairs)
find "$LOCK" -maxdepth 0 -mmin +10 -exec rmdir {} \; 2>/dev/null
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
inside() { curl -s -m 10 http://127.0.0.1:8000/api/healthz | grep -q '"ok":true'; }
outside() {
  curl -s -o /dev/null -m 15 --doh-url https://1.1.1.1/dns-query "https://$H/api/healthz" -w '%{http_code}' 2>/dev/null | grep -q 200 && return 0
  ip=$(nslookup "$H" 1.1.1.1 2>/dev/null | sed -n 's/^Address[ 0-9]*: *\([0-9.]*\)$/\1/p' | grep -v '^1\.1\.1\.1$' | head -1)
  [ -n "$ip" ] && curl -s -o /dev/null -m 15 --resolve "$H:443:$ip" "https://$H/api/healthz" -w '%{http_code}' | grep -q 200
}
internet() { curl -s -o /dev/null -m 10 https://1.1.1.1 || curl -s -o /dev/null -m 10 https://www.google.com; }
netview() {
  pub=$($TS netcheck 2>/dev/null | sed -n 's/.*IPv4: yes, \([0-9.]*\):.*/\1/p' | head -1)
  st=$($TS status 2>&1 | head -1 | cut -c1-100)
  echo "public IP ${pub:-unknown}; tailscale: $st"
}
note_ip() {
  pub=$($TS netcheck 2>/dev/null | sed -n 's/.*IPv4: yes, \([0-9.]*\):.*/\1/p' | head -1)
  [ -z "$pub" ] && return
  old=$(cat "$DIR/.doorway-ip" 2>/dev/null)
  [ "$pub" != "$old" ] && { [ -n "$old" ] && log "home public IP changed: $old -> $pub"; echo "$pub" > "$DIR/.doorway-ip"; }
}
tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
inside || exit 0                                   # server starting or stopped: nothing to judge
if outside || { sleep 5; outside; }; then
  [ -f "$DIR/.doorway-seen-ok" ] || { touch "$DIR/.doorway-seen-ok"; log "outside OK (first success, the check works from the NAS)"; }
  if [ -f "$DIR/.doorway-down" ]; then log "reachable from outside again (down since $(cat "$DIR/.doorway-down"))"; rm -f "$DIR/.doorway-down"; fi
  [ "$(date +%M)" = "00" ] && { log "outside OK"; note_ip; }
  exit 0
fi
[ -f "$DIR/.doorway-seen-ok" ] || { log "outside check failed, but it has never worked from the NAS: not acting"; exit 0; }
if ! internet; then log "home internet is down: nothing to repair on the NAS"; exit 0; fi
[ -f "$DIR/.doorway-down" ] || { now > "$DIR/.doorway-down"; log "NOT reachable from outside; $(netview)"; note_ip; }
if [ -f "$DIR/.doorway-last-fix" ] && [ $(( $(date +%s) - $(cat "$DIR/.doorway-last-fix") )) -lt 600 ]; then exit 0; fi
log "restarting Tailscale"
date +%s > "$DIR/.doorway-last-fix"
$TS cert "$H" >/dev/null 2>&1
synopkg restart Tailscale >/dev/null 2>&1
sleep 30
$TS funnel --bg 8000 >/dev/null 2>&1
sleep 15
if outside; then log "fixed: reachable from outside again"; rm -f "$DIR/.doorway-down"; else log "still not reachable after the restart; $(netview)"; fi
