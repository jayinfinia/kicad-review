## Results: mode=rules-basic, min severity=warning

| Bug type | Variants | Detected | Recall | Unrelated new findings |
|---|---|---|---|---|
| remove_decoupling | 6 | 6 | 100% | 0 |
| float_input | 6 | 6 | 100% | 0 |
| float_power_pin | 6 | 6 | 100% | 0 |
| supply_to_ground | 6 | 6 | 100% | 0 |
| rail_typo | 6 | 6 | 100% | 0 |
| remove_pullup | 6 | 4 | 67% | 0 |
| pullup_to_ground | 6 | 0 | 0% | 0 |
| pullup_wrong_rail | 6 | 0 | 0% | 0 |
| cap_undervoltage | 6 | 6 | 100% | 0 |
| short_led_resistor | 6 | 0 | 0% | 0 |
| remove_crystal_caps | 5 | 0 | 0% | 0 |
| **all** | **65** | **40** | **62%** | **0** |

Precision of new findings (point at the seeded bug): 100% (47 of 47)

Findings on the unmodified designs (not scored; may be real issues or false positives): StickHub 0, complex_hierarchy 2, ecc83-pp 0, interf_u 1, kit-dev-coldfire-xilinx_5213 1, pic_programmer 4, sonde_xilinx 1, video 2
