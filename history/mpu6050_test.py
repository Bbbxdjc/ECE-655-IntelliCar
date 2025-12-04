# mpu6050_test.py
# 一个简单的独立脚本，用于测试 MPU-6050 是否正常工作

import time
import board  # 来自 adafruit-blinka
import busio  # 来自 adafruit-blinka
import adafruit_mpu6050

print("正在查找 MPU-6050 传感器...")

try:
    # 1. 初始化 I2C
    i2c = busio.I2C(board.SCL, board.SDA)
    
    # 2. 初始化 MPU-6050 对象
    mpu = adafruit_mpu6050.MPU6050(i2c)
    
    print("MPU-6050 连接成功!")
    print("---------------------------------")
    print("现在将每秒读取一次数据。")
    print("请尝试移动和旋转传感器。")
    print("按 Ctrl+C 停止。\n")
    
    # 延迟1秒钟准备
    time.sleep(1)

except Exception as e:
    print(f"\n[错误] 无法初始化 MPU-6050: {e}")
    print("请检查：")
    print("  1. 硬件连接 (SDA, SCL, VCC, GND)")
    print("  2. 是否在 raspi-config 中启用了 I2C")
    print("  3. 是否运行了 'sudo i2cdetect -y 1' 并看到了 '68'")
    # 如果找不到设备，退出程序
    exit()


# 3. 主循环，持续读取数据
try:
    while True:
        # --- 读取加速度数据 (m/s^2) ---
        accel_x, accel_y, accel_z = mpu.acceleration
        print(f"加速度 (X, Y, Z): ({accel_x:6.2f}, {accel_y:6.2f}, {accel_z:6.2f}) m/s^2")

        # --- 读取角速度数据 (rad/s) ---
        gyro_x, gyro_y, gyro_z = mpu.gyro
        # 注意: 库默认返回 弧度/秒 (rad/s)
        print(f"角速度 (X, Y, Z): ({gyro_x:6.2f}, {gyro_y:6.2f}, {gyro_z:6.2f}) rad/s")

        # --- 读取温度数据 (°C) ---
        temp = mpu.temperature
        print(f"温度: {temp:.2f} °C")
        
        print("---------------------------------")
        
        # 每 0.5 秒打印一次
        time.sleep(0.5)

except KeyboardInterrupt:
    print("\n测试停止。程序退出。")