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

RS485 Modbus RTU energy meters — share the same RS485 bus as the temp/humidity sensors,
read directly by the Linux side. No wiring back to the Arduino MCU required.

| Qty | Component | Specifications | Notes |
|-----|-----------|---------------|-------|
| 2 | **Eastron SDM120-Modbus** (with split-core CT clamp) | Measures: voltage (V), current (A), active power (W), apparent power (VA), power factor, frequency (Hz), import energy (kWh). RS485 Modbus RTU. 80–270VAC input (works for 120V US circuits). DIN-rail mount (1 module wide). ~$25–35 each. | Get the **CT clamp variant** (SDM120CT) for non-invasive installation — no wire cutting needed. Ships with default Modbus address 0x01 — reprogram to **0x03** (AC system) and **0x04** (dehumidifier) via front panel before installation. Available from Amazon, AliExpress, automation suppliers. |

**CT clamp placement:**
- SDM120 #1 (addr 0x03): CT clamp around the **live wire** feeding the AC compressor/air-handler circuit
- SDM120 #2 (addr 0x04): CT clamp around the **live wire** feeding the dehumidifier
- Clamp around **live wire only** — never around both live and neutral together

---

## RS485 Bus Interface

All sensors and power monitors are read by the Linux side (QRB2210) via a USB-RS485
adapter plugged into the QRB2210 USB port. The FT232RL chip in the Waveshare adapter
is supported by the Linux kernel's built-in `ftdi_sio` driver — no manual driver
installation needed. The device appears as `/dev/ttyUSB0` when plugged in.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **Waveshare USB to RS485/422 Industrial Grade Isolated Converter** (FT232RL + SP485EEN) | DIN-rail mountable. Galvanic isolation protects the Linux side from HVAC bus noise. FT232RL is natively supported on Debian Linux — plug-and-play as `/dev/ttyUSB0`. Screw terminals for A/B/GND. Search: *"Waveshare USB to RS485 isolated FT232RL"*. ~$15–20. |
| 1 | **120Ω resistor, 1/4W** | RS485 bus termination — solder across A and B terminals at the outdoor sensor (far end of the bus) |

---

## Input Signal Conditioning

24VAC thermostat signals directly energize relay coils. The relay contacts connect
the Arduino 3.3V pin to MCU input pins — providing galvanic isolation from 24VAC
and safe 3.3V logic levels. No AC-DC conversion module required.

**Important:** The contact side of the input relays must use the Arduino **3.3V pin**,
not 5V. The STM32U585 input pins are 3.3V maximum — feeding 5V will damage the MCU.

| Qty | Component | Notes |
|-----|-----------|-------|
| 9 | **DIN-rail relay module, 24VAC coil, SPDT** | One per thermostat/humidity input (D2–D10). Coil driven directly by 24VAC thermostat signal (between call wire and C/common). Contact side: one terminal to Arduino 3.3V pin, other terminal to MCU input pin. ~$4–10 each. Search: *"DIN rail relay module 24VAC coil"*. Finder 34.51, Phoenix Contact PLC-RSC- 24AC/21, or equivalent. |

---

## Output Relay Board

Drives all 8 HVAC control outputs. MCU pins drive the relay coils via optocouplers;
relay contacts switch 24VAC to the HVAC equipment.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **8-channel 5V relay module, optocoupler-isolated** | Must have a **VCC/VDD jumper** separating relay coil power from logic input power. Wire: logic input VCC → Arduino **3.3V pin**; relay coil VCC → Arduino **5V pin**. Relay contacts rated for at least 10A/250VAC. ~$5–8. The Arduino 5V pin (supplied from the onboard regulator when powered via VIN) provides 5V coil power without a separate supply. |

---

## Power Supplies

A single 12V DIN-rail PSU is the only external DC supply needed. The Arduino's
onboard regulator provides 5V (relay coils) and 3.3V (logic + input relay contacts)
from the 12V VIN input. The 24VAC input relay coils are powered directly from the
existing HVAC thermostat wiring — no AC-DC conversion module required.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **12VDC DIN-rail PSU, 2A** | Powers: Uno Q VIN pin and RS485 temp/humidity sensors. The Arduino's onboard regulator derives 5V (for output relay coils) and 3.3V (for logic and input relay contacts) from this 12V input. Search: *"Mean Well HDR-30-12"* or *"Mean Well DR-30-12"* (~$15–20). **Note:** Monitor for instability if many output relays are active simultaneously — the 5V rail is shared with the Linux processor. If brownouts occur, add a separate 5V supply for the relay board coil VCC. |

---

## Wiring + Mounting

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **DIN rail enclosure / panel enclosure** | At minimum **300×250×100mm** to fit: Uno Q + shield, 8-channel output relay board, 9× DIN-rail input relay modules, 2× SDM120 meters, 12V PSU, Waveshare RS485 adapter, and terminal blocks. Keep mains-voltage wiring (relay contacts, SDM120 L/N terminals) physically separated from low-voltage logic wiring. |
| 1 roll | **Shielded cable, 4-conductor, 22 AWG** | RS485 sensor bus wiring. Carries both RS485 signal (A/B on a twisted pair) and 12V sensor power (VCC/GND on the remaining two conductors) in a single run. Daisy-chain from USB-RS485 adapter → indoor sensor → outdoor sensor. Shield drain wire connected to GND at the adapter end only. Search: *"4 conductor shielded cable 22 AWG"* or *"Belden 9504"*. |
| 1 roll | **Multi-conductor control wire, 22 AWG** | Thermostat signal runs (R, C, Y, W per thermostat) to input relay coils in enclosure. 4-conductor minimum per thermostat run. |
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
| RS485 (via USB-RS485 adapter) | Modbus RTU | QRB2210 USB port | 2× SDM120 power monitors + 2× SHT30 temp/hum | 1200m (well within 4m run) |
| 24VAC relay contacts | Active-high (3.3V) | MCU D2–D10 | 9 thermostat/humidity inputs | Relay contact to MCU pin |
| Digital (relay) | Active-low logic | MCU D11–D18 | 8 HVAC control outputs | Relay contacts to load |

---

## Where to Buy

- **Arduino Uno Q:** [store-usa.arduino.cc](https://store-usa.arduino.cc) or authorized distributors (Mouser, Digi-Key, Arrow)
- **Eastron SDM120-Modbus:** Amazon (search "Eastron SDM120 Modbus"), AliExpress, or automation suppliers (e.g. Allied Electronics, AutomationDirect)
- **RS485 SHT30 transmitters:** Amazon, AliExpress (search "RS485 Modbus SHT30 temperature humidity transmitter"), DFRobot (product-2279)
- **Waveshare USB to RS485 adapter:** Amazon (search "Waveshare USB RS485 isolated FT232RL"), Waveshare official store
- **DIN-rail relay modules (24VAC coil):** Digi-Key, Mouser, AutomationDirect (search "Finder 34.51 24VAC" or "Phoenix Contact PLC-RSC-24AC")
- **Output relay board, power modules:** Amazon, AliExpress, Digi-Key
