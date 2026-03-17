# PCB Carrier Board

A carrier board with sockets for all the modules. No SMD soldering — just plug in the modules and go.

## What's on the board

| Socket | Pins | Notes |
|--------|------|-------|
| ESP32 Dev Board | 2x19 headers (38-pin) | 2.54mm pitch |
| TJA1050 CAN module | 4-pin header (5V) | With voltage divider (R1 + R2). Use this OR the SN65HVD230, not both |
| SN65HVD230 CAN module | 4-pin header (3.3V) | Direct connect, no voltage divider needed. Leave R1/R2 empty |
| HW-125 SD Card module | 6-pin header | SPI bus |
| SSD1306 OLED | 4-pin JST-XH | I2C, 3.3V |
| DS3231 RTC | 4-pin header | I2C, 3.3V (shares bus with OLED) |
| OBD2 connector | 4-pin JST-XH | CANH, CANL, GND, +12V (12V pin unused for now) |
| R1 (1kΩ) + R2 (2.2kΩ) | Through-hole | Voltage divider for TJA1050 RX. Leave empty if using SN65HVD230 |

## Generate the netlist

```bash
pip install skidl
cd pcb
python obd2_logger_pcb.py
```

This generates `obd2_logger.net`.

## View and layout in KiCad

1. Install [KiCad](https://www.kicad.org/download/) (free, open-source)
2. Open KiCad → **New Project**
3. Open the PCB editor (**pcbnew**)
4. **File → Import Netlist** → select `obd2_logger.net`
5. All components appear stacked — drag them into position
6. Route the traces (or use the auto-router)
7. Export gerbers: **File → Fabrication Outputs → Gerbers**

## Order PCBs

Export gerbers from KiCad and upload to:
- [JLCPCB](https://jlcpcb.com/) — 5 boards for ~₹150, ships in ~1 week
- [PCBWay](https://www.pcbway.com/) — similar pricing

## CAN module options

The board supports two CAN transceiver modules (populate one, not both):

**Option A: TJA1050 (5V)** — Use header `J_CAN_5V`, populate R1 and R2
```
TJA1050 CRX → R1 (1kΩ) → ESP32 GPIO4
                          ↓
                     R2 (2.2kΩ) → GND
```

**Option B: SN65HVD230 (3.3V)** — Use header `J_CAN_3V3`, leave R1/R2 empty
```
SN65HVD230 RX → ESP32 GPIO4 (direct)
```
