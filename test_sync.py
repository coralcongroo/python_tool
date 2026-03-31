#!/usr/bin/env python3
"""
test_sync.py - UDP 多设备 LED 同步系统主机端测试工具

功能
----
1. 监听同步包（master 广播）：解析并统计时序精度
2. 模拟 master：向局域网广播同步包（用于测试从机无需真实硬件）
3. 压力测试：模拟多个从机计算并对比帧颜色，验证同步一致性
4. Pixel Push：由 PC 实时推送整帧 RGB 数据

用法
----
    python3 test_sync.py listen [--port 12345]
    python3 test_sync.py master [--port 12345] [--mode 0] [--speed 0.2] [--brightness 128]
    python3 test_sync.py verify
    python3 test_sync.py push --effect rainbow --fps 30 --leds 462

依赖：仅 Python 3 标准库，无需额外安装。
"""

import argparse
import gc
import math
import socket
import struct
import sys
import time
import random

# ─────────────────────────────────────────────────────────────────────────────
#  Packet layout (must mirror time_sync.h sync_packet_t, __attribute__((packed)))
#  Fields: magic(u32) seq(u32) master_us(i64) speed(f32) mode(u8) brightness(u8)
#          color_r(u8) color_g(u8) color_b(u8)
# ─────────────────────────────────────────────────────────────────────────────
PACKET_FMT   = "<IIqfBBBBB"     # little-endian, packed
PACKET_SIZE  = struct.calcsize(PACKET_FMT)
SYNC_MAGIC   = 0x4E59534C     # "LSYN" little-endian
PIXEL_MAGIC  = 0x50504550     # "LEPP" little-endian
PIXEL_HDR_FMT = "<IIqH"        # magic seq frame_us led_count  (18 bytes)
PIXEL_HDR_SIZE = struct.calcsize(PIXEL_HDR_FMT)  # == 18

EFFECT_NAMES = {0: "RAINBOW", 1: "BREATHING", 2: "RUNNING_COLOR", 3: "SPARKLE"}


def uint8_arg(value: str) -> int:
    v = int(value)
    if not (0 <= v <= 255):
        raise argparse.ArgumentTypeError("value must be in range 0..255")
    return v


def positive_int_arg(value: str) -> int:
    v = int(value)
    if v <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return v


def min_one_int_arg(value: str) -> int:
    v = int(value)
    if v < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return v

def pack_packet(seq: int, master_us: int, mode: int, speed: float,
                brightness: int, r: int, g: int, b: int) -> bytes:
    return struct.pack(PACKET_FMT,
                       SYNC_MAGIC, seq, master_us, speed,
                       mode, brightness, r, g, b)

def pack_pixel_packet(seq: int, frame_us: int, rgb_data) -> bytes:
    """
    Build a pixel-push UDP packet.
    rgb_data: bytes/bytearray/memoryview of raw RGB bytes, or list of (r,g,b).
    Wire format: PIXEL_HDR_FMT + raw RGB bytes
    """
    if isinstance(rgb_data, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(rgb_data)
        if len(raw_bytes) % 3 != 0:
            raise ValueError("raw rgb byte length must be multiple of 3")
        led_count = len(raw_bytes) // 3
        if led_count > 65535:
            raise ValueError("led_count must be <= 65535 for uint16 packet field")
        hdr = struct.pack(PIXEL_HDR_FMT, PIXEL_MAGIC, seq, frame_us, led_count)
        return hdr + raw_bytes

    led_count = len(rgb_data)
    if led_count > 65535:
        raise ValueError("led_count must be <= 65535 for uint16 packet field")
    hdr = struct.pack(PIXEL_HDR_FMT, PIXEL_MAGIC, seq, frame_us, led_count)
    raw = bytearray(led_count * 3)
    for i, (r, g, b) in enumerate(rgb_data):
        raw[i * 3]     = r & 0xFF
        raw[i * 3 + 1] = g & 0xFF
        raw[i * 3 + 2] = b & 0xFF
    return hdr + bytes(raw)


def render_frame_bytes(effect: str, t_rel: float, led_count: int, out_buf: bytearray,
                       speed: float, brightness: int,
                       r: int, g: int, b: int,
                       r2: int, g2: int, b2: int,
                       tail: int, gradient_width: int, fire_sim: "FireEffect"):
    """Render selected effect directly into out_buf as packed RGB bytes."""
    n3 = led_count * 3

    if effect == "solid":
        for i in range(0, n3, 3):
            out_buf[i] = r
            out_buf[i + 1] = g
            out_buf[i + 2] = b
        return

    if effect == "breathing":
        phase = (math.sin(t_rel * speed * math.tau) + 1.0) / 2.0
        scale = phase * (brightness / 255.0)
        rr = int(r * scale)
        gg = int(g * scale)
        bb = int(b * scale)
        for i in range(0, n3, 3):
            out_buf[i] = rr
            out_buf[i + 1] = gg
            out_buf[i + 2] = bb
        return

    if effect == "gradient":
        off = (t_rel * speed) % 1.0
        w = gradient_width if gradient_width > 1 else 1
        for i in range(led_count):
            # Lower spatial frequency continuously (no hard block boundaries).
            x = (((i / led_count) / w) + off) % 1.0
            j = i * 3
            out_buf[j] = int(r * (1 - x) + r2 * x)
            out_buf[j + 1] = int(g * (1 - x) + g2 * x)
            out_buf[j + 2] = int(b * (1 - x) + b2 * x)
        return

    if effect == "chase":
        for i in range(n3):
            out_buf[i] = 0
        pos = int((t_rel * speed * led_count) % led_count)
        bv = brightness / 255.0
        for k in range(tail):
            idx = (pos - k) % led_count
            fade = (1.0 - k / tail) * bv
            j = idx * 3
            out_buf[j] = int(r * fade)
            out_buf[j + 1] = int(g * fade)
            out_buf[j + 2] = int(b * fade)
        return

    if effect == "fire" and fire_sim is not None:
        fire_sim.step()
        pixels = fire_sim.render()
        for i, (rr, gg, bb) in enumerate(pixels):
            j = i * 3
            out_buf[j] = rr
            out_buf[j + 1] = gg
            out_buf[j + 2] = bb
        return

    # rainbow (default)
    bv = brightness / 255.0
    for i in range(led_count):
        rr, gg, bb = hsv_to_rgb(((t_rel * speed + i / led_count) % 1.0) * 360.0,
                                1.0, bv)
        j = i * 3
        out_buf[j] = rr
        out_buf[j + 1] = gg
        out_buf[j + 2] = bb

def unpack_packet(data: bytes):
    if len(data) != PACKET_SIZE:
        return None
    fields = struct.unpack(PACKET_FMT, data)
    magic, seq, master_us, speed, mode, brightness, r, g, b = fields
    if magic != SYNC_MAGIC:
        return None
    return dict(seq=seq, master_us=master_us, speed=speed,
                mode=mode, brightness=brightness, r=r, g=g, b=b)

# ─────────────────────────────────────────────────────────────────────────────
#  Colour maths  (mirrors led_effects.cpp — shared by verify and push modes)
# ─────────────────────────────────────────────────────────────────────────────
def hsv_to_rgb(h: float, s: float, v: float):
    if s <= 0:
        x = int(v * 255)
        return x, x, x
    hh = (h % 360) / 60.0
    i  = int(hh)
    ff = hh - i
    p  = v * (1 - s)
    q  = v * (1 - s * ff)
    t  = v * (1 - s * (1 - ff))
    rgb = [(v,t,p),(q,v,p),(p,v,t),(p,q,v),(t,p,v),(v,p,q)][i % 6]
    return tuple(int(c * 255) for c in rgb)

def compute_pixel_rainbow(global_us: int, speed: float, brightness: int,
                           led_index: int, led_count: int):
    speed = max(speed, 1e-3)
    period = int(1e6 / speed)
    t = (global_us % period) / period
    bv = brightness / 255.0
    hue = ((t + led_index / led_count) % 1.0) * 360.0
    return hsv_to_rgb(hue, 1.0, bv)


# ─────────────────────────────────────────────────────────────────────────────
#  PC-side effects for pixel-push mode
# ─────────────────────────────────────────────────────────────────────────────

def effect_solid(t: float, led_count: int, r: int, g: int, b: int, **_):
    return [(r, g, b)] * led_count


def effect_rainbow(t: float, led_count: int, speed: float,
                   brightness: int, **_):
    bv = brightness / 255.0
    return [hsv_to_rgb(((t * speed + i / led_count) % 1.0) * 360.0, 1.0, bv)
            for i in range(led_count)]


def effect_breathing(t: float, led_count: int, speed: float,
                     brightness: int, r: int, g: int, b: int, **_):
    phase = (math.sin(t * speed * math.tau) + 1.0) / 2.0
    scale = phase * brightness / 255.0
    c = (int(r * scale), int(g * scale), int(b * scale))
    return [c] * led_count


def effect_gradient(t: float, led_count: int, speed: float,
                    r: int, g: int, b: int, r2: int, g2: int, b2: int,
                    gradient_width: int = 10, **_):
    """Sweeping gradient between two colours."""
    off = (t * speed) % 1.0
    w = gradient_width if gradient_width > 1 else 1
    pixels = []
    for i in range(led_count):
        x = (((i / led_count) / w) + off) % 1.0
        pixels.append((int(r * (1 - x) + r2 * x),
                       int(g * (1 - x) + g2 * x),
                       int(b * (1 - x) + b2 * x)))
    return pixels


def effect_chase(t: float, led_count: int, speed: float,
                 brightness: int, r: int, g: int, b: int,
                 tail: int = 20, **_):
    """Comet with configurable tail length."""
    pos = int((t * speed * led_count) % led_count)
    bv = brightness / 255.0
    pixels = [(0, 0, 0)] * led_count
    for k in range(tail):
        idx = (pos - k) % led_count
        fade = (1.0 - k / tail) * bv
        pixels[idx] = (int(r * fade), int(g * fade), int(b * fade))
    return pixels


class FireEffect:
    """1-D fire simulation, base of strip is hottest."""

    def __init__(self, led_count: int, cooling: int = 55, sparking: int = 120):
        self.n       = led_count
        self.cooling = cooling
        self.sparking = sparking
        self.heat    = [0.0] * led_count

    def step(self):
        n = self.n
        # Cool down
        for i in range(n):
            cool = random.randint(0, ((self.cooling * 10) // n) + 2)
            self.heat[i] = max(0.0, self.heat[i] - cool / 255.0)
        # Drift heat upward (toward high indices = top of strip)
        for k in range(n - 1, 2, -1):
            self.heat[k] = (self.heat[k - 1] +
                            self.heat[k - 2] * 2) / 3.0
        # Random sparks at bottom
        if random.randint(0, 255) < self.sparking:
            y = random.randint(0, min(6, n - 1))
            self.heat[y] = min(1.0,
                               self.heat[y] + random.randint(160, 255) / 255.0)

    @staticmethod
    def _heat_to_rgb(h: float):
        t = h * 3.0
        if   t < 1.0: return int(t * 255), 0, 0
        elif t < 2.0: return 255, int((t - 1.0) * 255), 0
        else:         return 255, 255, int((t - 2.0) * 255)

    def render(self):
        return [self._heat_to_rgb(h) for h in self.heat]


# ─────────────────────────────────────────────────────────────────────────────
#  PUSH mode
# ─────────────────────────────────────────────────────────────────────────────

def push_pixels(host: str, port: int, fps: int, effect: str, led_count: int,
                speed: float, brightness: int,
                r: int, g: int, b: int,
                r2: int, g2: int, b2: int,
                tail: int, gradient_width: int, cooling: int, sparking: int):
    """
    Broadcast full 462-LED RGB frames at target FPS.
    Devices with fresh pixel data (< 500 ms old) will display it directly,
    overriding their locally-computed effect.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if host == "255.255.255.255":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    dest = (host, port)

    pkt_size = PIXEL_HDR_SIZE + led_count * 3
    if pkt_size > 1472:
        raise SystemExit(
            f"packet too large: {pkt_size}B (>1472B safe UDP payload). "
            f"Reduce --leds; current max is {(1472 - PIXEL_HDR_SIZE) // 3}."
        )
    print(f"[pixel-push]  host={host}  port={port}  effect={effect}  fps={fps}  "
          f"leds={led_count}  packet={pkt_size}B")
    print(f"  按 Ctrl+C 停止\n")

    fire_sim = FireEffect(led_count, cooling, sparking) if effect == "fire" else None

    seq       = 0
    start     = time.monotonic()
    interval  = 1.0 / fps
    next_tick = start
    stat_last = start
    stat_sent = 0
    frame_buf = bytearray(led_count * 3)

    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()

    try:
        while True:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
                now = time.monotonic()
            # If we are far behind schedule, resync to avoid burst-send jitter.
            elif now - next_tick > interval * 2:
                next_tick = now

            t0     = now
            t_rel  = t0 - start          # seconds since start
            frame_us = int(t_rel * 1_000_000)

            render_frame_bytes(effect, t_rel, led_count, frame_buf,
                               speed, brightness, r, g, b, r2, g2, b2,
                               tail, gradient_width, fire_sim)

            pkt = pack_pixel_packet(seq, frame_us, frame_buf)
            sock.sendto(pkt, dest)

            stat_sent += 1
            seq += 1
            next_tick += interval

            # Throttled status output: per-frame print/flush causes host-side jitter.
            if t0 - stat_last >= 1.0:
                dt = t0 - stat_last
                actual_fps = stat_sent / dt if dt > 0 else 0.0
                print(f"  → seq={seq:6d}  t={t_rel:8.3f}s  {len(pkt)}B  fps={actual_fps:5.1f}",
                    end="\r", flush=True)
                stat_last = t0
                stat_sent = 0

    except KeyboardInterrupt:
        print(f"\n  已停止，共发出 {seq} 帧像素数据  ({pkt_size}B/帧)。")
    finally:
        if gc_was_enabled:
            gc.enable()
        sock.close()

def verify_sync_consistency(led_count: int = 462):
    """
    离线验证：两个"设备"拥有不同的本地时间，但在经过时间同步后的
    global_us 误差 ≤ 5ms 时，肉眼可见的颜色差异有多小。
    """
    print("\n──── 同步一致性离线验证 ────")
    print(f"灯珠数: {led_count}  效果: RAINBOW  速度: 0.2 Hz")
    print()

    base_us  = int(time.time() * 1e6)   # 任意基准时间
    offsets  = [0, 1000, 2000, 5000, 10000]  # µs 误差

    print(f"{'误差 (ms)':>10}  {'最大 RGB 差':>12}  {'平均 RGB 差':>12}  {'可感知?':>8}")
    print("-" * 50)

    ref_pixels = [compute_pixel_rainbow(base_us, 0.2, 128, i, led_count)
                  for i in range(led_count)]

    for off_us in offsets:
        cmp_pixels = [compute_pixel_rainbow(base_us + off_us, 0.2, 128, i, led_count)
                      for i in range(led_count)]
        diffs = [max(abs(r[0]-c[0]), abs(r[1]-c[1]), abs(r[2]-c[2]))
                 for r, c in zip(ref_pixels, cmp_pixels)]
        max_diff = max(diffs)
        avg_diff = sum(diffs) / len(diffs)
        perceptible = "是 ⚠" if max_diff > 3 else "否 ✓"
        print(f"{off_us/1000:>10.1f}  {max_diff:>12.1f}  {avg_diff:>12.2f}  {perceptible:>8}")

    print()
    print("结论：误差 < 2ms 时颜色差异 ≤ 3/255，肉眼不可分辨。")

# ─────────────────────────────────────────────────────────────────────────────
#  LISTEN mode
# ─────────────────────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.intervals  = []
        self.drops      = 0
        self.last_seq   = None
        self.last_host  = None   # host time at last packet (seconds)

    def update(self, pkt, host_time):
        if self.last_seq is not None:
            gap = pkt["seq"] - self.last_seq
            if gap > 1:
                self.drops += gap - 1
            if self.last_host is not None:
                self.intervals.append((host_time - self.last_host) * 1000)
        self.last_seq  = pkt["seq"]
        self.last_host = host_time

    def report(self):
        n = len(self.intervals)
        if n < 2:
            return "等待更多包…"
        avg = sum(self.intervals) / n
        mn  = min(self.intervals)
        mx  = max(self.intervals)
        jitter = mx - mn
        return (f"共 {n+1:5d} 包  |  丢包 {self.drops:4d}  |  "
                f"间隔 avg={avg:.1f}ms  min={mn:.1f}ms  max={mx:.1f}ms  "
                f"抖动={jitter:.1f}ms")

def listen(port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    sock.settimeout(2.0)

    print(f"[监听] 等待 UDP 广播包  port={port}  packet_size={PACKET_SIZE}B")
    print(f"       按 Ctrl+C 退出\n")

    stats = Stats()
    try:
        while True:
            try:
                data, addr = sock.recvfrom(256)
            except socket.timeout:
                print("  (2s 内无包，等待中…)", end="\r", flush=True)
                continue

            host_t = time.time()
            pkt = unpack_packet(data)
            if pkt is None:
                print(f"  [!] 收到无效包 from {addr}  ({len(data)}B)")
                continue

            stats.update(pkt, host_t)
            effect = EFFECT_NAMES.get(pkt["mode"], f"?{pkt['mode']}")
            color  = f"rgb({pkt['r']:3d},{pkt['g']:3d},{pkt['b']:3d})"
            print(f"  seq={pkt['seq']:6d}  t={pkt['master_us']/1e6:10.3f}s  "
                  f"mode={effect:<15} speed={pkt['speed']:.2f}Hz  "
                  f"br={pkt['brightness']:3d}  {color}  from {addr[0]}")

            if pkt["seq"] % 10 == 9:
                print(f"  >>> {stats.report()}")
                print()

    except KeyboardInterrupt:
        print(f"\n\n──── 最终统计 ────")
        print(f"  {stats.report()}")
        print(f"  包大小验证: {PACKET_SIZE} B  magic=0x{SYNC_MAGIC:08X} (\"LSYN\")")
    finally:
        sock.close()

# ─────────────────────────────────────────────────────────────────────────────
#  MASTER simulation mode
# ─────────────────────────────────────────────────────────────────────────────
def simulate_master(port: int, mode: int, speed: float,
                    brightness: int, r: int, g: int, b: int,
                    interval_ms: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = ("255.255.255.255", port)

    effect = EFFECT_NAMES.get(mode, f"?{mode}")
    print(f"[模拟 master]  port={port}  mode={effect}  speed={speed}Hz  "
          f"brightness={brightness}  rgb=({r},{g},{b})  interval={interval_ms}ms")
    print(f"  按 Ctrl+C 停止\n")

    seq      = 0
    start    = time.monotonic()
    interval = interval_ms / 1000.0

    try:
        while True:
            t0 = time.monotonic()
            master_us = int((t0 - start) * 1e6)
            pkt = pack_packet(seq, master_us, mode, speed, brightness, r, g, b)
            sock.sendto(pkt, dest)
            print(f"  → seq={seq:6d}  t={master_us/1e6:8.3f}s", end="\r", flush=True)
            seq += 1

            elapsed = time.monotonic() - t0
            wait    = interval - elapsed
            if wait > 0:
                time.sleep(wait)
    except KeyboardInterrupt:
        print(f"\n  已停止，共发出 {seq} 个同步包。")
    finally:
        sock.close()

# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="UDP 多设备 LED 同步测试工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # listen
    p_listen = sub.add_parser("listen", help="监听 master 广播包并统计时序")
    p_listen.add_argument("--port", type=int, default=12345)

    # master
    p_master = sub.add_parser("master", help="模拟 master，向局域网广播同步包")
    p_master.add_argument("--port",       type=int,   default=12345)
    p_master.add_argument("--mode",       type=uint8_arg, default=0,
                          help="0=rainbow 1=breathing 2=running 3=sparkle")
    p_master.add_argument("--speed",      type=float, default=0.2, help="效果速度 Hz")
    p_master.add_argument("--brightness", type=uint8_arg, default=128, help="亮度 0-255")
    p_master.add_argument("--r",          type=uint8_arg, default=0,   help="主色 R")
    p_master.add_argument("--g",          type=uint8_arg, default=180, help="主色 G")
    p_master.add_argument("--b",          type=uint8_arg, default=255, help="主色 B")
    p_master.add_argument("--interval",   type=positive_int_arg, default=100, help="广播间隔 ms")

    # verify
    sub.add_parser("verify", help="离线验证：不同时间误差下彩虹效果颜色差异")

    # push
    p_push = sub.add_parser("push",
                             help="实时推送完整 LED 像素帧（Pixel Push 模式）")
    p_push.add_argument("--host",       default="255.255.255.255",
                        help="目标地址（默认广播 255.255.255.255，可填设备IP做单播）")
    p_push.add_argument("--port",       type=int,   default=12345)
    p_push.add_argument("--fps",        type=positive_int_arg, default=30,
                        help="目标帧率（default: 30）")
    p_push.add_argument("--leds",       type=positive_int_arg, default=462,
                        help="灯珠数量（default: 462）")
    p_push.add_argument("--effect",     default="rainbow",
                        choices=["solid","rainbow","breathing","gradient",
                                 "chase","fire"],
                        help="效果类型（default: rainbow）")
    p_push.add_argument("--speed",      type=float, default=0.2,
                        help="效果速度（Hz 或倍率，default: 0.2）")
    p_push.add_argument("--brightness", type=uint8_arg, default=128,
                        help="亮度 0-255（default: 128）")
    p_push.add_argument("--r",          type=uint8_arg, default=255, help="主色 R")
    p_push.add_argument("--g",          type=uint8_arg, default=100, help="主色 G")
    p_push.add_argument("--b",          type=uint8_arg, default=0,   help="主色 B")
    p_push.add_argument("--r2",         type=uint8_arg, default=0,   help="渐变终止色 R")
    p_push.add_argument("--g2",         type=uint8_arg, default=0,   help="渐变终止色 G")
    p_push.add_argument("--b2",         type=uint8_arg, default=255, help="渐变终止色 B")
    p_push.add_argument("--tail",       type=min_one_int_arg, default=20,
                        help="chase 模式拖尾长度（default: 20）")
    p_push.add_argument("--gradient-width", type=min_one_int_arg, default=10,
                        help="gradient 模式每个颜色步进覆盖灯珠数（default: 10）")
    p_push.add_argument("--cooling",    type=uint8_arg, default=55,
                        help="fire 模式冷却速率（default: 55）")
    p_push.add_argument("--sparking",   type=uint8_arg, default=120,
                        help="fire 模式点火概率 0-255（default: 120）")

    args = parser.parse_args()

    if args.cmd == "listen":
        listen(args.port)
    elif args.cmd == "master":
        simulate_master(args.port, args.mode, args.speed,
                        args.brightness, args.r, args.g, args.b, args.interval)
    elif args.cmd == "verify":
        verify_sync_consistency()
    elif args.cmd == "push":
        push_pixels(args.host, args.port, args.fps, args.effect, args.leds,
                    args.speed, args.brightness,
                    args.r, args.g, args.b,
                    args.r2, args.g2, args.b2,
                    args.tail, args.gradient_width,
                    args.cooling, args.sparking)

if __name__ == "__main__":
    main()
