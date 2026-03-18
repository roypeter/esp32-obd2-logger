#!/usr/bin/env python3
"""
Generate a SINGLE-SIDED KiCad PCB for DIY home etching.

All copper traces on B.Cu (bottom layer — the side you etch).
Components soldered on top. Wire jumpers bridge over traces on component side.

Optimized for DIY:
  - Wide traces (1.0mm signal, 1.5mm power, 2.0mm GND)
  - Only 4 wire jumpers needed (use short pieces of wire or 0Ω resistors)
  - Large clearances between traces
  - M3 mounting holes in corners

Run with KiCad's Python:
  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 generate_pcb_single.py
"""

import pcbnew
import os

MM = pcbnew.FromMM
PITCH = 2.54

# ============================================================
# Routing strategy:
# Modules placed in a column right of ESP32, aligned with their
# connected pins. This gives DIRECT horizontal routes for most
# signals — only MOSI and CS need jumpers to cross other signal
# zones. GND routes along board edges to avoid signal area.
#
# ESP32_R pin layout:
#   1=D23(MOSI) 2=D22(SCL) .. 5=D21(SDA) 6=D19(MISO)
#   7=D18(SCK) 8=D5(CAN_TX) .. 11=D4(CAN_RX) .. 13=D15(CS)
#
# Module order (top to bottom, matching pin order):
#   OLED+RTC (I2C, pins 2,5)
#   SD card (SPI, pins 6,7 — MOSI/CS via jumpers)
#   Resistors + CAN modules (pins 8,11)
#   OBD2 (bottom)
#
# Jumpers needed:
#   JW1: MOSI — pin 1 (top) crosses I2C zone to reach SD
#   JW2: CS — pin 13 (bottom) crosses CAN zone to reach SD
#   JW3: I2C_SCL — bridges over MOSI vertical drop
#   JW4: I2C_SDA — bridges over MOSI vertical drop
# ============================================================

BOARD_W = 95
BOARD_H = 68

EL_X = 10
ER_X = EL_X + 22.86  # 32.86
ESP_Y = 10

TW_SIG = 1.0    # 1mm signals — wide for DIY etching
TW_PWR = 1.5    # 1.5mm power
TW_GND = 2.0    # 2mm GND bus

MH_OFFSET = 4
CU = pcbnew.B_Cu  # all copper on bottom layer

FP_PATH = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/"


def esp_pin_y(pin):
    return ESP_Y + (pin - 1) * PITCH

def add_fp(board, lib, name, ref, val, x, y, angle=0):
    fp = pcbnew.FootprintLoad(FP_PATH + lib + ".pretty", name)
    fp.SetReference(ref)
    fp.SetValue(val)
    fp.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    if angle:
        fp.SetOrientationDegrees(angle)
    board.Add(fp)
    return fp

def pad_xy(fp, n):
    for pad in fp.Pads():
        if pad.GetNumber() == str(n):
            p = pad.GetPosition()
            return (p.x / 1e6, p.y / 1e6)
    raise ValueError(f"Pad {n} not found on {fp.GetReference()}")

def assign_net(fp, n, net):
    for pad in fp.Pads():
        if pad.GetNumber() == str(n):
            pad.SetNet(net)
            return

def make_net(board, name, d):
    n = pcbnew.NETINFO_ITEM(board, name)
    board.Add(n)
    d[name] = n
    return n

def tr(board, net, x1, y1, x2, y2, w=TW_SIG, layer=CU):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(MM(x1), MM(y1)))
    t.SetEnd(pcbnew.VECTOR2I(MM(x2), MM(y2)))
    t.SetWidth(MM(w))
    t.SetLayer(layer)
    t.SetNet(net)
    board.Add(t)

def tr_L(board, net, x1, y1, x2, y2, w=TW_SIG, layer=CU, h_first=True):
    if h_first:
        tr(board, net, x1, y1, x2, y1, w, layer)
        tr(board, net, x2, y1, x2, y2, w, layer)
    else:
        tr(board, net, x1, y1, x1, y2, w, layer)
        tr(board, net, x1, y2, x2, y2, w, layer)

def add_mh(board, x, y):
    fp = pcbnew.FootprintLoad(FP_PATH + "MountingHole.pretty", "MountingHole_3.2mm_M3")
    fp.SetReference("")
    fp.SetValue("")
    fp.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    board.Add(fp)


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

add_mh(board, MH_OFFSET, MH_OFFSET)
add_mh(board, BOARD_W - MH_OFFSET, MH_OFFSET)
add_mh(board, MH_OFFSET, BOARD_H - MH_OFFSET)
add_mh(board, BOARD_W - MH_OFFSET, BOARD_H - MH_OFFSET)

# ============================================================
# Nets
nets = {}
for name in ["GND", "VCC_5V", "VCC_3V3", "CAN_TX", "CAN_RX", "CAN_RX_5V",
             "SD_MISO", "SD_MOSI", "SD_SCK", "SD_CS", "I2C_SDA", "I2C_SCL"]:
    make_net(board, name, nets)

# ============================================================
# Place components — modules in column, aligned with ESP32 pins
# ============================================================

MOD_X = 58      # module column X
MOD_X2 = 78    # second column (RTC, CAN modules)

esp_l = add_fp(board, "Connector_PinHeader_2.54mm",
    "PinHeader_1x19_P2.54mm_Vertical", "J_ESP32_L", "ESP32_Left", EL_X, ESP_Y)
esp_r = add_fp(board, "Connector_PinHeader_2.54mm",
    "PinHeader_1x19_P2.54mm_Vertical", "J_ESP32_R", "ESP32_Right", ER_X, ESP_Y)

# OLED rotated 180° so SCL(pin4) is on top, matching ESP32 pin order
# Place so SCL aligns with ESP32_R pin 2
oled = add_fp(board, "Connector_JST",
    "JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
    "J_OLED", "SSD1306_OLED", MOD_X, esp_pin_y(2), 180)

# RTC 6-pin: 32K,SQW,SCL,SDA,VCC,GND — SCL(pin3) near ESP32 pin 2
rtc = add_fp(board, "Connector_PinHeader_2.54mm",
    "PinHeader_1x06_P2.54mm_Vertical",
    "J_RTC", "DS3231_RTC", MOD_X2, esp_pin_y(2) - 2 * PITCH)

# SD card: place so MISO(pin3) aligns with ESP32_R pin 6 (D19)
sd = add_fp(board, "Connector_PinHeader_2.54mm",
    "PinHeader_1x06_P2.54mm_Vertical",
    "J_SD", "HW-125_SD", MOD_X, esp_pin_y(4))

# CAN 5V: place so CTX(pin3) aligns with ESP32_R pin 8
can5v = add_fp(board, "Connector_PinHeader_2.54mm",
    "PinHeader_1x04_P2.54mm_Vertical",
    "J_CAN_5V", "TJA1050", MOD_X2, esp_pin_y(6))

# CAN 3.3V: near CAN_RX level
can3v = add_fp(board, "Connector_PinHeader_2.54mm",
    "PinHeader_1x04_P2.54mm_Vertical",
    "J_CAN_3V3", "SN65HVD230", MOD_X2, esp_pin_y(10))

# Resistors between SD and CAN areas
r1 = add_fp(board, "Resistor_THT",
    "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "R1", "1k", MOD_X, esp_pin_y(9))
r2 = add_fp(board, "Resistor_THT",
    "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "R2", "2.2k", MOD_X, esp_pin_y(11))

# OBD2 at bottom
obd2 = add_fp(board, "Connector_JST",
    "JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
    "J_OBD2", "OBD2", MOD_X, esp_pin_y(16))

# --- Jumper wires (0Ω resistors on component side) ---
# JW1: MOSI bridge — placed between pins 1 and 6 to bridge I2C zone
# Horizontal jumper at y midway between pin 1 and pin 6
jw1_y = esp_pin_y(3)  # between SCL(2) and SDA(5)
jw1 = add_fp(board, "Resistor_THT",
    "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "JW1", "wire", ER_X + 3, jw1_y)

# JW2: CS bridge — between pins 8 and 13 to bridge CAN zone
jw2_y = esp_pin_y(10)  # between CAN_TX(8) and CAN_RX(11)
jw2 = add_fp(board, "Resistor_THT",
    "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "JW2", "wire", ER_X + 3, jw2_y)

# ============================================================
# Assign nets
# ============================================================

assign_net(esp_l, 1, nets["VCC_3V3"])
assign_net(esp_l, 15, nets["GND"])
assign_net(esp_l, 16, nets["VCC_5V"])

assign_net(esp_r, 1, nets["SD_MOSI"])
assign_net(esp_r, 2, nets["I2C_SCL"])
assign_net(esp_r, 5, nets["I2C_SDA"])
assign_net(esp_r, 6, nets["SD_MISO"])
assign_net(esp_r, 7, nets["SD_SCK"])
assign_net(esp_r, 8, nets["CAN_TX"])
assign_net(esp_r, 11, nets["CAN_RX"])
assign_net(esp_r, 13, nets["SD_CS"])
assign_net(esp_r, 14, nets["GND"])
assign_net(esp_r, 15, nets["VCC_3V3"])

assign_net(can5v, 1, nets["VCC_5V"])
assign_net(can5v, 2, nets["GND"])
assign_net(can5v, 3, nets["CAN_TX"])
assign_net(can5v, 4, nets["CAN_RX_5V"])

assign_net(can3v, 1, nets["VCC_3V3"])
assign_net(can3v, 2, nets["GND"])
assign_net(can3v, 3, nets["CAN_RX"])
assign_net(can3v, 4, nets["CAN_TX"])

assign_net(r1, 1, nets["CAN_RX_5V"])
assign_net(r1, 2, nets["CAN_RX"])
assign_net(r2, 1, nets["CAN_RX"])
assign_net(r2, 2, nets["GND"])

assign_net(sd, 1, nets["GND"])
assign_net(sd, 2, nets["VCC_5V"])
assign_net(sd, 3, nets["SD_MISO"])
assign_net(sd, 4, nets["SD_MOSI"])
assign_net(sd, 5, nets["SD_SCK"])
assign_net(sd, 6, nets["SD_CS"])

assign_net(oled, 1, nets["GND"])
assign_net(oled, 2, nets["VCC_3V3"])
assign_net(oled, 3, nets["I2C_SDA"])
assign_net(oled, 4, nets["I2C_SCL"])

assign_net(rtc, 3, nets["I2C_SCL"])
assign_net(rtc, 4, nets["I2C_SDA"])
assign_net(rtc, 5, nets["VCC_3V3"])
assign_net(rtc, 6, nets["GND"])

assign_net(obd2, 3, nets["GND"])

# Jumper nets
assign_net(jw1, 1, nets["SD_MOSI"])
assign_net(jw1, 2, nets["SD_MOSI"])
assign_net(jw2, 1, nets["SD_CS"])
assign_net(jw2, 2, nets["SD_CS"])


# ============================================================
# Route signals on B.Cu (bottom copper)
# ============================================================

# --- Direct horizontal routes (no crossings) ---

# I2C_SCL: ESP32_R pin 2 → OLED pin 4 → RTC pin 3
e_scl = pad_xy(esp_r, 2)
o_scl = pad_xy(oled, 4)
r_scl = pad_xy(rtc, 3)
tr_L(board, nets["I2C_SCL"], e_scl[0], e_scl[1], o_scl[0], o_scl[1])
tr_L(board, nets["I2C_SCL"], o_scl[0], o_scl[1], r_scl[0], r_scl[1])

# I2C_SDA: ESP32_R pin 5 → OLED pin 3 → RTC pin 4
e_sda = pad_xy(esp_r, 5)
o_sda = pad_xy(oled, 3)
r_sda = pad_xy(rtc, 4)
tr_L(board, nets["I2C_SDA"], e_sda[0], e_sda[1], o_sda[0], o_sda[1])
tr_L(board, nets["I2C_SDA"], o_sda[0], o_sda[1], r_sda[0], r_sda[1])

# SD_MISO: ESP32_R pin 6 → SD pin 3 (straight right, same Y level)
e_miso = pad_xy(esp_r, 6)
s_miso = pad_xy(sd, 3)
tr_L(board, nets["SD_MISO"], e_miso[0], e_miso[1], s_miso[0], s_miso[1])

# SD_SCK: ESP32_R pin 7 → SD pin 5 (L-shape)
e_sck = pad_xy(esp_r, 7)
s_sck = pad_xy(sd, 5)
tr_L(board, nets["SD_SCK"], e_sck[0], e_sck[1], s_sck[0], s_sck[1])

# CAN_TX: ESP32_R pin 8 → CAN_5V pin 3 → CAN_3V3 pin 4
e_ctx = pad_xy(esp_r, 8)
c5_ctx = pad_xy(can5v, 3)
c3_ctx = pad_xy(can3v, 4)
tr_L(board, nets["CAN_TX"], e_ctx[0], e_ctx[1], c5_ctx[0], c5_ctx[1])
tr(board, nets["CAN_TX"], c5_ctx[0], c5_ctx[1], c5_ctx[0], c3_ctx[1])
tr(board, nets["CAN_TX"], c5_ctx[0], c3_ctx[1], c3_ctx[0], c3_ctx[1])

# CAN_RX: ESP32_R pin 11 → R2 pin 1
e_crx = pad_xy(esp_r, 11)
r2_1 = pad_xy(r2, 1)
tr_L(board, nets["CAN_RX"], e_crx[0], e_crx[1], r2_1[0], r2_1[1])

# CAN_RX: R1 pin 2 → R2 pin 1 (voltage divider midpoint)
r1_2 = pad_xy(r1, 2)
tr_L(board, nets["CAN_RX"], r1_2[0], r1_2[1], r2_1[0], r2_1[1], h_first=False)

# CAN_RX: CAN_3V3 pin 3 → R1 pin 2
c3_crx = pad_xy(can3v, 3)
tr_L(board, nets["CAN_RX"], c3_crx[0], c3_crx[1], r1_2[0], r1_2[1])

# CAN_RX_5V: CAN_5V pin 4 → R1 pin 1
c5_crx = pad_xy(can5v, 4)
r1_1 = pad_xy(r1, 1)
tr_L(board, nets["CAN_RX_5V"], c5_crx[0], c5_crx[1], r1_1[0], r1_1[1], h_first=False)


# --- Jumper routes (cross other signal zones) ---

# SD_MOSI: ESP32_R pin 1 → down to JW1 pad 1 (on copper)
#          JW1 pad 1 ↔ JW1 pad 2 (wire on component side, shown on F.Cu)
#          JW1 pad 2 → right/down to SD pin 4 (on copper)
e_mosi = pad_xy(esp_r, 1)
jw1_p1 = pad_xy(jw1, 1)
jw1_p2 = pad_xy(jw1, 2)
s_mosi = pad_xy(sd, 4)

# Copper: ESP32 pin 1 → down on left side (x = ER_X - 2) to JW1 pad 1
mosi_x = e_mosi[0] - 2  # route left of ESP32_R header to avoid other pads
tr(board, nets["SD_MOSI"], e_mosi[0], e_mosi[1], mosi_x, e_mosi[1])
tr(board, nets["SD_MOSI"], mosi_x, e_mosi[1], mosi_x, jw1_p1[1])
tr(board, nets["SD_MOSI"], mosi_x, jw1_p1[1], jw1_p1[0], jw1_p1[1])

# Wire jumper on component side (F.Cu represents the jumper wire)
tr(board, nets["SD_MOSI"], jw1_p1[0], jw1_p1[1], jw1_p2[0], jw1_p2[1],
   TW_SIG, pcbnew.F_Cu)

# Copper: JW1 pad 2 → right/down to SD pin 4
tr_L(board, nets["SD_MOSI"], jw1_p2[0], jw1_p2[1], s_mosi[0], s_mosi[1])


# SD_CS: ESP32_R pin 13 → up to JW2 pad 1 (on copper)
#        JW2 pad 1 ↔ JW2 pad 2 (wire on component side)
#        JW2 pad 2 → right/up to SD pin 6 (on copper)
e_cs = pad_xy(esp_r, 13)
jw2_p1 = pad_xy(jw2, 1)
jw2_p2 = pad_xy(jw2, 2)
s_cs = pad_xy(sd, 6)

# Copper: ESP32 pin 13 → up on left side to JW2 pad 1
cs_x = e_cs[0] - 4  # route left of MOSI vertical to avoid crossing
tr(board, nets["SD_CS"], e_cs[0], e_cs[1], cs_x, e_cs[1])
tr(board, nets["SD_CS"], cs_x, e_cs[1], cs_x, jw2_p1[1])
tr(board, nets["SD_CS"], cs_x, jw2_p1[1], jw2_p1[0], jw2_p1[1])

# Wire jumper on component side
tr(board, nets["SD_CS"], jw2_p1[0], jw2_p1[1], jw2_p2[0], jw2_p2[1],
   TW_SIG, pcbnew.F_Cu)

# Copper: JW2 pad 2 → right/up to SD pin 6
tr_L(board, nets["SD_CS"], jw2_p2[0], jw2_p2[1], s_cs[0], s_cs[1])


# ============================================================
# Power routing on B.Cu
# ============================================================

# --- VCC_3V3: along TOP edge ---
v3_y = 3  # 3V3 bus Y
# ESP32_L pin 1 (3V3) → up to bus → right along top
tr(board, nets["VCC_3V3"], EL_X, esp_pin_y(1), EL_X, v3_y, TW_PWR)
tr(board, nets["VCC_3V3"], EL_X, v3_y, BOARD_W - 3, v3_y, TW_PWR)

# Taps down to 3V3 devices
o_vcc = pad_xy(oled, 2)
tr(board, nets["VCC_3V3"], o_vcc[0], v3_y, o_vcc[0], o_vcc[1], TW_PWR)

r_vcc = pad_xy(rtc, 5)
tr(board, nets["VCC_3V3"], r_vcc[0], v3_y, r_vcc[0], r_vcc[1], TW_PWR)

c3_vcc = pad_xy(can3v, 1)
tr(board, nets["VCC_3V3"], BOARD_W - 3, v3_y, BOARD_W - 3, c3_vcc[1], TW_PWR)
tr(board, nets["VCC_3V3"], BOARD_W - 3, c3_vcc[1], c3_vcc[0], c3_vcc[1], TW_PWR)

# ESP32_R pin 15 (3V3) → left to left edge → up to bus
e_3v3r = pad_xy(esp_r, 15)
tr(board, nets["VCC_3V3"], e_3v3r[0], e_3v3r[1], 4, e_3v3r[1], TW_PWR)
tr(board, nets["VCC_3V3"], 4, e_3v3r[1], 4, v3_y, TW_PWR)

# --- VCC_5V: along BOTTOM edge ---
v5_y = BOARD_H - 3
# ESP32_L pin 16 (VIN) → escape left, then down, then right
vin_x = EL_X - 3
tr(board, nets["VCC_5V"], EL_X, esp_pin_y(16), vin_x, esp_pin_y(16), TW_PWR)
tr(board, nets["VCC_5V"], vin_x, esp_pin_y(16), vin_x, v5_y, TW_PWR)
tr(board, nets["VCC_5V"], vin_x, v5_y, BOARD_W - 3, v5_y, TW_PWR)

# Taps up to 5V devices
s_vcc = pad_xy(sd, 2)
tr(board, nets["VCC_5V"], s_vcc[0], v5_y, s_vcc[0], s_vcc[1], TW_PWR)

c5_vcc = pad_xy(can5v, 1)
tr(board, nets["VCC_5V"], BOARD_W - 3, v5_y, BOARD_W - 3, c5_vcc[1], TW_PWR)
tr(board, nets["VCC_5V"], BOARD_W - 3, c5_vcc[1], c5_vcc[0], c5_vcc[1], TW_PWR)


# ============================================================
# GND routing on B.Cu — along edges to avoid crossing signals
# ============================================================

# GND bus: right edge (vertical) + bottom edge (horizontal)
gnd_bus_x = BOARD_W - 5  # vertical GND bus on right side
gnd_bus_y = BOARD_H - 6  # horizontal GND bus near bottom

# Vertical bus on right side (from top to bottom)
tr(board, nets["GND"], gnd_bus_x, 6, gnd_bus_x, gnd_bus_y, TW_GND)
# Horizontal bus along bottom
tr(board, nets["GND"], 6, gnd_bus_y, gnd_bus_x, gnd_bus_y, TW_GND)

# ESP32_L pin 15 (GND) → left to edge → down to bottom bus
e_gnd_l = pad_xy(esp_l, 15)
tr(board, nets["GND"], e_gnd_l[0], e_gnd_l[1], 6, e_gnd_l[1], TW_GND)
tr(board, nets["GND"], 6, e_gnd_l[1], 6, gnd_bus_y, TW_GND)

# ESP32_R pin 14 (GND) → right to GND bus
e_gnd_r = pad_xy(esp_r, 14)
tr(board, nets["GND"], e_gnd_r[0], e_gnd_r[1], gnd_bus_x, e_gnd_r[1], TW_GND)

# Module GND pads → right to GND bus (horizontal runs at module Y levels)
gnd_pads = [
    (oled, 1), (rtc, 6), (sd, 1),
    (can5v, 2), (can3v, 2), (r2, 2), (obd2, 3),
]
for fp, pn in gnd_pads:
    px, py = pad_xy(fp, pn)
    tr(board, nets["GND"], px, py, gnd_bus_x, py, TW_GND)


# ============================================================
# Save
# ============================================================

out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "obd2_logger_single.kicad_pcb")
board.Save(out)
print(f"Single-sided PCB saved: {out}")
print(f"Board size: {BOARD_W}mm x {BOARD_H}mm")
print(f"Jumpers: JW1 (MOSI), JW2 (CS) — solder short wire links on component side")
print("All copper on B.Cu (bottom). Etch one side only.")
