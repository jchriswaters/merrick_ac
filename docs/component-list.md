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
| 2 | **RS485 Modbus RTU Temp/Humidity Transmitter** (SHT30-based, wall-mount) | SHT30 sensing element. ±0.3°C temp, ±2% RH humidity accuracy. RS485 Modbus RTU output. 5–36V DC supply. IP54 or better. Outputs: temperature (signed int16 ×100, °C), humidity (uint16 ×100, %RH). | Search: *"RS485 Modbus SHT30 temperature humidity transmitter wall mount"*. ~$15–25 each. Available from Amazon, AliExpress, DFRobot. Verify DIP switch address configuration in product listing. |
| 1 | **IP65 weatherproof enclosure** (small, ~100×68×50mm) | For outdoor sensor if the transmitter body is not already IP65-rated | Polycarbonate or ABS. Drill entry for cable gland. Mount sensor in shaded location away from direct sun and reflected heat. |

**Bus wiring:** Both sensors connect in parallel to the in-enclosure RS485 distribution
terminal block. Set indoor unit to Modbus address **0x01** and outdoor unit to **0x02**
via DIP switch before installation. The outdoor sensor terminates the bus with a 120 Ω
resistor across A+ and B-.

**Wire color convention** (confirmed against this build's modules — always re-verify
against the specific module's datasheet, OEMs occasionally swap colors):

| Wire color | Function                |
|------------|-------------------------|
| Red        | V+ (12 V DC supply)     |
| Green      | V- (DC return / ground) |
| Yellow     | A+ (RS485 data positive)|
| Blue       | B- (RS485 data negative)|

See `docs/system-design.md` §4b–4c for the full wiring diagram.

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
adapter. The FT232RL/FT232RNL/CH340 chip in the adapter is supported by the Linux
kernel's built-in `ftdi_sio` / `ch341` drivers — no manual driver installation needed.
The device appears as `/dev/ttyUSB0` when plugged in.

**Critical:** the Uno Q's USB-C port runs in **power-sink mode** when the board is
powered via VIN (not USB-C). It does not provide VBUS to peripherals. A USB-RS485
dongle plugged directly into the Uno Q **will not enumerate**. A **powered USB hub**
between the Uno Q and the dongle is required to source VBUS. See `docs/deployment.md`
for the diagnostic that uncovered this.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **USB-RS485 adapter** (FTDI FT232RL/RNL or CH340-based) | Either a bare dongle or a Waveshare USB-to-RS485 isolated converter (FT232RL + SP485EEN) works. Galvanic isolation is recommended to protect the Linux side from HVAC bus noise. Appears as `/dev/ttyUSB0`. ~$8–20 depending on isolation. |
| 1 | **Powered USB hub** (USB 2.0+, with its own DC adapter or 5 V input) | Required between the Uno Q USB-C port and the USB-RS485 dongle. The hub's own power supply provides VBUS to both the upstream and downstream USB ports, working around the Uno Q's Type-C sink mode. Any small 4-port USB 2.0 hub with a wall-wart power supply works. Inside the enclosure, power the hub from a 12 V→5 V buck converter ($2–3) tied to the same 12 V rail as the rest of the system — eliminates the need for a separate AC outlet. Hold the hub in place with double-sided industrial tape if a DIN-mount enclosure is not available. ~$8–15. |
| 1 | **12 V to 5 V buck converter module** (optional but recommended) | Mounts on DIN rail with a small bracket or in the enclosure. Powers the USB hub from the same 12 V supply as everything else, avoiding the need for a second wall adapter. LM2596 modules are widely available for ~$2. Pick one rated for ≥ 1 A output (the hub plus dongle plus FTDI together draw well under 500 mA). |
| 1 | **120 Ω resistor, 1/4 W** | RS485 bus termination — wire across the A+ and B- terminals at the outdoor sensor (far end of the bus). |
| 1 | **3-row DIN-rail terminal block strip** (or 4-row if you include a separate GND row) | In-enclosure star point for the RS485 bus. One row for A+, one for B-, one for GND reference. All four field devices and the USB-RS485 adapter land on these rows in parallel. Also use a separate 2-row block for 12 V and GND for SHT30 sensor power. |

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

Drives all 9 HVAC control outputs. MCU pins drive the relay coils via optocouplers;
relay contacts switch 24VAC to the HVAC equipment.

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **8-channel 5V relay module, optocoupler-isolated** | Must have a **VCC/VDD jumper** separating relay coil power from logic input power. Wire: logic input VCC → Arduino **3.3V pin**; relay coil VCC → Arduino **5V pin**. Relay contacts rated for at least 10A/250VAC. ~$5–8. The Arduino 5V pin (supplied from the onboard regulator when powered via VIN) provides 5V coil power without a separate supply. Handles outputs on D11–D18. |
| 1 | **Single-channel 5V relay module, optocoupler-isolated** | Same wiring as above (logic 3.3V, coil 5V). For the fan relay on D19 (Unico G wire). An individual module keeps the channel count clean without needing a 9th channel on a larger board. ~$2–4. Alternatively use a second 8-channel board and leave 7 channels unpopulated. |

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

## Local HMI Display

A standalone touchscreen panel on the same WiFi network. Communicates with the
Uno Q exclusively via MQTT — no wiring into the enclosure required. Used to view
live system state and adjust all configurable setpoints (temperature thresholds,
humidity limits, vent schedule, zone enable/disable).

| Qty | Component | Notes |
|-----|-----------|-------|
| 1 | **Elecrow CrowPanel 2.1" ESP32 Rotary Display** (EoW02CSCM3A or equivalent) | 2.1-inch round IPS display, 480×480, capacitive touch + rotary knob. ESP32-S3R8 processor (240 MHz dual-core, 8 MB PSRAM, 16 MB Flash). WiFi 802.11 b/g/n 2.4 GHz. Programmed via Arduino IDE with LVGL. Powered via USB-C 5V/1A. ~$25–35. Product wiki: elecrow.com/wiki/CrowPanel_2.1inch-HMI_ESP32_Rotary_Display |
| 1 | **USB-C 5V/1A power supply + cable** | For permanent panel mounting. A small USB wall adapter or a DIN-rail 5V USB supply keeps the HMI powered independently from the main controller enclosure. |
| 1 | **Surface-mount panel box** (optional) | For wall-mounting the round HMI near the main thermostat or in a utility area. The CrowPanel has a circular bezel suited to a standard round electrical box cutout. |

**Programming note:** The CrowPanel ESP32 sketch (not in this repository) needs:
- `PubSubClient` library for MQTT
- `ArduinoJson` for payload parsing
- `LVGL` (bundled with CrowPanel SDK) for the UI
- WiFi credentials and MQTT broker address configured at build time or via a
  first-boot provisioning screen

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
| Digital (relay) | Active-high logic | MCU D11–D19 | 9 HVAC control outputs | Relay contacts to load |

---

## Where to Buy

- **Arduino Uno Q:** [store-usa.arduino.cc](https://store-usa.arduino.cc) or authorized distributors (Mouser, Digi-Key, Arrow)
- **Eastron SDM120-Modbus:** Amazon (search "Eastron SDM120 Modbus"), AliExpress, or automation suppliers (e.g. Allied Electronics, AutomationDirect)
- **RS485 SHT30 transmitters:** Amazon, AliExpress (search "RS485 Modbus SHT30 temperature humidity transmitter"), DFRobot (product-2279)
- **Waveshare USB to RS485 adapter:** Amazon (search "Waveshare USB RS485 isolated FT232RL"), Waveshare official store
- **DIN-rail relay modules (24VAC coil):** Digi-Key, Mouser, AutomationDirect (search "Finder 34.51 24VAC" or "Phoenix Contact PLC-RSC-24AC")
- **Output relay board, power modules:** Amazon, AliExpress, Digi-Key
