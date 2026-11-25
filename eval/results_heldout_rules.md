## Results: mode=rules, min severity=warning

| Bug type | Variants | Detected | Recall | Unrelated new findings |
|---|---|---|---|---|
| remove_decoupling | 6 | 6 | 100% | 0 |
| float_input | 6 | 6 | 100% | 0 |
| float_power_pin | 6 | 6 | 100% | 0 |
| supply_to_ground | 6 | 6 | 100% | 0 |
| rail_typo | 6 | 6 | 100% | 0 |
| remove_pullup | 6 | 6 | 100% | 0 |
| pullup_to_ground | 6 | 6 | 100% | 0 |
| pullup_wrong_rail | 5 | 5 | 100% | 0 |
| cap_undervoltage | 6 | 6 | 100% | 0 |
| short_led_resistor | 6 | 6 | 100% | 0 |
| remove_crystal_caps | 6 | 6 | 100% | 0 |
| **all** | **65** | **65** | **100%** | **0** |

1 variant(s) excluded after hand review as not real defects (eval/label_audit.json). Raw score including them: 65/66 (98%).

Precision of new findings (point at the seeded bug): 100% (76 of 76)

Findings on the unmodified designs (not scored; may be real issues or false positives): Ottercast_OtterCastAudioV2_OtterCastAudioV2 6, badgeteam_mch2022-badge-hardware_mch2022 6, cardonabits_haxo-hw_haxophone001 2, hackrf_marzipan 20, hackrf_neapolitan 9, hackrf_one 17, hackrf_operacake 2, scottbez1_splitflap_sensor_smd 2, skot_bitaxe_bitaxeUltra 14, stack-chan_stack-chan_m5-pantilt 0
