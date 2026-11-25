# Design review: demo_board

13 components (3 ICs), 13 nets. 🔴 11 error · 🟠 1 warning · 🔵 0 info

_mode: rules_

## Findings

| # | Sev | Category | Source | Finding | Refs | Nets |
|---|---|---|---|---|---|---|
| 1 | 🔴 error | connectivity | rules:floating-input | U3 (BME280) input pin(s) left floating (no net, no no-connect flag): U3.2/CSB | U3 |  |
| 2 | 🔴 error | connectivity | rules:part-pin-unconnected | U2.18/AVCC (ATmega328P-AU): AVCC must be connected to VCC even if the ADC is unused; pin is unconnected | U2 | unconnected-(U2-Pad18) |
| 3 | 🔴 error | connectivity | rules:unconnected-power-pin | U2 (ATmega328P-AU) power pin(s) not connected: U2.18/AVCC | U2 |  |
| 4 | 🔴 error | decoupling | rules:missing-decoupling | No capacitor from +5V to ground; IC(s) U1 have no decoupling on this rail | U1 | +5V |
| 5 | 🔴 error | decoupling | rules:part-missing-cap | U1.1/VIN (AP2112K-3.3): input capacitor for LDO stability; no capacitor to ground on +5V | U1 | +5V |
| 6 | 🔴 error | power | rules:led-no-current-limit | LED D1 sits between GND and /LED with no series resistor or current driver: current is limited only by the driving pin | D1 | GND, /LED |
| 7 | 🔴 error | pullup | rules:bias-overvoltage | U2.27/PC4/SDA idles at 5.00 V via 4.7 kΩ on /SDA, above its 3.8 V limit (U2 I/O rail 3.3 V + 0.5 V (parts DB)) | U2, R2 | /SDA |
| 8 | 🔴 error | pullup | rules:bias-overvoltage | U2.28/PC5/SCL idles at 5.00 V via 4.7 kΩ on /SCL, above its 3.8 V limit (U2 I/O rail 3.3 V + 0.5 V (parts DB)) | U2, R3 | /SCL |
| 9 | 🔴 error | pullup | rules:bias-overvoltage | U3.3/SDI idles at 5.00 V via 4.7 kΩ on /SDA, above its 4.25 V limit (BME280 abs max 4.25 V (parts DB)) | U3, R2 | /SDA |
| 10 | 🔴 error | pullup | rules:bias-overvoltage | U3.4/SCK idles at 5.00 V via 4.7 kΩ on /SCL, above its 4.25 V limit (BME280 abs max 4.25 V (parts DB)) | U3, R3 | /SCL |
| 11 | 🔴 error | pullup | rules:part-strap | U3.2/CSB (BME280): CSB low at power-up latches SPI mode; tie to VDDIO for I2C; pin is floating | U3 | unconnected-(U3-Pad2) |
| 12 | 🟠 warning | other | rules:crystal-no-load-caps | Crystal Y1 (16MHz) has no load capacitors to ground on /XTAL1 or /XTAL2; frequency will be off unless the oscillator has internal load caps | Y1 | /XTAL1, /XTAL2 |

## Notes

2. Microchip ATmega328P datasheet: pins VCC+0.5 V abs max; RESET has internal pull-up
5. Diodes AP2112 datasheet: 1 uF input and output capacitors
7. DC nodal solve of the resistor network
8. DC nodal solve of the resistor network
9. DC nodal solve of the resistor network
10. DC nodal solve of the resistor network
11. Bosch BME280 datasheet, abs max VDDIO / pins 4.25 V

---
_Netlist-level review: placement, trace length and decoupling-cap proximity are not checked._
