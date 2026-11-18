#!/usr/bin/env bash
# End-to-end test of the Pulse and Launchpad starter kits with Podman (or Docker).
# Usage:
#   bash test-kits.sh                                   (kit folders next to this script)
#   bash test-kits.sh <pulse-kit-folder> <launchpad-kit-folder>
# Works with the Bash 3.2 that ships with macOS.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
find_kit() { # name [given path] -> prints the folder that contains docker-compose.yml
  for d in "${2:-}" "${2:-}/$1" "$HERE/$1" "$HERE/$1/$1"; do
    [ -n "$d" ] && [ -f "$d/docker-compose.yml" ] && { (cd "$d" && pwd); return 0; }
  done; return 1; }
PULSE_DIR=$(find_kit pulse-starter-kit "${1:-}") || { echo "Can't find the Pulse kit. Pass its folder as the first argument."; exit 1; }
LP_DIR=$(find_kit launchpad-starter-kit "${2:-}") || { echo "Can't find the Launchpad kit. Pass its folder as the second argument."; exit 1; }
echo "Pulse kit:     $PULSE_DIR"
echo "Launchpad kit: $LP_DIR"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  [PASS] $1"; }
bad() { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }
code() { curl -s -o /dev/null -w "%{http_code}" --max-time "${2:-10}" "$1"; }

# ---------------------------------------------------------------- engine
if command -v podman >/dev/null 2>&1; then ENGINE=podman; TLS="--tls-verify=false"
elif command -v docker >/dev/null 2>&1; then ENGINE=docker; TLS=""
else echo "Neither podman nor docker found"; exit 1; fi
if $ENGINE compose version >/dev/null 2>&1; then COMPOSE="$ENGINE compose"
elif command -v podman-compose >/dev/null 2>&1; then COMPOSE="podman-compose"
else echo "No compose found. Install podman-compose (brew install podman-compose) or docker-compose."; exit 1; fi
echo "Engine: $ENGINE ($($ENGINE --version))"
echo "Compose: $COMPOSE"

wait_for() { # url expected_code seconds
  for _ in $(seq 1 "$3"); do [ "$(code "$1" 3)" = "$2" ] && return 0; sleep 1; done; return 1; }

# ================================================================ PULSE
echo; echo "=== Pulse starter kit"
(cd "$PULSE_DIR" && $COMPOSE up -d --build >/tmp/pulse-up.log 2>&1) && ok "compose up" || { bad "compose up (see /tmp/pulse-up.log)"; }
for p in 7001 7002 7003 7004; do wait_for "http://localhost:$p/health" 200 60 && ok "service on $p healthy" || bad "service on $p not healthy"; done
wait_for "http://localhost:${WEBHOOK_PORT:-9900}/api/received" 200 30 && ok "webhook receiver up" || bad "webhook receiver not up"

curl -s -X POST localhost:7003/control/down >/dev/null
[ "$(code localhost:7003/health)" = "503" ] && ok "down mode returns 503" || bad "down mode"
curl -s localhost:7003/health | grep -q "10.12.4.7" && ok "down response includes internal error text (status-page trap)" || bad "internal error text missing"
curl -s -X POST localhost:7003/control/reset >/dev/null
[ "$(code localhost:7003/health)" = "200" ] && ok "reset returns to 200" || bad "reset"

curl -s -X POST "localhost:7002/control/slow?ms=3000" >/dev/null
t=$(curl -s -o /dev/null -w "%{time_total}" --max-time 10 localhost:7002/health)
awk "BEGIN{exit !($t >= 2.9)}" && ok "slow mode delays health by ~3 s (${t}s)" || bad "slow mode (${t}s)"
t2=$(curl -s -o /dev/null -w "%{time_total}" localhost:7001/health)
awk "BEGIN{exit !($t2 < 1)}" && ok "other services unaffected while one is slow (${t2}s)" || bad "other services slowed (${t2}s)"
curl -s -X POST localhost:7002/control/reset >/dev/null

curl -s -X POST "localhost:7001/control/flaky?rate=0.5" >/dev/null
fails=0; for _ in $(seq 1 40); do [ "$(code localhost:7001/health)" = "500" ] && fails=$((fails+1)); done
[ $fails -ge 8 ] && [ $fails -le 32 ] && ok "flaky 0.5 failed $fails of 40" || bad "flaky 0.5 failed $fails of 40"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST 'localhost:7001/control/flaky?rate=2')" = "400" ] && ok "bad control input rejected" || bad "bad control input not rejected"
bash "$PULSE_DIR/scripts/incident-drill.sh" --reset >/dev/null && [ "$(code localhost:7001/health)" = "200" ] && ok "drill --reset works" || bad "drill --reset"

curl -s -X DELETE localhost:${WEBHOOK_PORT:-9900}/api/received >/dev/null
curl -s -X POST localhost:${WEBHOOK_PORT:-9900}/test -H 'Content-Type: application/json' -d '{"service":"claims","event":"incident_opened"}' >/dev/null
curl -s localhost:${WEBHOOK_PORT:-9900}/api/received | grep -q incident_opened && ok "webhook stored a notification" || bad "webhook did not store"
curl -s localhost:${WEBHOOK_PORT:-9900}/ | grep -q incident_opened && ok "webhook page shows it" || bad "webhook page"
(cd "$PULSE_DIR" && $COMPOSE down >/dev/null 2>&1) && ok "compose down"

# ================================================================ LAUNCHPAD
echo; echo "=== Launchpad starter kit"
(cd "$LP_DIR" && $COMPOSE up -d --build >/tmp/lp-up.log 2>&1) && ok "compose up" || bad "compose up (see /tmp/lp-up.log)"
wait_for "http://localhost:5000/v2/" 200 60 && ok "registry up on 5000" || bad "registry not reachable on 5000 (macOS: AirPlay Receiver?)"
wait_for "http://localhost:${WEBHOOK_PORT:-9900}/api/received" 200 30 && ok "webhook receiver up" || bad "webhook receiver not up"

port=8601
for svc in quote-svc policy-svc; do
  repo="$LP_DIR/services/$svc"
  [ "$(git -C "$repo" tag | tr '\n' ' ')" = "v1.0.0 v1.1.0 v1.2.0 " ] && ok "$svc has tags v1.0.0 v1.1.0 v1.2.0" || bad "$svc tags"
  for v in v1.0.0 v1.1.0 v1.2.0; do
    src=$(mktemp -d); git -C "$repo" archive "$v" | tar -x -C "$src"
    img="localhost:5000/$svc:$v"
    if $ENGINE build -q -t "$img" "$src" >/dev/null 2>&1; then ok "$svc $v builds"; else bad "$svc $v build"; rm -rf "$src"; continue; fi
    $ENGINE push $TLS "$img" >/dev/null 2>&1 && ok "$svc $v pushed to registry" || bad "$svc $v push"
    rm -rf "$src"
    port=$((port+1)); c1="kit-test-$svc-$v-a"; c2="kit-test-$svc-$v-b"
    $ENGINE run -d --name "$c1" -p "$port:8080" -e APP_ENV=staging -e DB_PASSWORD=TopSecret123 "$img" >/dev/null
    port2=$((port+100))
    $ENGINE run -d --name "$c2" -p "$port2:8080" -e APP_ENV=staging "$img" >/dev/null
    if [ "$v" = "v1.2.0" ]; then
      sleep 5; h=$(code "localhost:$port/health")
      [ "$h" = "503" ] && ok "$svc $v never becomes healthy (503)" || bad "$svc $v health was $h, expected 503"
    else
      wait_for "http://localhost:$port/health" 200 30 && ok "$svc $v healthy with secret" || bad "$svc $v not healthy with secret"
      wait_for "http://localhost:$port2/health" 500 30 && ok "$svc $v unhealthy without DB_PASSWORD (500)" || bad "$svc $v without secret"
    fi
    curl -s "localhost:$port/version" | grep -q "\"$v\"" && ok "$svc $v reports its version" || bad "$svc $v /version"
    $ENGINE logs "$c1" 2>&1 | grep -q TopSecret123 && bad "$svc $v printed the secret in its logs" || ok "$svc $v keeps the secret out of its logs"
    if [ "$svc" = "quote-svc" ]; then
      prem=$(curl -s "localhost:$port/quote?bike_cc=150&rider_age=30" | sed 's/.*"annual_premium_inr": \([0-9]*\).*/\1/')
      exp=1620; [ "$v" = "v1.0.0" ] && exp=1800
      [ "$prem" = "$exp" ] && ok "$svc $v quote = $prem" || bad "$svc $v quote = $prem, expected $exp"
    else
      s=$(code "localhost:$port/policies?holder=asha"); exp=200; [ "$v" = "v1.0.0" ] && exp=404
      [ "$s" = "$exp" ] && ok "$svc $v holder search returns $s" || bad "$svc $v holder search returned $s, expected $exp"
    fi
    $ENGINE rm -f "$c1" "$c2" >/dev/null 2>&1
  done
done
curl -s localhost:5000/v2/_catalog | grep -q quote-svc && ok "registry catalog lists the pushed images" || bad "registry catalog"
(cd "$LP_DIR" && $COMPOSE down >/dev/null 2>&1) && ok "compose down"

echo; echo "Summary: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && echo "RESULT: BOTH KITS OK" || echo "RESULT: FIX THE FAILURES ABOVE"
