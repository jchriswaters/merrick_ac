# Component List — Uno Q HVAC Controller

All quantities are for a single-controller installation covering:
- 1× Unico mini-duct AC system (heat pump)
- 3× thermostats (main, downstairs, theater)
- 2× powered zone dampers (theater, downstairs)
- 1× dehumidifier
- 1× fresh-air vent actuator
- 2× current/power monitoring circuits
- 2× combined temp/humidity sensors (indoor + outdoor)

---

## Controller Board

| Qty | Component | Description / Notes |
|-----|-----------|---------------------|
| 1 | **Arduino Uno Q** | Main controller — QRB2210 Linux MPU + STM32U585 MCU on one board. Requires USB-C Power Delivery input. Get the 4GB RAM / 32GB eMMC variant for comfortable Linux headroom. |
| 1 | **USB-C PD power supply, 12V 2A min** | Must be USB Power Delivery (PD) compatible. A standard USB-C phone charger at 5V is insufficient. Use a 12V PD adapter or a powered USB-C dock with PD passthrough. |

---

## Temperature + Humidity Sensors

Both indoor and outdoor locations use the **same model** of industrial RS485 transmitter.
Factory calibrated — no user calibration required. Address set by physical DIP switch.

| Qty | Component | Specifications | Notes |
|-----|-----------|---------------|-------|
| 2 | **RS485 Modbus RTU Temp/Humidity Transmitter** (SHT30-based, wall-mount) | SHT30 sensing element. ±0.3°C temp, ±2% RH humidity accuracy. RS485 Modbus RTU output. 5–36V DC supply. IP54 or better. Outputs: temperature, humidity, dew point. | Search: *"RS485 Modbus SHT30 temperature humidity transmitter wall mount"*. ~$15–25 each. Available from Amazon, AliExpress, DFRobot. Verify DIP switch address configuration in product listing. |
| 1 | **IP65 weatherproof enclosure** (small, ~100×68×50mm) | For outdoor sensor if the transmitter body is not already IP65-rated | Polycarbonate or ABS. Drill entry for cable gland. Mount sensor in shaded location away from direct sun and reflected heat. |

**Bus wiring:** Both sensors daisy-chain on the same RS485 twisted-pair cable.
Set indoor unit to Modbus address **0x01** and outdoor unit to **0x02** via DIP
switch before installation.

---

## Power Monitoring

| Qty | Component | Specifications | Notes |
|-----|-----------|---------------|-------|
| 2 | **PZEM-004T V3.0** (with split-core / open CT clamp) | Measures: voltage (V), current (A), power (W), energy (kWh), frequency (Hz), power factor. UART Modbus RTU. 80–260VAC input. Split-core CT clamp (non-invasive — no wire cutting). ~$8–12 each. | Get the **V3.0** version and the **open CT / split-core clamp** variant. The closed-core version requires disconnecting wires. Verify the board has optocouplers on the UART lines; if not, add external isolation. One-time address assignment required (0x01 for AC system, 0x02 for dehumidifier) — see docs. |

**CT clamp placement:**
- PZEM #1 (addr 0x01): CT clamp around the **live wire** feeding the AC compressor/air-handler circuit
- PZEM #2 (addr 0x02): CT clamp around the **live wire** feeding the dehumidifier
- Clamp around **live wire only** — never around both live and neutral together

---

## RS485 Bus Interface

One module bridges the STM32U585's 3.3V UART to the RS485 differential bus.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **MAX3485 TTL↔RS485 transceiver module, 3.3V** | **Must be the 3.3V MAX3485 variant** — the common 5V MAX485 will damage STM32 GPIO pins. Search: *"MAX3485 3.3V RS485 module Arduino"*. ~$1–3. Includes screw terminals for A/B bus wires. |
| 1 | **120Ω resistor, 1/4W** | RS485 bus termination — solder across A and B terminals at the far end of the sensor cable |

---

## Input Signal Conditioning

Converts 24 VAC thermostat signals to 3.3V DC logic for the MCU.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **8-channel PC817 optocoupler isolation board** | Handles 8 of the 9 inputs (D2–D9). ~$3–5. Verify the board's output side can be powered at 3.3V, or use the input pulldown approach described in system-design.md. |
| 1 | **Single-channel PC817 optocoupler module** | 9th input (D10, vent_open_in). Alternatively buy a second 8-channel board for spares. |
| 1 | **24VAC→5VDC power module** (e.g. HLK-5M05 or equivalent small AC-DC) | Powers the input (LED) side of the opto boards from the existing HVAC 24VAC control transformer. Verify your HVAC transformer has enough spare VA capacity (~0.5W needed). |

---

## Output Relay Board

Drives all 8 HVAC control outputs.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **8-channel 5V relay module, optocoupler-isolated** | Must have a **VCC/VDD jumper** separating relay coil power from logic input power. Wire: logic input VCC → 3.3V rail; relay coil VCC → 5V rail. Relay contacts rated for at least 10A/250VAC. ~$5–8. |

---

## Power Supplies

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **5VDC regulated supply, 1A** | Powers: relay board coil side, PZEM optocoupler logic side. Can use a small HLK-5M05 AC-DC module inside the enclosure, or a wall-wart into the enclosure. |
| 1 | **12VDC regulated supply, 500mA** | Powers: both RS485 temp/humidity transmitters (they accept 5–36V; 12V is clean and widely available). Can share with a 12V→5V buck converter if only one supply is desired. |

---

## Wiring + Mounting

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **DIN rail enclosure / panel enclosure** | Mounts all boards cleanly. At minimum 200×200×80mm to fit the Uno Q, relay board, opto boards, and DIN-mounted power supplies. Keep mains-voltage wiring (relay contacts, PZEM terminals) physically separated from low-voltage logic wiring. |
| 1 roll | **Shielded twisted-pair cable, 2-conductor, 22 AWG** (e.g. Belden 9841 or equivalent) | RS485 bus wiring. Daisy-chain from controller → indoor sensor → outdoor sensor. Shield drain wire connected to ground at controller end only. |
| 1 roll | **3-conductor control wire, 22 AWG** | Thermostat signal runs to opto-isolator inputs |
| 1 roll | **2-conductor stranded wire, 18 AWG** | 24VAC and DC power distribution inside enclosure |
| 4 | **Cable glands** (PG7 or M16) | Weatherproof cable entry into enclosure |
| 1 bag | **Ferrule crimp terminals** (0.5mm², 22 AWG) | For clean, reliable connections to screw terminal blocks |
| 1 | **DIN rail terminal block strip** | Organized connection point for all field wiring entering the enclosure |

---

## Optional but Recommended

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **Home Assistant server** (Raspberry Pi 4 / 5, or existing NAS/PC) | Acts as MQTT broker, provides dashboards, historical logging, and automations based on the HVAC status topic. Alternatively use a standalone Mosquitto broker on any Linux machine. |
| 1 | **Mini UPS / battery backup** | HVAC controllers should survive short power interruptions without resetting. A small UPS or DC UPS module (12V, ~2Ah) protects the Uno Q and preserves the Linux filesystem cleanly. |
| 1 | **Ferrite choke** (for RS485 cable entry) | Reduces EMI pickup from nearby relay switching and motor loads |

---

## Summary: Communication Buses

| Bus | Protocol | MCU Port | Devices on Bus | Max cable length |
|-----|----------|----------|----------------|-----------------|
| RS485 (via MAX3485) | Modbus RTU | Serial2 | 2× SHT30 temp/hum transmitters | 1200m (well within 3m run) |
| UART direct | Modbus RTU | Serial1 | 2× PZEM-004T power monitors | ~5m practical (UART level) |
| Digital (opto) | Active-low logic | D2–D10 | 9 thermostat/humidity inputs | Determined by opto board |
| Digital (relay) | Active-low logic | D11–D18 | 8 HVAC control outputs | Relay contacts to load |

---

## Where to Buy

- **Arduino Uno Q:** [store-usa.arduino.cc](https://store-usa.arduino.cc) or authorized distributors (Mouser, Digi-Key, Arrow)
- **PZEM-004T V3.0:** Amazon (search "PZEM-004T V3 split core"), AliExpress, or eBay
- **RS485 SHT30 transmitters:** Amazon, AliExpress (search "RS485 Modbus SHT30 temperature humidity transmitter"), DFRobot (product-2279)
- **MAX3485 module:** Amazon (search "MAX3485 3.3V RS485 module"), AliExpress
- **Opto boards, relay boards, power modules:** Amazon, AliExpress, Digi-Key
