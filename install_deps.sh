#!/bin/bash
# Install chromium deps manually for sandboxed environments (E2B, etc.) where apt-get may not work
# Downloads Debian trixie packages for chromium headless
set -e
DEBS_DIR="/tmp/debs"
OUT_DIR="$DEBS_DIR/out"
mkdir -p "$DEBS_DIR" "$OUT_DIR/usr/lib/x86_64-linux-gnu"

cd "$DEBS_DIR"

# List of packages needed for chromium
PKGS=(
  libnspr4
  libnss3
  libatk1.0-0t64
  libatk-bridge2.0-0t64
  libatspi2.0-0t64
  libasound2t64
  libcups2t64
  libdrm2
  libxdamage1
  libxkbcommon0
  libxcomposite1
  libxrandr2
  libgbm1
  libpango-1.0-0
  libgtk-3-0t64
  libxfixes3
  libxext6
  libx11-6
  libavahi-common3
  libavahi-client3
  libglib2.0-0t64
  libgdk-pixbuf-2.0-0
  libcairo2
  libpangoft2-1.0-0
  libharfbuzz0b
  libgraphene-1.0-0
)

echo "Downloading packages..."
for pkg in "${PKGS[@]}"; do
  fname=$(curl -sL http://ftp.debian.org/debian/dists/trixie/main/binary-amd64/Packages.gz | gunzip | awk -v pkg="$pkg" '$0=="Package: "pkg {p=1} p && /^Filename:/{print $2; exit}')
  if [ -n "$fname" ]; then
    echo "$pkg -> $fname"
    curl -sL -o $(basename $fname) http://ftp.debian.org/debian/$fname || echo "Failed $pkg"
  else
    echo "$pkg NOT FOUND"
  fi
done

echo "Extracting..."
for deb in *.deb; do
  [ -f "$deb" ] || continue
  echo "Extract $deb"
  ar x "$deb" 2>/dev/null
  tar -xf data.tar.* -C "$OUT_DIR" 2>/dev/null
  rm -f control.tar.* data.tar.* debian-binary
done

echo "Done. Libs in $OUT_DIR/usr/lib/x86_64-linux-gnu"
echo "To use: export LD_LIBRARY_PATH=$OUT_DIR/usr/lib/x86_64-linux-gnu:\$LD_LIBRARY_PATH"
echo "Test: LD_LIBRARY_PATH=$OUT_DIR/usr/lib/x86_64-linux-gnu:\$LD_LIBRARY_PATH chromium --version"
