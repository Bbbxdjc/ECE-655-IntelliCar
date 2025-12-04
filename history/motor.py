# -*- coding: utf-8 -*-
import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BCM)

# 接口定义
INT1 = 17 # Board Pin 11
INT2 = 18 # Board Pin 12
INT3 = 27 # Board Pin 13
INT4 = 22 # Board Pin 15
ENA = 23  # Board Pin 16
ENB = 24  # Board Pin 18

# ----------------------------------------------------
# --- 修复 1: 添加 initial=GPIO.LOW 防止引脚浮空 ---
# ----------------------------------------------------
GPIO.setup(INT1, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(INT2, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(INT3, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(INT4, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(ENA, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(ENB, GPIO.OUT, initial=GPIO.LOW)
# ----------------------------------------------------

p_a = GPIO.PWM(ENA, 1000)
p_b = GPIO.PWM(ENB, 1000)

# 启动PWM，初始占空比为0 (即停止)
p_a.start(0)
p_b.start(0)

# ----------------------------------------------------
# --- 修复 2: 修改 stop_car 为最安全的速度归零 ---
# ----------------------------------------------------
def stop_car():
    print("Stopping car (Speed 0)...")
    # 将速度设为 0
    p_a.ChangeDutyCycle(0)
    p_b.ChangeDutyCycle(0)

# 封装一个前进函数
def move_forward(speed):
    print("Moving forward...")
    # 先设置方向
    GPIO.output(INT1, GPIO.LOW)
    GPIO.output(INT2, GPIO.HIGH)
    GPIO.output(INT3, GPIO.LOW)
    GPIO.output(INT4, GPIO.HIGH)
    # 再设置速度
    p_a.ChangeDutyCycle(speed)
    p_b.ChangeDutyCycle(speed)
# 封装一个后退函数
def move_backward(speed):
    print("Moving backward...")
    GPIO.output(INT1, GPIO.HIGH)
    GPIO.output(INT2, GPIO.LOW)
    GPIO.output(INT3, GPIO.HIGH)
    GPIO.output(INT4, GPIO.LOW)
    p_a.ChangeDutyCycle(speed)
    p_b.ChangeDutyCycle(speed)
def turn_right(speed):
    print("Turning right...")
    # (你的方向逻辑是 左后退/右前进，这会导致转弯半径大)
    # (更常见的原地转弯是 左前进/右后退)
    # 但我们暂时保持你的逻辑不变：
    GPIO.output(INT1, GPIO.HIGH) 
    GPIO.output(INT2, GPIO.LOW)
    GPIO.output(INT3, GPIO.LOW) 
    GPIO.output(INT4, GPIO.HIGH)
    p_a.ChangeDutyCycle(speed)
    p_b.ChangeDutyCycle(speed)
def turn_left(speed):
    print("Turning left...")
    GPIO.output(INT1, GPIO.LOW)
    GPIO.output(INT2, GPIO.HIGH)
    GPIO.output(INT3, GPIO.HIGH)
    GPIO.output(INT4, GPIO.LOW)
    p_a.ChangeDutyCycle(speed)
    p_b.ChangeDutyCycle(speed)
# --- ----------------- ---
try:
    print("开始测试程序...")
    # ----------------------------------------------------
    # --- 修复 3: 删除了错误的 p_a.ChangeDutyCycle(50) ---
    # ----------------------------------------------------

    # 1. 前进
    move_forward(50)
    time.sleep(0.5) # 前进0.5秒
    stop_car()
    time.sleep(0.2)
    turn_right(50)
    time.sleep(0.3) # 右转0.3秒
    stop_car()

    # 5. 停止
    stop_car()
    print("测试结束。")
except KeyboardInterrupt:
    print("程序被用户停止")

finally:
    print("Cleaning up GPIO...")
    # ----------------------------------------------------
    # --- 修复 4: 在 cleanup 之前安全地停止 PWM ---
    # ----------------------------------------------------
    p_a.stop()
    p_b.stop()
    GPIO.cleanup()