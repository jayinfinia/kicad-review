#!/usr/bin/env bash
# Build the held-out set: ten designs from open-source hardware projects on GitHub, pinned to the commits
# used for the README numbers. Needs git and KiCad 7 (kicad-cli). Output: examples/heldout_netlists/*.net
# The projects carry their own licenses (MIT, CERN-OHL-P/S, Apache-2.0, GPL-2.0, ...), so the netlists are
# not committed here.
set -euo pipefail
OUT=$(cd "$(dirname "$0")" && pwd)/heldout_netlists
SRC=${HELDOUT_SRC:-$(mktemp -d)}
mkdir -p "$OUT"
fetch() {  # repo commit dir
  [ -d "$SRC/$3" ] || git clone -q "https://github.com/$1.git" "$SRC/$3"
  git -C "$SRC/$3" checkout -q "$2"
}
sch() {    # dir schematic-path output-name
  kicad-cli sch export netlist -o "$OUT/$3.net" "$SRC/$1/$2" >/dev/null && echo "exported $3"
}
fetch Ottercast/OtterCastAudioV2 199395d ottercast
fetch badgeteam/mch2022-badge-hardware 9ca74d1 mch2022
fetch cardonabits/haxo-hw 83e9a15 haxo
fetch greatscottgadgets/hackrf 6166455 hackrf
fetch scottbez1/splitflap 87b17c5 splitflap
fetch skot/bitaxe 942bbd9 bitaxe
fetch stack-chan/stack-chan 48202f6 stackchan
sch ottercast OtterCastAudioV2.kicad_sch Ottercast_OtterCastAudioV2_OtterCastAudioV2
sch mch2022 mch2022.kicad_sch badgeteam_mch2022-badge-hardware_mch2022
sch haxo haxophone001.kicad_sch cardonabits_haxo-hw_haxophone001
sch hackrf hardware/hackrf-one/hackrf-one.kicad_sch hackrf_one
sch splitflap electronics/sensor_smd/sensor_smd.kicad_sch scottbez1_splitflap_sensor_smd
sch bitaxe bitaxeUltra.kicad_sch skot_bitaxe_bitaxeUltra
sch stackchan schematics/m5-pantilt/m5-pantilt.kicad_sch stack-chan_stack-chan_m5-pantilt
for b in marzipan neapolitan operacake; do   # KiCad 5 netlists committed in the HackRF repo
  cp "$SRC/hackrf/hardware/$b/$b.net" "$OUT/hackrf_$b.net" && echo "copied hackrf_$b"
done
