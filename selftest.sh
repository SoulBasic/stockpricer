#!/usr/bin/env bash
# Self-test the stock quote server. Usage: ./selftest.sh [port]
PORT="${1:-8849}"
B="http://127.0.0.1:${PORT}"
pass=0; fail=0
say(){ printf '%s\n' "$*"; }

pp(){ python3 -c "import sys,json
d=json.load(sys.stdin)
r=d.get('resolved') or {}
print(f\"  {str(d.get('query') or d.get('symbol','')):12s} -> {d.get('market','?')}:{str(d.get('symbol','')):9s} {(d.get('name') or '')[:16]:16s} price={d.get('price')} {d.get('currency','')} state={d.get('marketState')} src={d.get('source','')[:14]}\")"; }

geturl(){ curl -s -G "$B/quote" --data-urlencode "q=$1"; }

check(){ # check "label" expected_http "url"
  code=$(curl -s -o /tmp/_b -w '%{http_code}' "$3")
  if [ "$code" = "$2" ]; then pass=$((pass+1)); printf 'OK   %-34s [%s]\n' "$1" "$code";
  else fail=$((fail+1)); printf 'FAIL %-34s [got %s want %s]\n' "$1" "$code" "$2"; fi; }

# expect a fuzzy query to resolve to a given market:code
expect(){ # expect "query" "market:code"
  got=$(geturl "$1" | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"{d.get('market','?').lower()}:{d.get('code','')}\")" 2>/dev/null)
  norm=$(printf '%s' "$got" | sed 's/:0*/:/')        # ignore zero-padding
  want=$(printf '%s' "$2"  | sed 's/:0*/:/')
  if [ "$norm" = "$want" ]; then pass=$((pass+1)); printf 'OK   %-12s -> %-12s\n' "$1" "$got";
  else fail=$((fail+1)); printf 'FAIL %-12s -> got %-12s want %s\n' "$1" "$got" "$2"; fi; }

say "=== status codes ==="
check "health"          200 "$B/health"
check "root help"       200 "$B/"
check "valid US"        200 "$B/quote?symbol=AAPL"
check "fuzzy CN name"   200 "$B/quote?q=%E8%85%BE%E8%AE%AF"   # 腾讯
check "search endpoint" 200 "$B/search?q=%E8%85%BE%E8%AE%AF"
check "garbage -> 404"  404 "$B/quote?q=zzzqqqxxxnope"
check "missing -> 400"  400 "$B/quote"

say ""; say "=== fuzzy resolution -> PRIMARY listing ==="
expect 腾讯      hk:00700
expect tencent  hk:00700
expect 700      hk:00700
expect 阿里巴巴   hk:09988
expect alibaba  hk:09988
expect 京东      hk:09618
expect 网易      hk:09999
expect 小米      hk:01810
expect 茅台      cn:600519
expect moutai   cn:600519
expect 平安银行   cn:000001
expect 苹果      us:AAPL
expect apple    us:AAPL
expect 特斯拉     us:TSLA
expect 拼多多     us:PDD

say ""; say "=== quotes across markets ==="
for q in AAPL 0700.HK 600519 腾讯 阿里巴巴 茅台 苹果; do geturl "$q" | pp; done

say ""; say "===== $pass passed, $fail failed ====="
exit $fail
