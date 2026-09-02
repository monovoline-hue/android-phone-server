#!/bin/bash
# Build ZONIRA_Monitor_Bridge.apk without Gradle/Android Studio.
# aapt2 (resources+manifest) -> javac -> d8 (dex) -> zip -> zipalign -> apksigner
# All Windows-native tools receive cygpath -w converted paths.
set -e

SDK=/t/android-build/sdk
BT="$SDK/android-14"                    # build-tools 34 unzipped dir
PLAT="$SDK/android-34/android.jar"      # API 34 platform
BR=/t/android-build/bridge
OUT="$BR/build"
DIST="$BR/dist"

JDK=$(ls -d /t/android-build/jdk-ext/jdk-17*/ | head -1)
JDK="${JDK%/}"
export JAVA_HOME="$(cygpath -w "$JDK")"
export PATH="$JDK/bin:$PATH"

W() { cygpath -w "$1"; }

rm -rf "$OUT" "$DIST"
mkdir -p "$OUT/classes" "$OUT/dex" "$OUT/gen" "$DIST"

echo "[1/7] aapt2 compile resources"
"$BT/aapt2.exe" compile --dir "$(W "$BR/res")" -o "$(W "$OUT/res.zip")"

echo "[2/7] aapt2 link (manifest + resources + R.java)"
"$BT/aapt2.exe" link -o "$(W "$OUT/base.apk")" \
    -I "$(W "$PLAT")" \
    --manifest "$(W "$BR/AndroidManifest.xml")" \
    --java "$(W "$OUT/gen")" \
    --auto-add-overlay \
    "$(W "$OUT/res.zip")"

echo "[3/7] javac"
SOURCES=$(ls "$BR/java/com/zonira/monitorbridge/"*.java "$OUT/gen/com/zonira/monitorbridge/R.java" | while read f; do W "$f"; done | tr '\n' ' ')
javac -source 8 -target 8 -nowarn -encoding UTF-8 \
    -classpath "$(W "$PLAT")" \
    -d "$(W "$OUT/classes")" \
    $SOURCES

echo "[4/7] d8 (dex)"
CLASSES=$(ls "$OUT"/classes/com/zonira/monitorbridge/*.class | while read f; do W "$f"; done | tr '\n' ' ')
java -cp "$(W "$BT/lib/d8.jar")" com.android.tools.r8.D8 --release \
    --lib "$(W "$PLAT")" \
    --output "$(W "$OUT/dex")" \
    $CLASSES

echo "[5/7] pack classes.dex"
/c/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe - "$(W "$OUT/base.apk")" "$(W "$OUT/dex/classes.dex")" <<'PYEOF'
import sys, zipfile
apk, dex = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk, 'a', zipfile.ZIP_DEFLATED) as z:
    z.write(dex, 'classes.dex')
print('classes.dex packed')
PYEOF

echo "[6/7] zipalign"
"$BT/zipalign.exe" -f 4 "$(W "$OUT/base.apk")" "$(W "$OUT/aligned.apk")"

echo "[7/7] apksigner sign"
KS="$BR/debug.keystore"
if [ ! -f "$KS" ]; then
    keytool -genkeypair -keystore "$(W "$KS")" -alias bridge \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -storepass zonira123 -keypass zonira123 \
        -dname "CN=ZONIRA Monitor Bridge, O=zonira, C=CN"
fi
"$BT/apksigner.bat" sign \
    --ks "$(W "$KS")" --ks-pass pass:zonira123 --key-pass pass:zonira123 \
    --out "$(W "$DIST/ZONIRA_Monitor_Bridge.apk")" "$(W "$OUT/aligned.apk")"

"$BT/apksigner.bat" verify --print-certs "$(W "$DIST/ZONIRA_Monitor_Bridge.apk")" | head -6
echo ""
echo "=== DONE ==="
ls -la "$DIST"
sha256sum "$DIST/ZONIRA_Monitor_Bridge.apk"
