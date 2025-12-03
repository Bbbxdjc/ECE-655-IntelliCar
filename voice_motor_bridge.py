#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Voice-driven motor controller.

This script launches `local_processing/vosk_whisper_llm.py` to capture speech,
parses each JSON line the LLM emits, and immediately drives the motors on the Pi
according to the command payload.
"""

import json
import sys
import time
import signal
import subprocess
import threading
import queue
from pathlib import Path
from typing import Dict, Any, Optional

import RPi.GPIO as GPIO
import math
import board
import busio
import adafruit_mpu6050

# ---------- Paths ----------
REPO_ROOT = Path(__file__).resolve().parent
VOSK_SCRIPT = REPO_ROOT / "local_processing" / "vosk_whisper_llm.py"

# ---------- Motor GPIO ----------
INT1 = 17
INT2 = 18
INT3 = 27
INT4 = 22
ENA = 23
ENB = 24

GPIO.setmode(GPIO.BCM)
GPIO.setup([INT1, INT2, INT3, INT4, ENA, ENB], GPIO.OUT)
pwm_a = GPIO.PWM(ENA, 1000)
pwm_b = GPIO.PWM(ENB, 1000)
pwm_a.start(0)
pwm_b.start(0)

# ---------- MPU-6050 ----------
def _init_imu():
    i2c = busio.I2C(board.SCL, board.SDA)
    return adafruit_mpu6050.MPU6050(i2c)


mpu = _init_imu()
gyro_offset_z = 0.0
stop_guard_dynamic: Optional[float] = None
GYRO_RATE_LIMIT_RAD = math.radians(500)

# ---------- Control parameters ----------
SPEED_TABLE = {
    "L0": 0,
    "L1": 30,
    "L2": 55,
    "L3": 80,
}
DEFAULT_SPEED = "L1"
TURN_SPEED = 60
DEFAULT_DURATION_MS = 1500
SETTLE_GAP_S = 0.2
COMMAND_QUEUE_MAX = 50
MAX_STOP_GUARD_DELTA = 10.0

command_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=COMMAND_QUEUE_MAX)
motor_stop_event = threading.Event()
motor_thread: Optional[threading.Thread] = None


def _limit_guard_adjustment(candidate: float, baseline: float) -> float:
    delta = candidate - baseline
    if delta > MAX_STOP_GUARD_DELTA:
        return baseline + MAX_STOP_GUARD_DELTA
    if delta < -MAX_STOP_GUARD_DELTA:
        return max(0.0, baseline - MAX_STOP_GUARD_DELTA)
    return max(0.0, candidate)

# ---------- Motor primitives ----------
def stop_car():
    pwm_a.ChangeDutyCycle(0)
    pwm_b.ChangeDutyCycle(0)


def move_forward(duty: int):
    GPIO.output(INT1, GPIO.LOW)
    GPIO.output(INT2, GPIO.HIGH)
    GPIO.output(INT3, GPIO.LOW)
    GPIO.output(INT4, GPIO.HIGH)
    pwm_a.ChangeDutyCycle(duty)
    pwm_b.ChangeDutyCycle(duty)


def move_backward(duty: int):
    GPIO.output(INT1, GPIO.HIGH)
    GPIO.output(INT2, GPIO.LOW)
    GPIO.output(INT3, GPIO.HIGH)
    GPIO.output(INT4, GPIO.LOW)
    pwm_a.ChangeDutyCycle(duty)
    pwm_b.ChangeDutyCycle(duty)


def turn_right_raw(duty: int):
    GPIO.output(INT1, GPIO.HIGH)
    GPIO.output(INT2, GPIO.LOW)
    GPIO.output(INT3, GPIO.LOW)
    GPIO.output(INT4, GPIO.HIGH)
    pwm_a.ChangeDutyCycle(duty)
    pwm_b.ChangeDutyCycle(duty)


def turn_left_raw(duty: int):
    GPIO.output(INT1, GPIO.LOW)
    GPIO.output(INT2, GPIO.HIGH)
    GPIO.output(INT3, GPIO.HIGH)
    GPIO.output(INT4, GPIO.LOW)
    pwm_a.ChangeDutyCycle(duty)
    pwm_b.ChangeDutyCycle(duty)


def reconnect_imu():
    global mpu
    try:
        mpu = _init_imu()
        print("🔄 IMU 重新连接成功")
    except Exception as exc:
        print(f"⚠️ IMU 重连失败: {exc}")
        time.sleep(0.5)


def safe_read_gyro_z(retries: int = 3) -> float:
    for attempt in range(retries):
        try:
            return mpu.gyro[2]
        except Exception as exc:
            print(f"⚠️ IMU 读取失败({attempt+1}/{retries}): {exc}")
            reconnect_imu()
    raise RuntimeError("IMU 多次读取失败")


def calibrate_gyro(samples: int = 2000) -> float:
    print("🧭 正在标定陀螺仪，请保持静止...")
    total = 0.0
    for _ in range(samples):
        try:
            total += safe_read_gyro_z()
        except RuntimeError as exc:
            print(f"⚠️ 标定过程中失败: {exc}, 重试当前样本")
            continue
        time.sleep(0.001)
    offset = total / samples
    print(f"🧭 标定完成，Z 轴偏移 {offset:.6f} rad/s")
    return offset


def turn_precisely_deg(angle_deg: int, duty: int):
    """Closed-loop turning using the gyro with ramping, adaptive guard, and settle."""
    global gyro_offset_z, stop_guard_dynamic
    if angle_deg == 0:
        return

    target_abs = abs(angle_deg)
    direction = turn_right_raw if angle_deg > 0 else turn_left_raw
    turn_speed = max(0, min(100, duty))
    min_speed = max(20, turn_speed * 0.35)
    ramp_start = max(15.0, target_abs * 0.35)
    base_stop_guard = min(8.0, max(2.0, target_abs * 0.08))
    tuned_guard = max(base_stop_guard, stop_guard_dynamic) if stop_guard_dynamic is not None else base_stop_guard
    stop_guard = min(tuned_guard, target_abs * 0.7)

    total_angle = 0.0
    last_t = time.monotonic()
    direction(turn_speed)
    loop_count = 0

    while abs(total_angle) < max(0.0, target_abs - stop_guard):
        now = time.monotonic()
        dt = now - last_t
        last_t = now
        try:
            rate = safe_read_gyro_z() - gyro_offset_z
        except RuntimeError as exc:
            print(f"⚠️ 转向过程中 IMU 读取失败: {exc}, 尝试继续")
            reconnect_imu()
            rate = 0.0
        if abs(rate) > 0.9 * GYRO_RATE_LIMIT_RAD:
            print("[WARN] Gyro nearing saturation; 考虑降低转速或调整量程")
        delta_deg = math.degrees(rate * dt)
        total_angle += delta_deg
        remaining = target_abs - abs(total_angle)
        if remaining < ramp_start:
            ratio = max(0.0, remaining / ramp_start)
            current_speed = min_speed + (turn_speed - min_speed) * ratio
        else:
            current_speed = turn_speed
        direction(current_speed)
        loop_count += 1
        if loop_count % 10 == 0:
            print(f"[LOG] gyro_z={rate:.4f} rad/s, angle={abs(total_angle):.2f}°, pwm={current_speed:.1f}%")
        time.sleep(0.005)

    stop_car()

    settle_start = time.monotonic()
    settle_last = settle_start
    settle_duration = 0.4
    stable_samples = 0
    rate_threshold = 0.05

    while time.monotonic() - settle_start < settle_duration and stable_samples < 6:
        now = time.monotonic()
        dt = now - settle_last
        settle_last = now
        try:
            rate = safe_read_gyro_z() - gyro_offset_z
        except RuntimeError as exc:
            print(f"⚠️ 静置阶段 IMU 读取失败: {exc}, 尝试继续")
            reconnect_imu()
            rate = 0.0
        total_angle += math.degrees(rate * dt)
        if abs(rate) < rate_threshold:
            stable_samples += 1
        else:
            stable_samples = 0
        time.sleep(0.01)

    final_angle = abs(total_angle)
    overshoot = final_angle - target_abs
    print(f"转弯完毕! 实际转动: {final_angle:.2f} 度，偏差 {overshoot:+.2f} 度")

    if overshoot > 0.5:
        candidate = min(target_abs * 0.7, stop_guard + overshoot * 0.8)
        clamped = _limit_guard_adjustment(candidate, base_stop_guard)
        note = f" (限制 ±{MAX_STOP_GUARD_DELTA:.0f}°)" if clamped != candidate else ""
        stop_guard_dynamic = clamped
        print(f"[调参] 检测到过冲，stop_guard 调整为 {stop_guard_dynamic:.2f}{note}")
    elif overshoot < -0.5:
        candidate = max(base_stop_guard * 0.5, stop_guard * 0.7)
        clamped = _limit_guard_adjustment(candidate, base_stop_guard)
        note = f" (限制 ±{MAX_STOP_GUARD_DELTA:.0f}°)" if clamped != candidate else ""
        stop_guard_dynamic = clamped
        print(f"[调参] 检测到欠冲，stop_guard 调整为 {stop_guard_dynamic:.2f}{note}")
    else:
        stop_guard_dynamic = stop_guard
        print(f"[调参] stop_guard 保持为 {stop_guard_dynamic:.2f}")


# ---------- Command handling ----------
def duty_from_speed(speed_label: Optional[str]) -> int:
    label = (speed_label or DEFAULT_SPEED).upper()
    return SPEED_TABLE.get(label, SPEED_TABLE[DEFAULT_SPEED])


def execute_command(cmd: Dict[str, Any]):
    action = cmd.get("action")
    speed = duty_from_speed(cmd.get("speed"))
    if action in {"forward", "backward"}:
        duration_ms = cmd.get("duration_ms") or DEFAULT_DURATION_MS
        motion = move_forward if action == "forward" else move_backward
        motion(speed)
        time.sleep(duration_ms / 1000.0)
        stop_car()
    elif action in {"left", "right"}:
        angle = cmd.get("angle_deg") or 90
        signed = angle if action == "right" else -angle
        turn_precisely_deg(signed, TURN_SPEED)
    elif action == "stop":
        stop_car()
    else:
        print(f"⚠️ 未知 action: {action}")
    time.sleep(SETTLE_GAP_S)


def motor_worker():
    while not motor_stop_event.is_set():
        try:
            cmd = command_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if cmd is None:
            command_queue.task_done()
            break
        try:
            execute_command(cmd)
        finally:
            command_queue.task_done()

    while True:
        try:
            cmd = command_queue.get_nowait()
        except queue.Empty:
            break
        if cmd is not None:
            execute_command(cmd)
        command_queue.task_done()


def start_motor_worker():
    global motor_thread
    if motor_thread is not None and motor_thread.is_alive():
        return motor_thread
    motor_stop_event.clear()
    motor_thread = threading.Thread(target=motor_worker, daemon=True)
    motor_thread.start()
    return motor_thread


def stop_motor_worker():
    global motor_thread
    if motor_thread is None:
        motor_stop_event.set()
        return
    motor_stop_event.set()
    inserted = False
    while not inserted:
        try:
            command_queue.put_nowait(None)
            inserted = True
        except queue.Full:
            try:
                command_queue.get_nowait()
                command_queue.task_done()
            except queue.Empty:
                break
    motor_thread.join(timeout=5.0)
    motor_thread = None


def enqueue_motor_command(cmd: Dict[str, Any]):
    while not motor_stop_event.is_set():
        try:
            command_queue.put(cmd, timeout=0.1)
            return True
        except queue.Full:
            continue
    return False


# ---------- Vosk runner ----------
def start_vosk_process():
    if not VOSK_SCRIPT.exists():
        raise SystemExit(f"找不到脚本: {VOSK_SCRIPT}")
    cmd = [sys.executable, "-u", str(VOSK_SCRIPT)]
    print(f"🚀 启动语音识别: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)


def consume_vosk(proc):
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            # 先把 Vosk/LLM 的原始输出透传出来，方便调试
            print(line)
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            keys = {"action", "duration_ms", "angle_deg", "speed"}
            if not keys.issubset(payload.keys()):
                continue
            if "id" not in payload or payload.get("id") is None:
                continue
            seg_id = payload.get("id")
            print(f"🛠️  执行命令 (segment #{seg_id}): {payload}")
            if not enqueue_motor_command(payload):
                print("⚠️ Motor queue stopped; command丢弃", file=sys.stderr)
    finally:
        proc.terminate()
        proc.wait(timeout=3)


# ---------- Cleanup ----------
def cleanup():
    stop_motor_worker()
    stop_car()
    pwm_a.stop()
    pwm_b.stop()
    GPIO.cleanup()


def main():
    global gyro_offset_z
    gyro_offset_z = calibrate_gyro()
    start_motor_worker()
    proc = start_vosk_process()

    def handle_signal(signum, frame):
        print("\n⏹️  收到信号，正在退出...")
        proc.terminate()
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        consume_vosk(proc)
    except KeyboardInterrupt:
        handle_signal(signal.SIGINT, None)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
