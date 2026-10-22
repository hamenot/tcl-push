#!/usr/bin/env bash
# 不依赖 Gradle 的构建脚本：aapt2 编资源 → javac → d8 → zipalign → apksigner。
# 用法：bash build.sh
set -euo pipefail
cd "$(dirname "$0")"

# 需要 ANDROID_SDK（或 ANDROID_HOME / ANDROID_SDK_ROOT）指向 Android SDK，
# 里面要有 build-tools/android-XX 与 platforms/android-34/android.jar。
if [ -n "${ANDROID_SDK:-}" ]; then SDK="$ANDROID_SDK"
elif [ -n "${ANDROID_HOME:-}" ]; then SDK="$ANDROID_HOME"
elif [ -n "${ANDROID_SDK_ROOT:-}" ]; then SDK="$ANDROID_SDK_ROOT"
else SDK="${LOCALAPPDATA:-$HOME}/Android/Sdk"; fi

BT="$(ls -d "$SDK"/build-tools/* 2>/dev/null | sort -V | tail -1)"
ANDROID_JAR="$SDK/platforms/android-34/android.jar"

# JDK：优先 JAVA_HOME，其次 SDK 自带 jdk/*，最后用 PATH 里的
if [ -z "${JAVA_HOME:-}" ] && ls -d "$SDK"/jdk/* >/dev/null 2>&1; then
  JAVA_HOME="$(ls -d "$SDK"/jdk/* | head -1)"
fi
if [ -n "${JAVA_HOME:-}" ]; then
  JAVA="$JAVA_HOME/bin/java"; JAVAC="$JAVA_HOME/bin/javac"; KEYTOOL="$JAVA_HOME/bin/keytool"
else
  JAVA=java; JAVAC=javac; KEYTOOL=keytool
fi
PY="${PY:-python}"

case "$(uname -s 2>/dev/null || echo Windows)" in
  MINGW*|MSYS*|CYGWIN*) EXE=".exe" ;;
  *) EXE="" ;;
esac
AAPT2="$BT/aapt2$EXE"
ZIPALIGN="$BT/zipalign$EXE"
APKSIGNER_JAR="$BT/lib/apksigner.jar"
D8_JAR="$BT/lib/d8.jar"

OUT=build/apk
KS="${KEYSTORE:-keystore/tclpush.jks}"
KS_PASS="${KS_PASS:-tclpush}"
APK_NAME="TCL推送.apk"

d8() { "$JAVA" -Xmx1024M -cp "$D8_JAR" com.android.tools.r8.D8 "$@"; }

for f in "$AAPT2" "$ZIPALIGN" "$ANDROID_JAR" "$APKSIGNER_JAR" "$D8_JAR" "$JAVA" "$JAVAC"; do
  [ -e "$f" ] || { echo "缺少构建组件：$f"; exit 1; }
done

echo "== 清理 =="
rm -rf "$OUT"
mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex" "$OUT/res" dist

echo "== 编译资源 =="
"$AAPT2" compile --dir res -o "$OUT/res/resources.zip"

echo "== 链接资源 =="
"$AAPT2" link \
  -o "$OUT/base.apk" \
  -I "$ANDROID_JAR" \
  --manifest AndroidManifest.xml \
  -R "$OUT/res/resources.zip" \
  --java "$OUT/gen" \
  --auto-add-overlay

echo "== 编译 Java =="
find src "$OUT/gen" -name '*.java' > "$OUT/sources.txt"
"$JAVAC" -encoding UTF-8 -source 8 -target 8 \
  -bootclasspath "$ANDROID_JAR" \
  -d "$OUT/classes" @"$OUT/sources.txt"

echo "== 转 dex =="
find "$OUT/classes" -name '*.class' > "$OUT/classes.txt"
d8 --release --lib "$ANDROID_JAR" --min-api 24 --output "$OUT/dex" @"$OUT/classes.txt"

echo "== 写入 dex =="
"$PY" - "$OUT" <<'PYEOF'
import os
import sys
import zipfile

out = sys.argv[1]
apk = os.path.join(out, "base.apk")
dex = os.path.join(out, "dex", "classes.dex")
with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
print("classes.dex -> %.1f KB" % (os.path.getsize(dex) / 1024.0))
PYEOF

echo "== 对齐 =="
"$ZIPALIGN" -f -p 4 "$OUT/base.apk" "$OUT/aligned.apk"

echo "== 签名 =="
if [ ! -f "$KS" ]; then
  echo "首次构建，生成自签名密钥（口令可用 KS_PASS 覆盖，默认 tclpush）"
  mkdir -p "$(dirname "$KS")"
  "$KEYTOOL" -genkeypair \
    -keystore "$KS" -alias tclpush -keyalg RSA -keysize 2048 -validity 10950 \
    -storepass "$KS_PASS" -keypass "$KS_PASS" \
    -dname "CN=TCLPush,O=Local,C=CN" >/dev/null 2>&1
fi
"$JAVA" -jar "$APKSIGNER_JAR" sign \
  --ks "$KS" --ks-key-alias tclpush \
  --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" \
  --out "dist/$APK_NAME" "$OUT/aligned.apk"

echo "== 校验 =="
"$JAVA" -jar "$APKSIGNER_JAR" verify --print-certs "dist/$APK_NAME" | head -4
"$AAPT2" dump badging "dist/$APK_NAME" | head -3

echo
echo "产物：dist/$APK_NAME  ($(wc -c < "dist/$APK_NAME") 字节)"
