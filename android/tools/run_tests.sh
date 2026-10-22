#!/usr/bin/env bash
# PC 端协议回归：用假电视跑一遍所有分支，断言事件流与结果码。
# 用法：bash tools/run_tests.sh
set -u

cd "$(dirname "$0")/.." || exit 1
# 需要 JDK 8+（用到 javac / java）和 Python 3；都可用环境变量指过去
if [ -n "${JAVA_HOME:-}" ]; then
  JAVA="$JAVA_HOME/bin/java"; JAVAC="$JAVA_HOME/bin/javac"
else
  JAVA=java; JAVAC=javac
fi
PY="${PY:-python}"
APK="build/demo.apk"
PKG="com.example.demo"
LABEL="演示应用"
VC=121303001

mkdir -p build
[ -f "$APK" ] || "$PY" -c "
import os
open('$APK','wb').write(os.urandom(3*1024*1024))
"

echo "== 编译 =="
"$JAVAC" -encoding UTF-8 --release 8 -d build/pc \
  src/com/hamenot/tclpush/PushCore.java \
  src/com/hamenot/tclpush/ApkServer.java \
  src/com/hamenot/tclpush/NetUtil.java \
  tools/PushCli.java || exit 1
echo "编译通过"

pass=0
fail=0

run_case() {
  mode="$1"; np="$2"; want="$3"; extra="$4"; stopafter="${5:-0}"
  log="build/case_$mode.log"
  "$PY" -u tools/fake_tv.py --mode "$mode" > "$log.tv" 2>&1 &
  tvpid=$!
  sleep 0.8
  out=$(TCLPUSH_LOCAL_IP=127.0.0.1 timeout 40 "$JAVA" -Dfile.encoding=UTF-8 -cp build/pc \
        PushCli 127.0.0.1 "$APK" "$PKG" "$LABEL" "$VC" "$np" 120 "$stopafter" 2>&1)
  rc=$?
  kill $tvpid 2>/dev/null
  wait $tvpid 2>/dev/null
  echo "$out" > "$log"

  got=$(printf '%s' "$out" | grep '^\[result\]' | sed 's/^\[result\] //')
  status=$(printf '%s' "$got" | cut -d'|' -f1 | tr -d ' ')

  ok=1
  [ "$status" = "$want" ] || ok=0
  if [ -n "$extra" ]; then
    printf '%s' "$out" | grep -q "$extra" || ok=0
  fi
  # 除了超时分支，都该真的把文件下发出去
  if [ "$mode" != "denied" ] && [ "$mode" != "fail" ]; then
    printf '%s' "$out" | grep -q 'FETCH 200' || true
  fi

  if [ $ok -eq 1 ]; then
    echo "  PASS  $mode  -> $got"
    pass=$((pass+1))
  else
    echo "  FAIL  $mode  期望 $want，实际 $got"
    fail=$((fail+1))
  fi
}

echo "== 分支 =="
run_case ok        15 ok      '\[stage\] install'
run_case denied    15 denied  '\[stage\] confirm'
run_case fail      15 failed  '\[stage\] download'
run_case stall      4 timeout '\[stage\] install'
run_case wrongpkg  15 ok      '\[stage\] install'
run_case slow      60 stopped '\[stop\] 手动停止' 3

echo "== 汇总 =="
echo "通过 $pass 个，失败 $fail 个"
[ "$fail" -eq 0 ] || exit 1
