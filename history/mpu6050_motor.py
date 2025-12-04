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
    print("正在标定陀螺仪... 请保持小车完全静止...")
    gyro_z_sum = 0
    # 读取 2000 次数据
    for _ in range(2000):
        gyro_z_sum += mpu.gyro[2] # [2] 对应 Z 轴
        time.sleep(0.001) # 1ms
    
    # 计算平均偏移量
    gyro_offset_z = gyro_z_sum / 2000
    print(f"标定完成。Z轴偏移量: {gyro_offset_z_rads} rad/s")
    return gyro_offset_z

# ========================================
# 5. 精确转弯函数 (核心)
# ========================================

# 我们需要一个全局变量来存储偏移量
gyro_offset_z_rads = 0

def turn_precisely(target_angle_degrees, turn_speed):
    """
    使用陀螺仪精确转弯
    :param target_angle_degrees: 目标角度 (正数=右转, 负数=左转)
    :param turn_speed: 转弯速度 (0-100)
    """
    print(f"准备精确转弯 {target_angle_degrees} 度...")
    
    total_angle_turned = 0
    last_time = time.time()
    
    # 决定转弯方向
    if target_angle_degrees > 0:
        turn_function = turn_right
    else:
        turn_function = turn_left
        
    # 开始转弯
    turn_function(turn_speed)
    
    # --- 闭环控制 ---
    while abs(total_angle_turned) < abs(target_angle_degrees):
        current_time = time.time()
        time_delta = current_time - last_time
        last_time = current_time
        
        # 1. 读取 Z 轴角速度 (弧度/秒)
        gyro_z_rads = mpu.gyro[2]
        
        # 2. 减去偏移量
        corrected_gyro_z_rads = gyro_z_rads - gyro_offset_z_rads
        
        # 3. 计算转过的角度 (积分)
        angle_change_rads = corrected_gyro_z_rads * time_delta
        
        # 4. 转换为角度 (Degrees)
        angle_change_degrees = math.degrees(angle_change_rads)
        
        # 5. 累加总角度
        total_angle_turned += angle_change_degrees
        
        # (可选) 打印调试信息
        # print(f"已转动: {total_angle_turned:.2f} 度")

        # 循环不要太快
        time.sleep(0.005) 
        
    # 6. 达到目标角度，立即停止
    stop_car()
    print(f"转弯完毕! 实际转动: {total_angle_turned:.2f} 度")


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


    turn_precisely(90, TURN_SPEED) # 调用我们的新函数！
    time.sleep(2) # 转弯后停顿
    turn_precisely(90, TURN_SPEED) # 调用我们的新函数！
    time.sleep(2) # 转弯后停顿
    turn_precisely(90, TURN_SPEED) # 调用我们的新函数！
    time.sleep(2) # 转弯后停顿
    turn_precisely(90, TURN_SPEED) # 调用我们的新函数！


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