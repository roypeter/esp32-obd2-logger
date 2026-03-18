#!/usr/bin/env python3
"""
Generate a complete KiCad PCB with components placed and traces routed.
Run with KiCad's Python:
  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 generate_pcb.py
"""

import pcbnew
import os

MM = pcbnew.FromMM
PITCH = 2.54

# ============================================================
# Layout strategy v5:
# - ESP32 headers on the left
# - Module headers at board EDGES, modules extend OUTWARD:
#     SD card & RTC at top edge (rotated 90°, modules extend UP)
#     CAN modules at right edge (vertical, modules extend RIGHT)
# - Connectors (OLED JST, OBD2 JST) and resistors in the interior
# - M3 mounting holes in all 4 corners
# - Standard trace widths: 0.5mm signal, 1.0mm power
# ============================================================

BOARD_W = 80
BOARD_H = 56

# ESP32 position (left side)
EL_X = 8        # left header X
ER_X = 8 + 22.86  # right header X (30.86)
ESP_Y = 5       # top of headers (pin 1)
# ESP32 bottom: ESP_Y + 18*2.54 = 5 + 45.72 = 50.72

# Track widths (standard)
TW_SIG = 0.5
TW_PWR = 1.0

# Mounting hole offset from board edge
MH_OFFSET = 3.5
MH_DRILL = 3.2  # M3 screw


# ============================================================
# Helpers
# ============================================================

def esp_pin_y(pin):
    """Y position of ESP32 header pin (1-indexed)."""
    return ESP_Y + (pin - 1) * PITCH


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


def pad_xy(fp, pad_num):
    """Return pad position in mm as (x, y) tuple."""
    for pad in fp.Pads():
        if pad.GetNumber() == str(pad_num):
            p = pad.GetPosition()
            return (p.x / 1e6, p.y / 1e6)
    raise ValueError(f"Pad {pad_num} not found on {fp.GetReference()}")


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


def track(board, net, x1, y1, x2, y2, width=TW_SIG, layer=pcbnew.F_Cu):
    """Add a track segment using mm coordinates."""
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(MM(x1), MM(y1)))
    t.SetEnd(pcbnew.VECTOR2I(MM(x2), MM(y2)))
    t.SetWidth(MM(width))
    t.SetLayer(layer)
    t.SetNet(net)
    board.Add(t)


def track_L(board, net, x1, y1, x2, y2, width=TW_SIG, layer=pcbnew.F_Cu, h_first=True):
    """L-shaped route: horizontal then vertical (or vertical then horizontal)."""
    if h_first:
        track(board, net, x1, y1, x2, y1, width, layer)
        track(board, net, x2, y1, x2, y2, width, layer)
    else:
        track(board, net, x1, y1, x1, y2, width, layer)
        track(board, net, x1, y2, x2, y2, width, layer)


def via_at(board, net, x, y):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    v.SetDrill(MM(0.3))
    v.SetWidth(MM(0.6))
    v.SetNet(net)
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    board.Add(v)


def add_mounting_hole(board, x, y):
    """Add M3 mounting hole (NPTH)."""
    fp = pcbnew.FootprintLoad(
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/MountingHole.pretty",
        "MountingHole_3.2mm_M3"
    )
    fp.SetReference("")
    fp.SetValue("")
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
# More clearance from ESP32 (>15mm from ESP32_R)
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
# Route signals on F.Cu
# ============================================================

# --- I2C ---
e_scl = pad_xy(esp_r, 2)
o_scl = pad_xy(oled, 4)
r_scl = pad_xy(rtc, 3)
track_L(board, nets["I2C_SCL"], e_scl[0], e_scl[1], o_scl[0], o_scl[1])
track_L(board, nets["I2C_SCL"], o_scl[0], o_scl[1], r_scl[0], r_scl[1])

e_sda = pad_xy(esp_r, 5)
o_sda = pad_xy(oled, 3)
r_sda = pad_xy(rtc, 4)
track_L(board, nets["I2C_SDA"], e_sda[0], e_sda[1], o_sda[0], o_sda[1])
track_L(board, nets["I2C_SDA"], o_sda[0], o_sda[1], r_sda[0], r_sda[1])

# --- SD Card ---
e_miso = pad_xy(esp_r, 6)
s_miso = pad_xy(sd, 3)
track_L(board, nets["SD_MISO"], e_miso[0], e_miso[1], s_miso[0], s_miso[1], h_first=False)

e_sck = pad_xy(esp_r, 7)
s_sck = pad_xy(sd, 5)
track_L(board, nets["SD_SCK"], e_sck[0], e_sck[1], s_sck[0], s_sck[1], h_first=False)

e_mosi = pad_xy(esp_r, 1)
s_mosi = pad_xy(sd, 4)
track_L(board, nets["SD_MOSI"], e_mosi[0], e_mosi[1], s_mosi[0], s_mosi[1], h_first=False)

# SD CS: ESP32_R pin 13 (D15) → SD pin 6, route on B.Cu to avoid crossings
e_cs = pad_xy(esp_r, 13)
s_cs = pad_xy(sd, 6)
via_x_cs = ER_X + 3
via_at(board, nets["SD_CS"], via_x_cs, e_cs[1])
track(board, nets["SD_CS"], e_cs[0], e_cs[1], via_x_cs, e_cs[1])
track(board, nets["SD_CS"], via_x_cs, e_cs[1], via_x_cs, s_cs[1], layer=pcbnew.B_Cu)
via_at(board, nets["SD_CS"], via_x_cs, s_cs[1])
track(board, nets["SD_CS"], via_x_cs, s_cs[1], s_cs[0], s_cs[1])

# --- CAN ---
e_ctx = pad_xy(esp_r, 8)
c5_ctx = pad_xy(can5v, 3)
c3_ctx = pad_xy(can3v, 4)
track_L(board, nets["CAN_TX"], e_ctx[0], e_ctx[1], c5_ctx[0], c5_ctx[1])
track(board, nets["CAN_TX"], c5_ctx[0], c5_ctx[1], c5_ctx[0], c3_ctx[1])
track(board, nets["CAN_TX"], c5_ctx[0], c3_ctx[1], c3_ctx[0], c3_ctx[1])

c5_crx = pad_xy(can5v, 4)
r1_1 = pad_xy(r1, 1)
track_L(board, nets["CAN_RX_5V"], c5_crx[0], c5_crx[1], r1_1[0], r1_1[1], h_first=False)

r1_2 = pad_xy(r1, 2)
r2_1 = pad_xy(r2, 1)
track_L(board, nets["CAN_RX"], r1_2[0], r1_2[1], r2_1[0], r2_1[1], h_first=False)

e_crx = pad_xy(esp_r, 11)
track_L(board, nets["CAN_RX"], e_crx[0], e_crx[1], r2_1[0], r2_1[1])

c3_crx = pad_xy(can3v, 3)
track_L(board, nets["CAN_RX"], c3_crx[0], c3_crx[1], r1_2[0], r1_2[1])


# ============================================================
# Route power on F.Cu — wide traces
# ============================================================

# --- VCC_3V3: runs along LEFT edge then across top ---
v3_bus_x = 3
v3_top_y = 2

# ESP32_L pin 1 (3V3) left to bus
track(board, nets["VCC_3V3"], EL_X, esp_pin_y(1), v3_bus_x, esp_pin_y(1), TW_PWR)
track(board, nets["VCC_3V3"], v3_bus_x, esp_pin_y(1), v3_bus_x, v3_top_y, TW_PWR)
track(board, nets["VCC_3V3"], v3_bus_x, v3_top_y, BOARD_W - 2, v3_top_y, TW_PWR)

# Tap to OLED VCC (pin 2)
o_vcc = pad_xy(oled, 2)
tap_x_oled = o_vcc[0] - 2
track(board, nets["VCC_3V3"], tap_x_oled, v3_top_y, tap_x_oled, o_vcc[1], TW_PWR)
track(board, nets["VCC_3V3"], tap_x_oled, o_vcc[1], o_vcc[0], o_vcc[1], TW_PWR)

# Tap to RTC VCC (pin 5)
r_vcc = pad_xy(rtc, 5)
track(board, nets["VCC_3V3"], r_vcc[0], v3_top_y, r_vcc[0], r_vcc[1], TW_PWR)

# Tap to CAN_3V3 VCC (pin 1)
c3_vcc = pad_xy(can3v, 1)
track(board, nets["VCC_3V3"], BOARD_W - 2, v3_top_y, BOARD_W - 2, c3_vcc[1], TW_PWR)
track(board, nets["VCC_3V3"], BOARD_W - 2, c3_vcc[1], c3_vcc[0], c3_vcc[1], TW_PWR)

# ESP32_R pin 15 (3V3) — connect to left bus
e_3v3r = pad_xy(esp_r, 15)
track(board, nets["VCC_3V3"], e_3v3r[0], e_3v3r[1], v3_bus_x, e_3v3r[1], TW_PWR)

# --- VCC_5V: runs along BOTTOM edge ---
v5_bus_y = BOARD_H - 2

# ESP32_L pin 16 (VIN) — route left to clear pins 17-19, then down
vin_escape_x = EL_X - 3
track(board, nets["VCC_5V"], EL_X, esp_pin_y(16), vin_escape_x, esp_pin_y(16), TW_PWR)
track(board, nets["VCC_5V"], vin_escape_x, esp_pin_y(16), vin_escape_x, v5_bus_y, TW_PWR)
track(board, nets["VCC_5V"], vin_escape_x, v5_bus_y, BOARD_W - 2, v5_bus_y, TW_PWR)

# Tap to SD VCC (pin 2)
s_vcc = pad_xy(sd, 2)
track(board, nets["VCC_5V"], s_vcc[0], v5_bus_y, s_vcc[0], s_vcc[1], TW_PWR)

# Tap to CAN_5V VCC (pin 1)
c5_vcc = pad_xy(can5v, 1)
track(board, nets["VCC_5V"], BOARD_W - 2, v5_bus_y, BOARD_W - 2, c5_vcc[1], TW_PWR)
track(board, nets["VCC_5V"], BOARD_W - 2, c5_vcc[1], c5_vcc[0], c5_vcc[1], TW_PWR)


# ============================================================
# Route GND on B.Cu with vias
# ============================================================

gnd_pads = [
    (esp_l, 15), (esp_r, 14),
    (can5v, 2), (can3v, 2), (r2, 2),
    (sd, 1), (oled, 1), (rtc, 6), (obd2, 3),
]

gnd_via_positions = []
for fp, pn in gnd_pads:
    px, py = pad_xy(fp, pn)
    vx = px + 1.5
    vy = py
    via_at(board, nets["GND"], vx, vy)
    track(board, nets["GND"], px, py, vx, vy, TW_PWR)
    gnd_via_positions.append((vx, vy))

# Connect all GND vias on B.Cu
gnd_via_positions.sort(key=lambda p: (p[1], p[0]))
for i in range(len(gnd_via_positions) - 1):
    x1, y1 = gnd_via_positions[i]
    x2, y2 = gnd_via_positions[i + 1]
    track(board, nets["GND"], x1, y1, x2, y1, TW_PWR, pcbnew.B_Cu)
    track(board, nets["GND"], x2, y1, x2, y2, TW_PWR, pcbnew.B_Cu)


# ============================================================
# Save
# ============================================================

output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "obd2_logger", "obd2_logger.kicad_pcb")
board.Save(output_path)
print(f"PCB saved: {output_path}")
print(f"Board size: {BOARD_W}mm x {BOARD_H}mm")
print("Open in KiCad to view!")
