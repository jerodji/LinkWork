"""生成 LinkWork 的图标资源。

产物：
  - extension/icons/{16,32,48,128}.png  （浏览器扩展图标）
  - 可选：scripts/assets/LinkWork.icns  （macOS 应用图标，需 iconutil）

不依赖 Pillow：用标准库 zlib/struct 手写 PNG。
图形：圆角方块 + 链条（链接）+ 文本行（文案）。

用法：
  python scripts/make_icon.py            只生成扩展图标
  python scripts/make_icon.py --icns     同时生成 macOS .icns
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().parent / "assets"
ICONSET = ASSETS / "LinkWork.iconset"
ICNS = ASSETS / "LinkWork.icns"

# 品牌配色
BG_TOP = (0x2B, 0x6C, 0xF6)
BG_BOTTOM = (0x1E, 0x3F, 0xB8)
LINK = (0xFF, 0xFF, 0xFF)
LINE = (0xD8, 0xE4, 0xFF)

SS = 3  # 超采样倍数，用于抗锯齿


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def blend(dst: tuple[float, float, float, float], src: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """src 覆盖在 dst 之上（均为预乘直通颜色，alpha 直接混合）。"""
    sa = src[3]
    if sa <= 0:
        return dst
    da = dst[3]
    out_a = sa + da * (1 - sa)
    if out_a <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    out = tuple((src[i] * sa + dst[i] * da * (1 - sa)) / out_a for i in range(3))
    return (out[0], out[1], out[2], out_a)


def rounded_rect_coverage(x: float, y: float, w: float, h: float, radius: float, px: float, py: float) -> float:
    """点 (px,py) 是否落在圆角矩形内，返回 0/1（用于超采样）。"""
    if px < x or px > x + w or py < y or py > y + h:
        return 0.0
    cx = min(max(px, x + radius), x + w - radius)
    cy = min(max(py, y + radius), y + h - radius)
    if (px - cx) ** 2 + (py - cy) ** 2 <= radius**2:
        return 1.0
    return 0.0


def capsule_coverage(x0: float, y0: float, x1: float, y1: float, thickness: float, px: float, py: float) -> float:
    """线段（带圆头）覆盖判定。"""
    dx, dy = x1 - x0, y1 - y0
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return 1.0 if (px - x0) ** 2 + (py - y0) ** 2 <= (thickness / 2) ** 2 else 0.0
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length_sq))
    cx, cy = x0 + t * dx, y0 + t * dy
    return 1.0 if (px - cx) ** 2 + (py - cy) ** 2 <= (thickness / 2) ** 2 else 0.0


def render(size: int) -> bytes:
    """渲染 RGBA 像素数据（size x size）。"""
    hi = size * SS
    buffer = [[(0.0, 0.0, 0.0, 0.0) for _ in range(hi)] for _ in range(hi)]

    pad = hi * 0.06
    radius = hi * 0.22
    for py in range(hi):
        for px in range(hi):
            if rounded_rect_coverage(pad, pad, hi - 2 * pad, hi - 2 * pad, radius, px + 0.5, py + 0.5):
                t = py / hi
                color = (
                    lerp(BG_TOP[0], BG_BOTTOM[0], t) / 255,
                    lerp(BG_TOP[1], BG_BOTTOM[1], t) / 255,
                    lerp(BG_TOP[2], BG_BOTTOM[2], t) / 255,
                    1.0,
                )
                buffer[py][px] = color

    # 链条：两个椭圆环（用粗线段近似）
    cx, cy = hi / 2, hi * 0.44
    ring_w, ring_h = hi * 0.20, hi * 0.13
    thickness = hi * 0.055
    left = (cx - ring_w, cy)
    right = (cx + ring_w, cy)
    for py in range(hi):
        for px in range(hi):
            hit = False
            for ox, oy in (left, right):
                # 椭圆环：外圈内且不在内圈
                nx, ny = (px - ox) / (ring_w * 0.62), (py - oy) / (ring_h * 1.0)
                outer = nx * nx + ny * ny <= 1.0
                nx2, ny2 = (px - ox) / (ring_w * 0.34), (py - oy) / (ring_h * 0.62)
                inner = nx2 * nx2 + ny2 * ny2 <= 1.0
                if outer and not inner:
                    hit = True
                    break
            # 中间连接杆
            if not hit and capsule_coverage(cx - ring_w * 0.55, cy, cx + ring_w * 0.55, cy, thickness * 0.85, px + 0.5, py + 0.5):
                hit = True
            if hit:
                buffer[py][px] = blend(buffer[py][px], (LINK[0] / 255, LINK[1] / 255, LINK[2] / 255, 1.0))

    # 文本行（三条圆角横线）
    line_specs = [(0.30, 0.66, 0.72), (0.30, 0.78, 0.58), (0.30, 0.90, 0.44)]
    line_h = hi * 0.045
    for x0f, yf, x1f in line_specs:
        x0, x1, y = hi * x0f, hi * x1f, hi * yf
        for py in range(hi):
            for px in range(hi):
                if capsule_coverage(x0, y, x1, y, line_h, px + 0.5, py + 0.5):
                    buffer[py][px] = blend(buffer[py][px], (LINE[0] / 255, LINE[1] / 255, LINE[2] / 255, 1.0))

    # 降采样（超采样平均）+ 预乘 alpha 还原
    out = bytearray()
    for py in range(size):
        out.append(0)  # PNG 过滤器字节
        for px in range(size):
            r = g = b = a = 0.0
            for dy in range(SS):
                for dx in range(SS):
                    pr, pg, pb, pa = buffer[py * SS + dy][px * SS + dx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            n = SS * SS
            alpha = a / n
            if alpha > 0:
                r, g, b = r / a, g / a, b / a
            out.extend(
                (
                    int(max(0, min(255, r * 255 + 0.5))),
                    int(max(0, min(255, g * 255 + 0.5))),
                    int(max(0, min(255, b * 255 + 0.5))),
                    int(max(0, min(255, alpha * 255 + 0.5))),
                )
            )
    return bytes(out)


def write_png(path: Path, size: int) -> None:
    raw = render(size)
    compressed = zlib.compress(raw, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    path.write_bytes(png)


def write_extension_icons() -> int:
    """生成浏览器扩展所需的 PNG 图标。"""
    target = ROOT / "extension" / "icons"
    target.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        path = target / f"{size}.png"
        write_png(path, size)
        print(f"  扩展图标 {path.relative_to(ROOT)} ({size}x{size})")
    print(f"✅ 扩展图标已生成：{target}")
    return 0


def write_icns() -> int:
    """生成 macOS 应用图标（需要 iconutil）。"""
    ASSETS.mkdir(parents=True, exist_ok=True)
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True)

    specs = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, name in specs:
        write_png(ICONSET / name, size)

    iconutil = shutil.which("iconutil")
    if not iconutil:
        print("未找到 iconutil（需要 macOS），跳过 .icns", file=sys.stderr)
        shutil.rmtree(ICONSET, ignore_errors=True)
        return 1
    result = subprocess.run(
        [iconutil, "-c", "icns", str(ICONSET), "-o", str(ICNS)], capture_output=True, text=True
    )
    shutil.rmtree(ICONSET, ignore_errors=True)
    if result.returncode != 0:
        print("iconutil 失败：", result.stderr, file=sys.stderr)
        return 1
    print(f"✅ 已生成 {ICNS.relative_to(ROOT)}（{ICNS.stat().st_size // 1024} KB）")
    return 0


def main() -> int:
    want_icns = "--icns" in sys.argv
    print("==> 生成扩展图标")
    code = write_extension_icons()
    if want_icns:
        print("==> 生成 macOS 图标")
        code = max(code, write_icns())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
