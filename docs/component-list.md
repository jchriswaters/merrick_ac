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
| 1 | **Arduino Uno Q** | Main controller — QRB2210 Linux MPU + STM32U585 MCU on one board. Get the 4GB RAM / 32GB eMMC variant for comfortable Linux headroom. For permanent installation, powered via the VIN pin (accepts 7–24V DC) from the 12V DIN-rail supply (see Power Supplies section). |
| 1 | **USB-C cable (data-capable)** | For initial setup, programming, and bench development only. Must be a full data cable — charge-only cables will not connect. Standard 5V 3A supply is sufficient; Power Delivery is not required. Not used for permanent power in the enclosure. |

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

The RS485 temp/humidity sensors are read by the Linux side (QRB2210) via a USB-RS485
adapter — not by the MCU. The MAX3485 module is not used.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **USB-to-RS485 adapter** | Plugs into one of the QRB2210's USB ports. Search: *"USB RS485 adapter CH340"* or *"USB RS485 converter"*. ~$3–8. Choose one with screw terminals for A/B wires. The CH340 or FTDI chipset variants both work on Linux. |
| 1 | **120Ω resistor, 1/4W** | RS485 bus termination — solder across A and B terminals at the far end of the sensor cable (outdoor sensor end) |

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

A single 12V DIN-rail PSU powers everything in the enclosure. A small buck converter
derives the 5V needed for relay coils and PZEM logic. The HVAC 24VAC transformer
powers the optocoupler input side separately.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **12VDC DIN-rail PSU, 2A** | Main supply for the whole enclosure. Powers: Uno Q VIN pin, RS485 temp/humidity sensors, and buck converter input. Search: *"Mean Well HDR-30-12"* or *"Mean Well DR-30-12"* (~$15–20). DIN-rail mount keeps wiring clean. 2A provides comfortable headroom for the Uno Q running Linux (~1A peak) plus sensors. |
| 1 | **12V→5V DC-DC buck converter module** | Derives 5V from the 12V main supply. Powers: relay board coil VCC, PZEM optocoupler logic VCC. Search: *"LM2596 buck converter module"* or *"MP1584 buck converter"* (~$2–4). Set output to 5.0V with a multimeter before connecting. |

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
| 1 | **Mini UPS / battery backup, 12V** | HVAC controllers should survive short power interruptions without resetting. A DC UPS module that accepts 12V input and provides 12V output on battery backup protects the Uno Q and preserves the Linux filesystem cleanly. Search: *"12V DC UPS module lithium"*. |
| 1 | **Ferrite choke** (for RS485 cable entry) | Reduces EMI pickup from nearby relay switching and motor loads |

---

## Summary: Communication Buses

| Bus | Protocol | Connected to | Devices on Bus | Max cable length |
|-----|----------|--------------|----------------|-----------------|
| RS485 (via USB-RS485 adapter) | Modbus RTU | QRB2210 USB port | 2× SHT30 temp/hum transmitters | 1200m (well within 4m run) |
| UART direct | Modbus RTU | MCU Serial1 (D0/D1) | 2× PZEM-004T power monitors | ~5m practical (UART level) |
| Digital (opto) | Active-low logic | MCU D2–D10 | 9 thermostat/humidity inputs | Determined by opto board |
| Digital (relay) | Active-low logic | MCU D11–D18 | 8 HVAC control outputs | Relay contacts to load |

---

## Where to Buy

- **Arduino Uno Q:** [store-usa.arduino.cc](https://store-usa.arduino.cc) or authorized distributors (Mouser, Digi-Key, Arrow)
- **PZEM-004T V3.0:** Amazon (search "PZEM-004T V3 split core"), AliExpress, or eBay
- **RS485 SHT30 transmitters:** Amazon, AliExpress (search "RS485 Modbus SHT30 temperature humidity transmitter"), DFRobot (product-2279)
- **MAX3485 module:** Amazon (search "MAX3485 3.3V RS485 module"), AliExpress
- **Opto boards, relay boards, power modules:** Amazon, AliExpress, Digi-Key
