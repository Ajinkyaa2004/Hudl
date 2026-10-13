# PuckTrace Doorway Check: a DSM scheduled task (user root, every 5 minutes).
# Checks that the NAS can be reached from the internet through Tailscale Funnel. When the server
# answers at home but not from outside (the Funnel stopped passing traffic, as on 12 October from
# 15:58 to 19:40), it restarts Tailscale, at most once every 30 minutes.
# It never restarts anything until it has seen the outside check succeed once, so a check that
# cannot work from the NAS does no harm. Log: /volume2/docker/pucktrace/doorway.log
H=home-nas.tailfff30a.ts.net
TS=/var/packages/Tailscale/target/bin/tailscale
DIR=/volume2/docker/pucktrace
LOG=$DIR/doorway.log
now() { date '+%Y-%m-%d %H:%M:%S'; }
inside() { curl -s -m 10 http://127.0.0.1:8000/api/healthz | grep -q '"ok":true'; }
outside() {
  curl -s -o /dev/null -m 20 --doh-url https://1.1.1.1/dns-query "https://$H/api/healthz" -w '%{http_code}' 2>/dev/null | grep -q 200 && return 0
  ip=$(nslookup "$H" 1.1.1.1 2>/dev/null | sed -n 's/^Address[ 0-9]*: *\([0-9.]*\)$/\1/p' | grep -v '^1\.1\.1\.1$' | head -1)
  [ -n "$ip" ] && curl -s -o /dev/null -m 20 --resolve "$H:443:$ip" "https://$H/api/healthz" -w '%{http_code}' | grep -q 200
}
tail -n 300 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
if ! inside; then echo "$(now) server not answering at home (starting or stopped); nothing to do" >> "$LOG"; exit 0; fi
for i in 1 2 3; do
  if outside; then
    [ -f "$DIR/.doorway-seen-ok" ] || { touch "$DIR/.doorway-seen-ok"; echo "$(now) outside OK (first success, the check works from the NAS)" >> "$LOG"; }
    [ "$(date +%M)" = "00" ] && echo "$(now) outside OK" >> "$LOG"
    exit 0
  fi
  sleep 20
done
if [ ! -f "$DIR/.doorway-seen-ok" ]; then echo "$(now) outside check failed, but it has never worked from the NAS: not acting" >> "$LOG"; exit 0; fi
if [ -f "$DIR/.doorway-last-fix" ] && [ $(( $(date +%s) - $(cat "$DIR/.doorway-last-fix") )) -lt 1800 ]; then
  echo "$(now) outside still failing; restarted less than 30 min ago, waiting" >> "$LOG"; exit 0
fi
echo "$(now) server answers at home but not from outside: restarting Tailscale" >> "$LOG"
date +%s > "$DIR/.doorway-last-fix"
$TS cert "$H" >/dev/null 2>&1
synopkg restart Tailscale >/dev/null 2>&1
sleep 40
$TS funnel --bg 8000 >/dev/null 2>&1
sleep 20
if outside; then echo "$(now) fixed: reachable from outside again" >> "$LOG"; else echo "$(now) still not reachable after the restart" >> "$LOG"; fi
