# -*- coding: utf-8 -*-
import RPi.GPIO as GPIO
import time
import math

# --- 导入 I2C 和 MPU-6050 库 ---
import board  # from adafruit-blinka
import busio  # from adafruit-blinka
import adafruit_mpu6050

# ========================================
# 1. 初始化 I2C 和 MPU-6050
# ========================================
i2c = busio.I2C(board.SCL, board.SDA)
mpu = adafruit_mpu6050.MPU6050(i2c)
mpu.gyro_range = adafruit_mpu6050.GyroRange.RANGE_500_DPS  # default 250 dps 会饱和
gyro_rate_limit_rad = math.radians(500)

# ========================================
# 2. 电机引脚定义 (与你之前相同)
# ========================================
INT1 = 17  # Board Pin 11
INT2 = 18  # Board Pin 12
INT3 = 27  # Board Pin 13
INT4 = 22  # Board Pin 15

ENA = 23   # Board Pin 16
ENB = 24   # Board Pin 18

# --- 引脚设置 (这里的代码保持不变) ---
GPIO.setup(INT1, GPIO.OUT)
GPIO.setup(INT2, GPIO.OUT)
GPIO.setup(INT3, GPIO.OUT)
GPIO.setup(INT4, GPIO.OUT)
GPIO.setup(ENA, GPIO.OUT)
GPIO.setup(ENB, GPIO.OUT)

# --- PWM对象初始化 (这里的代码也保持不变) ---
p_a = GPIO.PWM(ENA, 1000)
p_b = GPIO.PWM(ENB, 1000)
p_a.start(0)
p_b.start(0)


# ========================================
# 实用日志函数
# ========================================
def log_step_start(step_name):
    wall_ts = time.time()
    mono_ts = time.monotonic()
    print(f"[STEP] {step_name} 开始: {wall_ts:.3f}s (mono {mono_ts:.3f}s)")
    return wall_ts, mono_ts


def log_step_end(step_name, start_pair):
    _, mono_start = start_pair
    wall_ts = time.time()
    mono_ts = time.monotonic()
    duration = mono_ts - mono_start
    print(f"[STEP] {step_name} 结束: {wall_ts:.3f}s (mono {mono_ts:.3f}s, 历时 {duration:.3f}s)")
    return wall_ts, mono_ts


# ========================================
# 3. 基础电机函数 (与你之前相同)
# ========================================
def stop_car():
    p_a.ChangeDutyCycle(0)
    p_b.ChangeDutyCycle(0)

def move_forward(speed):
    GPIO.output(INT1, GPIO.LOW)
    GPIO.output(INT2, GPIO.HIGH)
    GPIO.output(INT3, GPIO.LOW)
    GPIO.output(INT4, GPIO.HIGH)
    p_a.ChangeDutyCycle(speed)
    p_b.ChangeDutyCycle(speed)

def move_backward(speed):
    GPIO.output(INT1, GPIO.HIGH)
    GPIO.output(INT2, GPIO.LOW)
    GPIO.output(INT3, GPIO.HIGH)
    GPIO.output(INT4, GPIO.LOW)
    p_a.ChangeDutyCycle(speed)
    p_b.ChangeDutyCycle(speed)

def turn_left(speed):
    GPIO.output(INT1, GPIO.LOW)
    GPIO.output(INT2, GPIO.HIGH)
    GPIO.output(INT3, GPIO.HIGH)
    GPIO.output(INT4, GPIO.LOW)
    p_a.ChangeDutyCycle(speed)
    p_b.ChangeDutyCycle(speed)

def turn_right(speed):
    GPIO.output(INT1, GPIO.HIGH)
    GPIO.output(INT2, GPIO.LOW)
    GPIO.output(INT3, GPIO.LOW)
    GPIO.output(INT4, GPIO.HIGH)
    p_a.ChangeDutyCycle(speed)
    p_b.ChangeDutyCycle(speed)

# ========================================
# 4. MPU-6050 陀螺仪标定函数
# ========================================
def calibrate_gyro():
    step_timing = log_step_start("陀螺仪标定")
    print("正在标定陀螺仪... 请保持小车完全静止...")
    gyro_z_sum = 0
    # 读取 2000 次数据
    for _ in range(2000):
        gyro_z_sum += mpu.gyro[2] # [2] 对应 Z 轴
        time.sleep(0.001) # 1ms
    
    # 计算平均偏移量
    gyro_offset_z = gyro_z_sum / 2000
    print(f"标定完成。Z轴偏移量: {gyro_offset_z} rad/s")
    log_step_end("陀螺仪标定", step_timing)
    return gyro_offset_z

# ========================================
# 5. 精确转弯函数 (核心)
# ========================================

# 我们需要一个全局变量来存储偏移量
gyro_offset_z_rads = 0
stop_guard_dynamic = None

def turn_precisely(target_angle_degrees, turn_speed):
    """
    使用陀螺仪精确转弯
    :param target_angle_degrees: 目标角度 (正数=右转, 负数=左转)
    :param turn_speed: 转弯速度 (0-100)
    """
    step_label = f"精确转弯 {target_angle_degrees}°"
    step_timing = log_step_start(step_label)
    print(f"准备精确转弯 {target_angle_degrees} 度...")

    total_angle_turned = 0.0
    last_time = time.monotonic()

    # 决定转弯方向，并准备一个函数方便随时调速
    if target_angle_degrees >= 0:
        apply_turn = turn_right
    else:
        apply_turn = turn_left

    global stop_guard_dynamic

    target_abs = abs(target_angle_degrees)
    if target_abs == 0:
        stop_car()
        print("目标角度为0，未执行转弯。")
        log_step_end(step_label, step_timing)
        return

    turn_speed = max(0, min(100, turn_speed))
    min_speed = max(20, turn_speed * 0.35)
    ramp_start = max(15.0, target_abs * 0.35)  # 开始减速的角度
    base_stop_guard = min(8.0, max(2.0, target_abs * 0.08))
    if stop_guard_dynamic is not None:
        tuned_guard = max(base_stop_guard, stop_guard_dynamic)
    else:
        tuned_guard = base_stop_guard
    stop_guard = min(tuned_guard, target_abs * 0.7)  # 预留提前量，抵消惯性

    apply_turn(turn_speed)

    # --- 闭环控制 ---
    loop_count = 0
    while abs(total_angle_turned) < max(0.0, target_abs - stop_guard):
        current_time = time.monotonic()
        time_delta = current_time - last_time
        last_time = current_time

        gyro_z_rads = mpu.gyro[2]
        if abs(gyro_z_rads) > 0.9 * gyro_rate_limit_rad:
            print("[WARN] Gyro nearing saturation; consider减速或更大量程")
        corrected_gyro_z_rads = gyro_z_rads - gyro_offset_z_rads
        angle_change_rads = corrected_gyro_z_rads * time_delta
        angle_change_degrees = math.degrees(angle_change_rads)
        total_angle_turned += angle_change_degrees

        remaining = target_abs - abs(total_angle_turned)

        # 根据剩余角度动态调整速度，避免因惯性超调
        if remaining < ramp_start:
            ratio = max(0.0, remaining / ramp_start)
            current_speed = min_speed + (turn_speed - min_speed) * ratio
        else:
            current_speed = turn_speed

        apply_turn(current_speed)
        loop_count += 1
        if loop_count % 10 == 0:
            print(f"[LOG] gyro_z={gyro_z_rads:.4f} rad/s, angle={total_angle_turned:.2f}°, pwm={current_speed:.1f}%")
        time.sleep(0.005)

    stop_car()

    # 稍作等待并继续积分，捕捉停下后的惯性旋转
    settle_start = time.monotonic()
    settle_last = settle_start
    settle_duration = 0.4
    stable_samples = 0
    rate_threshold = 0.05  # rad/s

    while time.monotonic() - settle_start < settle_duration and stable_samples < 6:
        current_time = time.monotonic()
        time_delta = current_time - settle_last
        settle_last = current_time

        gyro_z_rads = mpu.gyro[2]
        corrected_gyro_z_rads = gyro_z_rads - gyro_offset_z_rads
        angle_change_rads = corrected_gyro_z_rads * time_delta
        total_angle_turned += math.degrees(angle_change_rads)

        if abs(corrected_gyro_z_rads) < rate_threshold:
            stable_samples += 1
        else:
            stable_samples = 0

        time.sleep(0.01)

    final_angle = abs(total_angle_turned)
    overshoot = final_angle - target_abs
    print(f"转弯完毕! 实际转动: {total_angle_turned:.2f} 度，偏差 {overshoot:+.2f} 度")

    # 根据实际偏差调整 stop_guard，逐渐收敛
    if overshoot > 0.5:
        stop_guard_dynamic = min(target_abs * 0.7, stop_guard + overshoot * 0.8)
        print(f"[调参] 检测到过冲，stop_guard 调整为 {stop_guard_dynamic:.2f}")
    elif overshoot < -0.5:
        stop_guard_dynamic = max(base_stop_guard * 0.5, stop_guard * 0.7)
        print(f"[调参] 检测到欠冲，stop_guard 调整为 {stop_guard_dynamic:.2f}")
    else:
        stop_guard_dynamic = stop_guard
        print(f"[调参] stop_guard 保持为 {stop_guard_dynamic:.2f}")

    log_step_end(step_label, step_timing)


# ========================================
# 6. 主程序
# ========================================
try:
    # 启动时必须标定！
    # 确保你的小车放在地上，完全静止！
    gyro_offset_z_rads = calibrate_gyro()
    print("标定完成，2秒后开始...")
    time.sleep(2)

    MOVE_SPEED = 50     # 前进速度 50%
    TURN_SPEED = 60     # 转弯速度 100% (转弯速度慢一点，陀螺仪更准)

    turn_precisely(90, TURN_SPEED)  # 先转90度，测试一下
    time.sleep(2)  # 转弯后停顿
    turn_precisely(90, TURN_SPEED)  # 再转90度，测试一下
    time.sleep(2)  # 转弯后停顿

    print("\n正方形绘制完毕!")
    stop_car()


except KeyboardInterrupt:
    print("程序被用户停止")

finally:
    # 无论如何，最后都要停止PWM并清理GPIO
    print("Cleaning up GPIO...")
    p_a.stop()
    p_b.stop()
    GPIO.cleanup()