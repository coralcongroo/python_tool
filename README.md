# Python 测试脚本说明

本文档说明 `test_sync.py` 的用途、参数和常见排查方法。

## 1. 脚本作用

`test_sync.py` 用于测试 UDP 多设备灯带同步系统，包含 4 个子命令：

- `listen`：监听并解析主设备同步包
- `master`：在 PC 上模拟主设备广播同步包
- `verify`：离线验证时间误差对颜色一致性的影响
- `push`：实时推送整帧 RGB 像素数据（Pixel Push）

## 2. 环境要求

- Python 3.8+
- 仅使用标准库（无需额外安装依赖）

## 3. 文件位置

- 脚本：`test_sync.py`
- 推荐在脚本所在目录执行，或使用绝对路径调用

示例：

```bash
python3 /home/ats/git-code/test/python_tool/test_sync.py --help
```

## 4. 子命令说明

### 4.1 listen

监听主设备发出的同步包并打印统计信息。

```bash
python3 test_sync.py listen --port 12345
```

常用参数：

- `--port`：UDP 端口（默认 `12345`）

---

### 4.2 master

在 PC 上模拟主设备，定时广播同步参数包。

```bash
python3 test_sync.py master --port 12345 --mode 0 --speed 0.2 --brightness 128
```

常用参数：

- `--mode`：效果编号（0/1/2/3）
- `--speed`：效果速度（Hz）
- `--brightness`：亮度（0~255）
- `--r --g --b`：主色
- `--interval`：广播间隔毫秒（必须 > 0）

---

### 4.3 verify

离线评估时间误差下颜色差异，便于判断同步精度是否满足视觉要求。

```bash
python3 test_sync.py verify
```

---

### 4.4 push（重点）

从 PC 按帧发送完整像素数据到设备，从机收到后直接显示。

```bash
python3 test_sync.py push --host 设备IP --effect gradient --fps 45 --leds 462
```

常用参数：

- `--host`：目标地址
  - `255.255.255.255` 为广播
  - 设备 IP 为单播（更推荐，通常更稳定）
- `--port`：UDP 端口（默认 `12345`）
- `--fps`：发送帧率（必须 > 0）
- `--leds`：灯珠数量（必须 > 0）
- `--effect`：`solid/rainbow/breathing/gradient/chase/fire`
- `--speed`：效果速度
- `--brightness`：亮度（0~255）
- `--r --g --b`：主色
- `--r2 --g2 --b2`：渐变终止色
- `--tail`：追光拖尾长度（`chase`）
- `--gradient-width`：渐变宽度（每组覆盖灯珠数，默认 `10`）
- `--cooling --sparking`：火焰参数（`fire`）

## 5. 渐变效果建议

如果使用 `gradient`：

- 建议先用 `--fps 45`
- 建议从 `--gradient-width 8~12` 区间调节
- 若网络较差，优先单播而不是广播

示例：

```bash
python3 test_sync.py push \
  --host 192.168.1.88 \
  --effect gradient \
  --gradient-width 10 \
  --fps 45 \
  --leds 462
```

## 6. 常见问题

### 6.1 发送后灯无反应

检查顺序：

1. 固件是否已烧录到最新版本
2. 设备是否处于从机模式
3. `--port` 与固件端口是否一致（默认 12345）
4. `--leds` 是否与设备端 `LED_COUNT` 一致
5. 单播时 IP 是否正确

### 6.2 报文过大报错

脚本限制单包安全载荷（1472B），会提示 `packet too large`。

解决方式：

- 减小 `--leds`
- 或降低协议单包大小（需要改固件协议，不建议测试阶段修改）

### 6.3 动画不够顺滑

建议优先级：

1. 使用单播 `--host 设备IP`
2. `--fps` 设为 `45`
3. 调整 `--gradient-width`（例如 8、10、12）
4. 确认设备端已启用队列+按 `frame_us` 对齐播放版本

## 7. 快速命令集合

```bash
# 查看帮助
python3 test_sync.py --help

# 监听同步包
python3 test_sync.py listen --port 12345

# 模拟主设备
python3 test_sync.py master --mode 0 --speed 0.2 --brightness 128

# 离线验证
python3 test_sync.py verify

# 像素推送（推荐单播）
python3 test_sync.py push --host 192.168.1.88 --effect breathing --fps 45 --leds 462
```
