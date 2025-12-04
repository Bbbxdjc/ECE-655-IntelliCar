# reset_gpio.py
import RPi.GPIO as GPIO

print("正在强制重置所有GPIO引脚...")

try:
    # 必须使用 BCM 模式来匹配你主程序的模式
    GPIO.setmode(GPIO.BCM) 
    GPIO.cleanup()
    print("GPIO 已清理完毕。")

except Exception as e:
    print(f"重置时出错: {e}")
    print("尝试直接清理...")
    GPIO.cleanup()
    print("清理完成。")