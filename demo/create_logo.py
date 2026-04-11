"""Create AgentNet logo in multiple formats."""

import math
import os

from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = "C:/Windows/Fonts"
FONT_BOLD = os.path.join(FONTS_DIR, "arialbd.ttf")
FONT_REG = os.path.join(FONTS_DIR, "arial.ttf")

BG = (10, 14, 26)
ACCENT = (79, 110, 247)
PURPLE = (139, 92, 246)
CYAN = (6, 182, 212)
WHITE = (255, 255, 255)
MUTED = (148, 163, 184)


def draw_node(draw, cx, cy, r, color):
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
    hr = r // 3
    highlight = tuple(min(255, c + 60) for c in color)
    draw.ellipse(
        [(cx - hr - 1, cy - hr - 2), (cx + hr - 1, cy + hr - 2)], fill=highlight
    )


def create_logo_icon(size=512):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy = size // 2, size // 2
    main_r = size * 0.32
    node_r = size // 14
    node_colors = [ACCENT, PURPLE, CYAN, ACCENT, PURPLE, CYAN]

    outer_nodes = []
    for i in range(6):
        angle = math.radians(60 * i - 90)
        nx = cx + main_r * math.cos(angle)
        ny = cy + main_r * math.sin(angle)
        outer_nodes.append((nx, ny))

    # Connections to center
    for nx, ny in outer_nodes:
        draw.line([(cx, cy), (int(nx), int(ny))], fill=(40, 55, 90), width=2)

    # Adjacent connections
    for i in range(6):
        j = (i + 1) % 6
        x1, y1 = outer_nodes[i]
        x2, y2 = outer_nodes[j]
        draw.line([(int(x1), int(y1)), (int(x2), int(y2))], fill=(30, 45, 80), width=1)

    # Star connections
    for i in range(6):
        j = (i + 2) % 6
        x1, y1 = outer_nodes[i]
        x2, y2 = outer_nodes[j]
        draw.line([(int(x1), int(y1)), (int(x2), int(y2))], fill=(35, 50, 85), width=1)

    # Data flow particles
    for i, (nx, ny) in enumerate(outer_nodes):
        for t in [0.3, 0.6]:
            px = cx + (nx - cx) * t
            py = cy + (ny - cy) * t
            draw.ellipse([(px - 3, py - 3), (px + 3, py + 3)], fill=node_colors[i])

    # Outer nodes
    for i, (nx, ny) in enumerate(outer_nodes):
        draw_node(draw, int(nx), int(ny), node_r, node_colors[i])

    # Center node (larger)
    center_r = size // 9
    for r in range(center_r + 15, center_r, -1):
        alpha = (center_r + 15 - r) / 15
        gc = tuple(int(ACCENT[j] * alpha * 0.15) for j in range(3))
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=gc)

    draw_node(draw, cx, cy, center_r, ACCENT)

    # "A" in center
    try:
        a_font = ImageFont.truetype(FONT_BOLD, center_r)
        bbox = draw.textbbox((0, 0), "A", font=a_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2 - 3), "A", fill=WHITE, font=a_font)
    except Exception:
        pass

    return img


def create_full_logo(width=1200, height=300):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    icon = create_logo_icon(size=280)
    img.paste(icon, (10, 10), icon)

    try:
        title_font = ImageFont.truetype(FONT_BOLD, 90)
        sub_font = ImageFont.truetype(FONT_REG, 24)
    except Exception:
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()

    text_x = 310
    draw.text((text_x, 80), "Agent", fill=WHITE, font=title_font)
    bbox = draw.textbbox((0, 0), "Agent", font=title_font)
    agent_w = bbox[2] - bbox[0]
    draw.text((text_x + agent_w, 80), "Net", fill=ACCENT, font=title_font)
    draw.text(
        (text_x + 5, 185),
        "The Autonomous AI Agent Economy",
        fill=MUTED,
        font=sub_font,
    )

    return img


os.makedirs("demo/assets", exist_ok=True)

# 1. Icon transparent
icon = create_logo_icon(512)
icon.save("demo/assets/logo-icon-512.png")
print("Created: logo-icon-512.png")

# 2. Icon dark bg
icon_dark = Image.new("RGB", (512, 512), BG)
icon_dark.paste(icon, (0, 0), icon)
icon_dark.save("demo/assets/logo-icon-dark.png")
print("Created: logo-icon-dark.png")

# 3. Full logo transparent
full = create_full_logo()
full.save("demo/assets/logo-full.png")
print("Created: logo-full.png")

# 4. Full logo dark bg
full_dark = Image.new("RGB", (1200, 300), BG)
full_dark.paste(full, (0, 0), full)
full_dark.save("demo/assets/logo-full-dark.png")
print("Created: logo-full-dark.png")

# 5. Full logo white bg
full_white = Image.new("RGB", (1200, 300), (255, 255, 255))
# Redraw with dark text for white bg
full_w = Image.new("RGBA", (1200, 300), (0, 0, 0, 0))
dw = ImageDraw.Draw(full_w)
icon_sm = create_logo_icon(280)
full_w.paste(icon_sm, (10, 10), icon_sm)
try:
    tf = ImageFont.truetype(FONT_BOLD, 90)
    sf = ImageFont.truetype(FONT_REG, 24)
except Exception:
    tf = ImageFont.load_default()
    sf = ImageFont.load_default()
dw.text((310, 80), "Agent", fill=(30, 30, 30), font=tf)
bbox = dw.textbbox((0, 0), "Agent", font=tf)
aw = bbox[2] - bbox[0]
dw.text((310 + aw, 80), "Net", fill=ACCENT, font=tf)
dw.text((315, 185), "The Autonomous AI Agent Economy", fill=(100, 100, 100), font=sf)
full_white.paste(full_w, (0, 0), full_w)
full_white.save("demo/assets/logo-full-white.png")
print("Created: logo-full-white.png")

# 6. Favicon
fav = create_logo_icon(64)
fav_rgb = Image.new("RGB", (64, 64), BG)
fav_rgb.paste(fav, (0, 0), fav)
fav_rgb.save("demo/assets/favicon.png")
print("Created: favicon.png")

print("\nAll logos saved to demo/assets/")
