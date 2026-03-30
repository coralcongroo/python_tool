#!/usr/bin/env python3
"""
test_sync.py — UDP 多设备 LED 同步系统主机端测试工具

功能
────
1. 监听同步包（master 广播）：解析并统计时序精度
2. 模拟 master：向局域网广播同步包（用于测试从机无需真实硬件）
3. 压力测试：模拟多个从机计算并对比帧颜色，验证同步一致性

用法
────
  # 监听模式（需要有一台设备已烧录 master 固件并运行）
  python3 test_sync.py listen [--port 12345]

  # 模拟 master（测试从机固件，无需真实 master 设备）
  python3 test_sync.py master [--port 12345] [--mode 0] [--speed 0.2] [--brightness 128]

  # 颜色一致性验证（离线，验证 compute_frame 逻辑）
  python3 test_sync.py verify

依赖：仅 Python 3 标准库，无需额外安装。
"""

import argparse
import math
import socket
import struct
import sys
import time

# ─────────────────────────────────────────────────────────────────────────────
#  Packet layout (must mirror time_sync.h sync_packet_t, __attribute__((packed)))
#  Fields: magic(u32) seq(u32) master_us(i64) speed(f32) mode(u8) brightness(u8)
#          color_r(u8) color_g(u8) color_b(u8)
# ─────────────────────────────────────────────────────────────────────────────
PACKET_FMT   = "<IIqfBBBBB"     # little-endian, packed
PACKET_SIZE  = struct.calcsize(PACKET_FMT)
SYNC_MAGIC   = 0x4E59534C     # "LSYN" little-endian

EFFECT_NAMES = {0: "RAINBOW", 1: "BREATHING", 2: "RUNNING_COLOR", 3: "SPARKLE"}

def pack_packet(seq: int, master_us: int, mode: int, speed: float,
                brightness: int, r: int, g: int, b: int) -> bytes:
    return struct.pack(PACKET_FMT,
                       SYNC_MAGIC, seq, master_us, speed,
                       mode, brightness, r, g, b)

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
#  Colour maths  (mirrors led_effects.cpp — used for offline verification)
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
    p_master.add_argument("--mode",       type=int,   default=0,   help="0=rainbow 1=breathing 2=running 3=sparkle")
    p_master.add_argument("--speed",      type=float, default=0.2, help="效果速度 Hz")
    p_master.add_argument("--brightness", type=int,   default=128, help="亮度 0-255")
    p_master.add_argument("--r",          type=int,   default=0,   help="主色 R")
    p_master.add_argument("--g",          type=int,   default=180, help="主色 G")
    p_master.add_argument("--b",          type=int,   default=255, help="主色 B")
    p_master.add_argument("--interval",   type=int,   default=100, help="广播间隔 ms")

    # verify
    sub.add_parser("verify", help="离线验证：不同时间误差下彩虹效果颜色差异")

    args = parser.parse_args()

    if args.cmd == "listen":
        listen(args.port)
    elif args.cmd == "master":
        simulate_master(args.port, args.mode, args.speed,
                        args.brightness, args.r, args.g, args.b, args.interval)
    elif args.cmd == "verify":
        verify_sync_consistency()

if __name__ == "__main__":
    main()
