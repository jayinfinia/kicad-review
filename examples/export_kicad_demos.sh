#!/usr/bin/env bash
# Export netlists from the open-source demo projects that ship with KiCad 7
# (Ubuntu/Debian: sudo apt install kicad kicad-demos). Output: examples/netlists/*.net
set -euo pipefail
DEMOS=${KICAD_DEMOS:-/usr/share/kicad/demos}
OUT=$(dirname "$0")/netlists
TMP=$(mktemp -d)
mkdir -p "$OUT"
for p in "stickhub/StickHub" "pic_programmer/pic_programmer" "kit-dev-coldfire-xilinx_5213/kit-dev-coldfire-xilinx_5213" \
         "interf_u/interf_u" "video/video" "sonde xilinx/sonde xilinx" "complex_hierarchy/complex_hierarchy" "ecc83/ecc83-pp"; do
  dir=$(dirname "$p"); name=$(basename "$p")
  cp -r "$DEMOS/$dir" "$TMP/"                       # kicad-cli wants a writable project dir
  kicad-cli sch export netlist -o "$OUT/${name// /_}.net" "$TMP/$dir/$name.kicad_sch" >/dev/null
  echo "exported $OUT/${name// /_}.net"
done
rm -rf "$TMP"
