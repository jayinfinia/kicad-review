# Design review: demo_board

13 components (3 ICs), 13 nets. 🔴 5 error · 🟠 0 warning · 🔵 0 info

_mode: rules_

## Findings

| # | Sev | Category | Source | Finding | Refs | Nets |
|---|---|---|---|---|---|---|
| 1 | 🔴 error | connectivity | rules:floating-input | U3 (BME280) input pin(s) left floating (no net, no no-connect flag): U3.2/CSB | U3 |  |
| 2 | 🔴 error | connectivity | rules:unconnected-power-pin | U2 (ATmega328P-AU) power pin(s) not connected: U2.18/AVCC | U2 |  |
| 3 | 🔴 error | decoupling | rules:missing-decoupling | No capacitor from +5V to ground; IC(s) U1 have no decoupling on this rail | U1 | +5V |
| 4 | 🔴 error | pullup | rules:pullup-wrong-rail | R2 pulls /SDA up to +5V (5 V) but U2.27/PC4/SDA is powered from 3.3 V; pin may be overdriven unless it is tolerant | R2, U2 | /SDA, +5V |
| 5 | 🔴 error | pullup | rules:pullup-wrong-rail | R3 pulls /SCL up to +5V (5 V) but U2.28/PC5/SCL is powered from 3.3 V; pin may be overdriven unless it is tolerant | R3, U2 | /SCL, +5V |

---
_Netlist-level review: placement, trace length and decoupling-cap proximity are not checked._
