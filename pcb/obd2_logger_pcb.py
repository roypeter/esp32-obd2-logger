"""
ESP32 OBD2 Logger — PCB netlist generator

Generates a KiCad netlist (.net) directly without requiring KiCad
symbol libraries. Just needs Python — no other dependencies.

Board sockets:
  - ESP32 Dev Board (38-pin, 2x19, 2.54mm pitch)
  - TJA1050 CAN module (4-pin, 5V) with voltage divider (R1+R2)
  - SN65HVD230 CAN module (4-pin, 3.3V) direct connect
    (populate ONE CAN header, not both)
  - HW-125 Micro SD card module (6-pin header)
  - SSD1306 OLED display (4-pin JST-XH)
  - DS3231 RTC module (4-pin header)
  - OBD2 connection (4-pin JST-XH: CANH, CANL, GND, +12V)

Usage:
  python obd2_logger_pcb.py
  # Generates: obd2_logger.net → open in KiCad pcbnew → Import Netlist
"""

import time


# ============================================================
# Simple netlist builder (KiCad .net format)
# ============================================================

class NetlistComponent:
    def __init__(self, ref, value, footprint, pins):
        self.ref = ref
        self.value = value
        self.footprint = footprint
        self.pins = pins  # list of pin names


class NetlistNet:
    def __init__(self, name):
        self.name = name
        self.connections = []  # list of (component_ref, pin_number)

    def connect(self, component, pin_name):
        # Find pin number from name
        pin_idx = component.pins.index(pin_name) + 1
        self.connections.append((component.ref, str(pin_idx)))
        return self


class Netlist:
    def __init__(self):
        self.components = []
        self.nets = []

    def add_component(self, ref, value, footprint, pins):
        c = NetlistComponent(ref, value, footprint, pins)
        self.components.append(c)
        return c

    def add_net(self, name):
        n = NetlistNet(name)
        self.nets.append(n)
        return n

    def export(self, filename):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        lines.append("(export (version D)")
        lines.append(f'  (design (date "{timestamp}"))')
        lines.append("")

        # Components
        lines.append("  (components")
        for c in self.components:
            lines.append(f'    (comp (ref {c.ref})')
            lines.append(f'      (value "{c.value}")')
            lines.append(f'      (footprint "{c.footprint}")')
            lines.append(f"      (fields)")
            lines.append(f"      (libsource)")
            lines.append(f"      (sheetpath (names /) (tstamps /))")
            lines.append(f"    )")
        lines.append("  )")
        lines.append("")

        # Nets
        lines.append("  (nets")
        for i, n in enumerate(self.nets, start=1):
            if not n.connections:
                continue
            lines.append(f'    (net (code {i}) (name "{n.name}")')
            for ref, pin in n.connections:
                lines.append(f"      (node (ref {ref}) (pin {pin}))")
            lines.append(f"    )")
        lines.append("  )")
        lines.append(")")

        with open(filename, "w") as f:
            f.write("\n".join(lines))
        print(f"Netlist generated: {filename}")


# ============================================================
# Footprint constants
# ============================================================

FP_HDR = "Connector_PinHeader_2.54mm:PinHeader_1x{n}_P2.54mm_Vertical"
FP_JST4 = "Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical"
FP_RES = "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"


# ============================================================
# Build the netlist
# ============================================================

nl = Netlist()

# --- Components ---

esp32_l = nl.add_component("J_ESP32_L", "ESP32_Left", FP_HDR.format(n=19), [
    "3V3", "EN", "VP", "VN", "D34", "D35", "D32", "D33",
    "D25", "D26", "D27", "D14", "D12", "D13", "GND",
    "VIN", "CMD", "CLK", "SD0",
])

esp32_r = nl.add_component("J_ESP32_R", "ESP32_Right", FP_HDR.format(n=19), [
    "D23", "D22", "TX0", "RX0", "D21", "D19", "D18", "D5",
    "D17", "D16", "D4", "D2", "D15", "GND_R",
    "3V3_R", "D6", "D7", "D8", "D9",
])

can_5v = nl.add_component("J_CAN_5V", "TJA1050", FP_HDR.format(n=4), [
    "VCC", "GND", "CTX", "CRX",
])

can_3v3 = nl.add_component("J_CAN_3V3", "SN65HVD230", FP_HDR.format(n=4), [
    "3V3", "GND", "CRX", "CTX",
])

sd = nl.add_component("J_SD", "HW-125_SD", FP_HDR.format(n=6), [
    "GND", "VCC", "MISO", "MOSI", "SCK", "CS",
])

oled = nl.add_component("J_OLED", "SSD1306_OLED", FP_JST4, [
    "GND", "VCC", "SDA", "SCL",
])

rtc = nl.add_component("J_RTC", "DS3231_RTC", FP_HDR.format(n=4), [
    "GND", "VCC", "SDA", "SCL",
])

obd2 = nl.add_component("J_OBD2", "OBD2", FP_JST4, [
    "CANH", "CANL", "GND", "12V",
])

r1 = nl.add_component("R1", "1k", FP_RES, ["1", "2"])
r2 = nl.add_component("R2", "2.2k", FP_RES, ["1", "2"])


# --- Nets ---

# Power
gnd = nl.add_net("GND")
gnd.connect(esp32_l, "GND")
gnd.connect(esp32_r, "GND_R")
gnd.connect(can_5v, "GND")
gnd.connect(can_3v3, "GND")
gnd.connect(sd, "GND")
gnd.connect(oled, "GND")
gnd.connect(rtc, "GND")
gnd.connect(obd2, "GND")
gnd.connect(r2, "2")  # voltage divider bottom

vcc5 = nl.add_net("VCC_5V")
vcc5.connect(esp32_l, "VIN")
vcc5.connect(can_5v, "VCC")
vcc5.connect(sd, "VCC")

vcc3 = nl.add_net("VCC_3V3")
vcc3.connect(esp32_l, "3V3")
vcc3.connect(esp32_r, "3V3_R")
vcc3.connect(can_3v3, "3V3")
vcc3.connect(oled, "VCC")
vcc3.connect(rtc, "VCC")

# CAN TX: ESP32 GPIO5 → both CAN module TX pins
can_tx = nl.add_net("CAN_TX")
can_tx.connect(esp32_r, "D5")
can_tx.connect(can_5v, "CTX")
can_tx.connect(can_3v3, "CTX")

# CAN RX: ESP32 GPIO4 ← voltage divider midpoint / direct from 3.3V module
can_rx = nl.add_net("CAN_RX")
can_rx.connect(esp32_r, "D4")
can_rx.connect(r1, "2")       # midpoint of voltage divider
can_rx.connect(r2, "1")       # midpoint of voltage divider
can_rx.connect(can_3v3, "CRX")  # direct from 3.3V module

# CAN RX 5V: TJA1050 CRX → top of voltage divider
can_rx_5v = nl.add_net("CAN_RX_5V")
can_rx_5v.connect(can_5v, "CRX")
can_rx_5v.connect(r1, "1")

# SPI: SD Card
sd_miso = nl.add_net("SD_MISO")
sd_miso.connect(esp32_r, "D19")
sd_miso.connect(sd, "MISO")

sd_mosi = nl.add_net("SD_MOSI")
sd_mosi.connect(esp32_r, "D23")
sd_mosi.connect(sd, "MOSI")

sd_sck = nl.add_net("SD_SCK")
sd_sck.connect(esp32_r, "D18")
sd_sck.connect(sd, "SCK")

sd_cs = nl.add_net("SD_CS")
sd_cs.connect(esp32_r, "D15")
sd_cs.connect(sd, "CS")

# I2C: OLED + RTC
i2c_sda = nl.add_net("I2C_SDA")
i2c_sda.connect(esp32_r, "D21")
i2c_sda.connect(oled, "SDA")
i2c_sda.connect(rtc, "SDA")

i2c_scl = nl.add_net("I2C_SCL")
i2c_scl.connect(esp32_r, "D22")
i2c_scl.connect(oled, "SCL")
i2c_scl.connect(rtc, "SCL")


# --- Generate ---

nl.export("obd2_logger.net")
print("Open in KiCad: pcbnew → File → Import Netlist")
