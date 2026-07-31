#!/usr/bin/env python3
"""Generate the concise Co-Analyst download-agent architecture diagram."""

from __future__ import annotations

from html import escape
import math
from pathlib import Path
import shutil
import subprocess


WIDTH = 3000
HEIGHT = 1700
OUT = Path(__file__).with_name("download-agent-aws-full-infrastructure.svg")
OUT_PNG = OUT.with_suffix(".png")


def text(x, y, value, size=23, weight=400, anchor="start", fill="#17202a"):
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'font-family="Arial, Helvetica, sans-serif" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}">{escape(value)}</text>'
    )


def box(x, y, w, h, stroke, fill="#ffffff", radius=24, sw=3, dash=""):
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{dashed}/>'
    )


def card(x, y, w, h, accent, title, badge, fill="#ffffff", title_size=27):
    return "\n".join([
        '<g filter="url(#shadow)">',
        box(x, y, w, h, accent, fill),
        '</g>',
        box(x + 24, y + (h - 82) / 2, 82, 82, accent, "#ffffff", 20, 3),
        text(x + 65, y + h / 2 + 10, badge, 24, 700, "middle", accent),
        text(x + 132, y + h / 2 + 10, title, title_size, 700),
    ])


def arrow_polygon(tip, previous, color):
    tx, ty = tip
    px, py = previous
    dx, dy = tx - px, ty - py
    length = math.hypot(dx, dy) or 1
    ux, uy = dx / length, dy / length
    bx, by = tx - ux * 18, ty - uy * 18
    vx, vy = -uy * 9, ux * 9
    return (
        f'<polygon points="{tx},{ty} {bx + vx},{by + vy} {bx - vx},{by - vy}" '
        f'fill="{color}"/>'
    )


def flow(points, color, both=False, dashed=False, end=True, width=4):
    d = "M " + " L ".join(f"{x} {y}" for x, y in points)
    dash = ' stroke-dasharray="13 10"' if dashed else ""
    result = [
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linecap="round" stroke-linejoin="round"{dash}/>'
    ]
    if both:
        result.append(arrow_polygon(points[0], points[1], color))
    if both or end:
        result.append(arrow_polygon(points[-1], points[-2], color))
    return "\n".join(result)


def step(x, y, number, color=PURPLE if "PURPLE" in globals() else "#7437e6"):
    return "\n".join([
        f'<circle cx="{x}" cy="{y}" r="22" fill="#ffffff" stroke="{color}" stroke-width="4"/>',
        text(x, y + 8, str(number), 21, 700, "middle", color),
    ])


def flow_item(x, y, number, value):
    return "\n".join([
        f'<circle cx="{x}" cy="{y}" r="18" fill="#ffffff" stroke="#7437e6" stroke-width="3"/>',
        text(x, y + 6, str(number), 17, 700, "middle", "#7437e6"),
        text(x + 29, y + 6, value, 17, 600),
    ])


BLUE = "#1473c9"
PURPLE = "#7437e6"
GREEN = "#258b35"
RED = "#df3030"
GRAY = "#5d6d78"
ORANGE = "#dc6b13"

svg = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">
<defs>
  <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#ffffff"/>
    <stop offset="1" stop-color="#f5f7fa"/>
  </linearGradient>
  <linearGradient id="runtime" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#ffffff"/>
    <stop offset="1" stop-color="#f1e8ff"/>
  </linearGradient>
  <filter id="shadow" x="-15%" y="-20%" width="130%" height="150%">
    <feDropShadow dx="0" dy="5" stdDeviation="7" flood-color="#25364d" flood-opacity="0.12"/>
  </filter>
</defs>
<rect width="3000" height="1700" fill="url(#background)"/>
''']

# Account and external boundaries.
svg.append(box(45, 45, 2225, 1605, "#ff7a00", "#ffffff", 34, 4))
svg.append(box(2320, 155, 630, 1495, GRAY, "#fafbfc", 30, 3, "12 10"))
svg.append(text(1155, 112, "AWS Account — Co-Analyst Download Agent", 40, 700, "middle", "#111111"))
svg.append(text(2635, 215, "External Services", 29, 700, "middle", "#37474f"))

# Support paths: deployment, IAM, and logs (not part of invocation numbering).
svg.append(flow([(565, 250), (650, 250), (650, 565), (865, 565), (865, 592)], BLUE))
svg.append(flow([(565, 445), (665, 445), (665, 285), (717, 285)], BLUE))
svg.append(flow([(565, 1035), (660, 1035), (660, 800), (697, 800)], RED, dashed=True))
svg.append(flow([(565, 1080), (625, 1080), (625, 350), (717, 350)], RED, dashed=True))
svg.append(flow([(565, 1125), (1465, 1125), (1465, 790), (1492, 790)], RED, dashed=True))
svg.append(flow([(1383, 800), (2160, 800), (2160, 1170), (2096, 1170)], GREEN))
svg.append(flow([(1000, 195), (1000, 150), (2210, 150), (2210, 1235), (2096, 1235)], GREEN))

# Numbered invocation flow.
# 1. Invoke runtime; 13. return the structured result.
svg.append(flow([(565, 695), (697, 695)], PURPLE))
svg.append(flow([(715, 755), (583, 755)], PURPLE))

# 2. Runtime calls the Vertex-search Lambda for identity and grounded search.
svg.append(flow([(980, 592), (980, 408)], PURPLE, both=True))

# 3. Lambda obtains GCP credentials from Secrets Manager.
svg.append(flow([(1283, 330), (1410, 330), (1410, 450), (1482, 450)], PURPLE, both=True))

# 4. Lambda calls Google Cloud Vertex AI Search and receives grounded URLs.
svg.append(flow([(1283, 350), (1385, 350), (1385, 560), (2240, 560), (2240, 340), (2342, 340)], ORANGE, both=True))

# 5. Runtime uses Bedrock for rewrite, relevance, and vision verification.
svg.append(flow([(1383, 650), (1445, 650), (1445, 255), (1482, 255)], PURPLE, both=True))

# 6. Managed web-search fallback through AgentCore Gateway.
svg.append(flow([(1383, 735), (1492, 735)], PURPLE, both=True))

# 7. Direct official-site search, sitemap, and static crawl.
svg.append(flow([(1383, 810), (1450, 810), (1450, 600), (2240, 600), (2240, 755), (2342, 755)], GRAY, both=True))

# 8. Conditional AgentCore Browser fallback; 9. browser navigation/download.
svg.append(flow([(1250, 858), (1250, 930), (1492, 930)], PURPLE, both=True))
svg.append(flow([(2088, 930), (2260, 930), (2260, 820), (2342, 820)], GRAY, both=True))

# 10. Final authoritative-registry fallback.
svg.append(flow([(1383, 820), (1475, 820), (1475, 1060), (2260, 1060), (2260, 1165), (2342, 1165)], GRAY, both=True))

# 11. Store report/metadata; 12. write provenance.
svg.append(flow([(900, 858), (900, 1092)], GREEN, both=True))
svg.append(flow([(1120, 858), (1120, 1040), (1350, 1040), (1350, 1092)], GREEN, both=True))

# Number markers are rendered above connector lines.
for x, y, number, color in [
    (640, 695, 1, PURPLE),
    (980, 500, 2, PURPLE),
    (1410, 420, 3, PURPLE),
    (2050, 560, 4, ORANGE),
    (1445, 575, 5, PURPLE),
    (1438, 735, 6, PURPLE),
    (2040, 600, 7, GRAY),
    (1390, 930, 8, PURPLE),
    (2260, 870, 9, GRAY),
    (1960, 1060, 10, GRAY),
    (900, 990, 11, GREEN),
    (1240, 1040, 12, GREEN),
    (640, 755, 13, PURPLE),
]:
    svg.append(step(x, y, number, color))

# Service headings only.
svg.append(card(85, 175, 480, 150, BLUE, "Amazon ECR — Download Agent", "ECR", "#f4f9ff", 24))
svg.append(card(85, 370, 480, 150, BLUE, "Amazon ECR — Vertex Search", "ECR", "#f4f9ff", 24))
svg.append(card(85, 650, 480, 150, PURPLE, "AgentCore Runtime Endpoint", "API", "#faf7ff", 24))
svg.append(card(85, 960, 480, 220, RED, "AWS IAM", "IAM", "#fff8f8", 28))

svg.append(card(735, 195, 530, 195, ORANGE, "AWS Lambda — Vertex Search", "λ", "#fff9f2", 26))

svg.append('<g filter="url(#shadow)">')
svg.append(box(715, 610, 650, 230, PURPLE, "url(#runtime)", 30, 4))
svg.append('</g>')
svg.append(box(760, 675, 100, 100, PURPLE, "#ffffff", 24, 4))
svg.append(text(810, 737, "AC", 31, 700, "middle", PURPLE))
svg.append(text(895, 737, "Bedrock AgentCore Runtime", 34, 700))

svg.append(card(1500, 180, 570, 150, PURPLE, "Amazon Bedrock", "AI", "#faf7ff"))
svg.append(card(1500, 375, 570, 150, PURPLE, "AWS Secrets Manager", "KEY", "#faf7ff"))
svg.append(card(1510, 650, 560, 150, PURPLE, "AgentCore Gateway", "GW", "#faf7ff"))
svg.append(card(1510, 855, 560, 150, PURPLE, "AgentCore Browser", "WEB", "#faf7ff"))

svg.append(card(700, 1110, 420, 160, GREEN, "Amazon S3", "S3", "#f5fbf5"))
svg.append(card(1180, 1110, 420, 160, BLUE, "Amazon DynamoDB", "DDB", "#f4f9ff", 24))
svg.append(card(1660, 1110, 420, 160, GREEN, "CloudWatch Logs", "LOG", "#f5fbf5", 24))

svg.append(card(2360, 265, 550, 150, ORANGE, "Google Cloud → Vertex AI Search", "GCP", "#fffaf4", 24))
svg.append(card(2360, 680, 550, 150, GRAY, "Official Web Sources", "WWW", "#ffffff", 25))
svg.append(card(2360, 1090, 550, 150, GRAY, "Authoritative Registries", "REG", "#ffffff", 25))

# Numbered invocation sequence.
svg.append(box(650, 1320, 1420, 270, "#c5cdd4", "#fbfcfd", 20, 2))
svg.append(text(690, 1362, "Invocation flow", 22, 700))

for column_x, items in [
    (690, [(1, "Invoke runtime"), (2, "Vertex Lambda search"),
           (3, "Load GCP credentials"), (4, "Vertex AI Search")]),
    (1035, [(5, "Bedrock verification"), (6, "Gateway fallback"),
            (7, "Official web crawl"), (8, "Browser fallback")]),
    (1380, [(9, "Browser download"), (10, "Registry fallback"),
            (11, "Store report in S3"), (12, "Write provenance")]),
    (1725, [(13, "Return S3 result")]),
]:
    for row, (number, value) in enumerate(items):
        svg.append(flow_item(column_x, 1410 + row * 52, number, value))

svg.append('</svg>')
OUT.write_text("\n".join(svg), encoding="utf-8")
print(OUT)

if sips := shutil.which("sips"):
    subprocess.run(
        [sips, "-s", "format", "png", str(OUT), "--out", str(OUT_PNG)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(OUT_PNG)
