#!/usr/bin/env python3
"""
Generate KiCad PCB with components placed and nets assigned — NO traces.
Route interactively in KiCad pcbnew (the ratsnest shows unrouted connections).

Run with KiCad's Python:
  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 generate_pcb.py

Routing guide (use pcbnew interactive router):
  - Signal traces: 0.5mm on F.Cu
  - Power traces: 1.0mm on F.Cu
  - GND: use vias + B.Cu to avoid crossings
  - SD_CS may need a via to B.Cu to avoid crossing other signals
"""

import pcbnew
import os

MM = pcbnew.FromMM
PITCH = 2.54

# ============================================================
# Layout:
# - ESP32 headers on the left
# - Module headers at board EDGES, modules extend OUTWARD
# - M3 mounting holes in all 4 corners
# ============================================================

BOARD_W = 80
BOARD_H = 56

# ESP32 position (left side)
EL_X = 8        # left header X
ER_X = 8 + 22.86  # right header X (30.86)
ESP_Y = 5       # top of headers (pin 1)

# Mounting hole offset from board edge
MH_OFFSET = 3.5


# ============================================================
# Helpers
# ============================================================

def add_footprint(board, lib, fp_name, ref, value, x, y, angle=0):
    fp = pcbnew.FootprintLoad(
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/" + lib + ".pretty",
        fp_name
    )
    fp.SetReference(ref)
    fp.SetValue(value)
    fp.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    if angle:
        fp.SetOrientationDegrees(angle)
    board.Add(fp)
    return fp


def assign_net(fp, pad_num, net):
    for pad in fp.Pads():
        if pad.GetNumber() == str(pad_num):
            pad.SetNet(net)
            return


def make_net(board, name, net_dict):
    n = pcbnew.NETINFO_ITEM(board, name)
    board.Add(n)
    net_dict[name] = n
    return n


_mh_count = 0
def add_mounting_hole(board, x, y):
    """Add M3 mounting hole (NPTH)."""
    global _mh_count
    _mh_count += 1
    fp = pcbnew.FootprintLoad(
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/MountingHole.pretty",
        "MountingHole_3.2mm_M3"
    )
    fp.SetReference(f"MH{_mh_count}")
    fp.SetValue("MountingHole")
    fp.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    board.Add(fp)


# ============================================================
# Create board
# ============================================================

board = pcbnew.BOARD()

# Board outline
outline = pcbnew.PCB_SHAPE(board)
outline.SetShape(pcbnew.SHAPE_T_RECT)
outline.SetStart(pcbnew.VECTOR2I(MM(0), MM(0)))
outline.SetEnd(pcbnew.VECTOR2I(MM(BOARD_W), MM(BOARD_H)))
outline.SetLayer(pcbnew.Edge_Cuts)
outline.SetWidth(MM(0.1))
board.Add(outline)

# M3 mounting holes in all 4 corners
add_mounting_hole(board, MH_OFFSET, MH_OFFSET)
add_mounting_hole(board, BOARD_W - MH_OFFSET, MH_OFFSET)
add_mounting_hole(board, MH_OFFSET, BOARD_H - MH_OFFSET)
add_mounting_hole(board, BOARD_W - MH_OFFSET, BOARD_H - MH_OFFSET)

# ============================================================
# Nets
# ============================================================

nets = {}
for name in ["GND", "VCC_5V", "VCC_3V3", "CAN_TX", "CAN_RX", "CAN_RX_5V",
             "SD_MISO", "SD_MOSI", "SD_SCK", "SD_CS", "I2C_SDA", "I2C_SCL"]:
    make_net(board, name, nets)

# ============================================================
# Place components
# ============================================================

# --- ESP32 headers (left side) ---
esp_l = add_footprint(board,
    "Connector_PinHeader_2.54mm", "PinHeader_1x19_P2.54mm_Vertical",
    "J_ESP32_L", "ESP32_Left", EL_X, ESP_Y)

esp_r = add_footprint(board,
    "Connector_PinHeader_2.54mm", "PinHeader_1x19_P2.54mm_Vertical",
    "J_ESP32_R", "ESP32_Right", ER_X, ESP_Y)

# --- SD card at TOP edge (rotated 90°, module extends UP) ---
SD_X = 46
SD_Y = 3.5
sd = add_footprint(board,
    "Connector_PinHeader_2.54mm", "PinHeader_1x06_P2.54mm_Vertical",
    "J_SD", "HW-125_SD", SD_X, SD_Y, 90)

# --- RTC at TOP edge (rotated 90°, module extends UP) ---
RTC_X = 63
RTC_Y = 3.5
rtc = add_footprint(board,
    "Connector_PinHeader_2.54mm", "PinHeader_1x06_P2.54mm_Vertical",
    "J_RTC", "DS3231_RTC", RTC_X, RTC_Y, 90)

# --- CAN 5V (TJA1050) at RIGHT edge (module extends RIGHT) ---
CAN5V_X = BOARD_W - 3.5
CAN5V_Y = 16
can5v = add_footprint(board,
    "Connector_PinHeader_2.54mm", "PinHeader_1x04_P2.54mm_Vertical",
    "J_CAN_5V", "TJA1050", CAN5V_X, CAN5V_Y)

# --- CAN 3.3V (SN65HVD230) at RIGHT edge below CAN 5V ---
CAN3V_X = BOARD_W - 3.5
CAN3V_Y = 32
can3v = add_footprint(board,
    "Connector_PinHeader_2.54mm", "PinHeader_1x04_P2.54mm_Vertical",
    "J_CAN_3V3", "SN65HVD230", CAN3V_X, CAN3V_Y)

# --- OLED connector (interior, JST) ---
OLED_X = 46
OLED_Y = 14
oled = add_footprint(board,
    "Connector_JST", "JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
    "J_OLED", "SSD1306_OLED", OLED_X, OLED_Y, 180)

# --- OBD2 connector (interior bottom, JST) ---
OBD2_X = 46
OBD2_Y = BOARD_H - 8
obd2 = add_footprint(board,
    "Connector_JST", "JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
    "J_OBD2", "OBD2", OBD2_X, OBD2_Y)

# --- Voltage divider resistors (interior, near CAN area) ---
R_X = 50
r1 = add_footprint(board,
    "Resistor_THT", "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "R1", "1k", R_X, 28)

r2 = add_footprint(board,
    "Resistor_THT", "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "R2", "2.2k", R_X, 33)


# ============================================================
# Assign nets to pads
# ============================================================

# ESP32 Left: pin 1=3V3, 15=GND, 16=VIN
assign_net(esp_l, 1, nets["VCC_3V3"])
assign_net(esp_l, 15, nets["GND"])
assign_net(esp_l, 16, nets["VCC_5V"])

# ESP32 Right
assign_net(esp_r, 1, nets["SD_MOSI"])   # D23
assign_net(esp_r, 2, nets["I2C_SCL"])   # D22
assign_net(esp_r, 5, nets["I2C_SDA"])   # D21
assign_net(esp_r, 6, nets["SD_MISO"])   # D19
assign_net(esp_r, 7, nets["SD_SCK"])    # D18
assign_net(esp_r, 8, nets["CAN_TX"])    # D5
assign_net(esp_r, 11, nets["CAN_RX"])   # D4
assign_net(esp_r, 13, nets["SD_CS"])    # D15
assign_net(esp_r, 14, nets["GND"])
assign_net(esp_r, 15, nets["VCC_3V3"])

# CAN 5V: 1=VCC, 2=GND, 3=CTX, 4=CRX
assign_net(can5v, 1, nets["VCC_5V"])
assign_net(can5v, 2, nets["GND"])
assign_net(can5v, 3, nets["CAN_TX"])
assign_net(can5v, 4, nets["CAN_RX_5V"])

# CAN 3.3V: 1=3V3, 2=GND, 3=CRX, 4=CTX
assign_net(can3v, 1, nets["VCC_3V3"])
assign_net(can3v, 2, nets["GND"])
assign_net(can3v, 3, nets["CAN_RX"])
assign_net(can3v, 4, nets["CAN_TX"])

# Resistors
assign_net(r1, 1, nets["CAN_RX_5V"])
assign_net(r1, 2, nets["CAN_RX"])
assign_net(r2, 1, nets["CAN_RX"])
assign_net(r2, 2, nets["GND"])

# SD: 1=GND, 2=VCC, 3=MISO, 4=MOSI, 5=SCK, 6=CS
assign_net(sd, 1, nets["GND"])
assign_net(sd, 2, nets["VCC_5V"])
assign_net(sd, 3, nets["SD_MISO"])
assign_net(sd, 4, nets["SD_MOSI"])
assign_net(sd, 5, nets["SD_SCK"])
assign_net(sd, 6, nets["SD_CS"])

# OLED: 1=GND, 2=VCC, 3=SDA, 4=SCL
assign_net(oled, 1, nets["GND"])
assign_net(oled, 2, nets["VCC_3V3"])
assign_net(oled, 3, nets["I2C_SDA"])
assign_net(oled, 4, nets["I2C_SCL"])

# RTC: 1=32K, 2=SQW, 3=SCL, 4=SDA, 5=VCC, 6=GND
assign_net(rtc, 3, nets["I2C_SCL"])
assign_net(rtc, 4, nets["I2C_SDA"])
assign_net(rtc, 5, nets["VCC_3V3"])
assign_net(rtc, 6, nets["GND"])

# OBD2: 3=GND
assign_net(obd2, 3, nets["GND"])


# ============================================================
# Save PCB and export Specctra DSN for freerouting
# ============================================================

script_dir = os.path.dirname(os.path.abspath(__file__))
output_path = os.path.join(script_dir, "obd2_logger", "obd2_logger.kicad_pcb")
dsn_path = os.path.join(script_dir, "obd2_logger", "obd2_logger.dsn")

board.Save(output_path)
print(f"PCB saved: {output_path}")
print(f"Board size: {BOARD_W}mm x {BOARD_H}mm")

# Export Specctra DSN for freerouting
ok = pcbnew.ExportSpecctraDSN(board, dsn_path)
if ok:
    print(f"DSN exported: {dsn_path}")
else:
    print("WARNING: DSN export failed")

print()
print("Next steps:")
print(f"  1. Autoroute:  java -jar freerouting-2.1.0.jar -de {dsn_path} -do {dsn_path.replace('.dsn', '.ses')} -mp 20")
print(f"  2. Import SES: In pcbnew → File → Import → Specctra Session → select obd2_logger.ses")
