"""
Simplified English command parser for voice-controlled car.
Converts recognized text into structured JSON commands.

Since grammar is strictly limited, only handles numbers that appear in GRAMMAR_EXTENDED.
"""

import re

# Simple number mapping (only numbers in grammar)
NUMBER_WORDS = {
    # Basic numbers
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    # Common values
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty five": 45, "forty-five": 45,
    "sixty": 60, "ninety": 90,
    "one hundred": 100, "one hundred eighty": 180,
}

# Configuration
MAX_DURATION_MS = 30000  # 30 seconds max
MAX_ANGLE_DEG = 360      # 360 degrees max


def _parse_number(text):
    """
    Extract number from text.
    Returns integer or None if not found.
    """
    text = text.lower().strip()
    
    # Try direct digit
    if text.isdigit():
        return int(text)
    
    # Try word lookup (exact match first)
    if text in NUMBER_WORDS:
        return NUMBER_WORDS[text]
    
    # Try with normalized spaces and hyphens
    normalized = text.replace("-", " ").strip()
    if normalized in NUMBER_WORDS:
        return NUMBER_WORDS[normalized]
    
    return None


def to_structured(text, debug=False):
    """
    Parse English command text into structured JSON format.
    
    Args:
        text: Input text from speech recognition
        debug: If True, print detailed parsing steps
    
    Returns:
        {
            "action": "forward|backward|left|right|stop|null",
            "duration_ms": int|null,
            "angle_deg": int|null,
            "speed": null  (always null, not used)
        }
    """
    if debug:
        print(f"  ├─ 输入: '{text}'")
    
    # Normalize
    t = " ".join(text.lower().split())
    
    if debug:
        print(f"  ├─ 规范化: '{t}'")
    
    # Default output (all null)
    result = {
        "action": None,
        "duration_ms": None,
        "angle_deg": None,
        "speed": None
    }
    
    # Check for stop
    if "stop" in t:
        result["action"] = "stop"
        if debug:
            print(f"  ├─ 识别: 停止")
            print(f"  └─ 输出: {result}")
        return result
    
    # Check for forward/backward
    if "forward" in t:
        result["action"] = "forward"
        
        if debug:
            print(f"  ├─ 识别: 前进")
        
        # Extract duration: match number before time unit
        match = re.search(r"forward\s+(.+?)\s+(?:second|sec)s?", t)
        if match:
            num_text = match.group(1).strip()
            num = _parse_number(num_text)
            
            if debug:
                print(f"  ├─ 数字文本: '{num_text}'")
                print(f"  ├─ 解析结果: {num}")
            
            if num is not None:
                duration = num * 1000  # convert to milliseconds
                result["duration_ms"] = min(duration, MAX_DURATION_MS)
                
                if debug:
                    if duration != result["duration_ms"]:
                        print(f"  ├─ 时长裁剪: {duration}ms → {result['duration_ms']}ms")
                    else:
                        print(f"  ├─ 时长: {result['duration_ms']}ms")
        else:
            if debug:
                print(f"  ├─ 未匹配到时长")
        
        if debug:
            print(f"  └─ 输出: {result}")
        return result
    
    if "backward" in t:
        result["action"] = "backward"
        
        if debug:
            print(f"  ├─ 识别: 后退")
        
        # Extract duration
        match = re.search(r"backward\s+(.+?)\s+(?:second|sec)s?", t)
        if match:
            num_text = match.group(1).strip()
            num = _parse_number(num_text)
            
            if debug:
                print(f"  ├─ 数字文本: '{num_text}'")
                print(f"  ├─ 解析结果: {num}")
            
            if num is not None:
                duration = num * 1000
                result["duration_ms"] = min(duration, MAX_DURATION_MS)
                
                if debug:
                    if duration != result["duration_ms"]:
                        print(f"  ├─ 时长裁剪: {duration}ms → {result['duration_ms']}ms")
                    else:
                        print(f"  ├─ 时长: {result['duration_ms']}ms")
        else:
            if debug:
                print(f"  ├─ 未匹配到时长")
        
        if debug:
            print(f"  └─ 输出: {result}")
        return result
    
    # Check for left turn
    if "left" in t:
        result["action"] = "left"
        
        if debug:
            print(f"  ├─ 识别: 左转")
        
        # Extract angle: match number before angle unit
        match = re.search(r"left\s+(.+?)\s+(?:degree|deg)s?", t)
        if match:
            num_text = match.group(1).strip()
            num = _parse_number(num_text)
            
            if debug:
                print(f"  ├─ 数字文本: '{num_text}'")
                print(f"  ├─ 解析结果: {num}")
            
            if num is not None:
                result["angle_deg"] = min(max(num, 1), MAX_ANGLE_DEG)
                
                if debug:
                    if num != result["angle_deg"]:
                        print(f"  ├─ 角度裁剪: {num}° → {result['angle_deg']}°")
                    else:
                        print(f"  ├─ 角度: {result['angle_deg']}°")
        else:
            if debug:
                print(f"  ├─ 未匹配到角度")
        
        if debug:
            print(f"  └─ 输出: {result}")
        return result
    
    # Check for right turn
    if "right" in t:
        result["action"] = "right"
        
        if debug:
            print(f"  ├─ 识别: 右转")
        
        # Extract angle
        match = re.search(r"right\s+(.+?)\s+(?:degree|deg)s?", t)
        if match:
            num_text = match.group(1).strip()
            num = _parse_number(num_text)
            
            if debug:
                print(f"  ├─ 数字文本: '{num_text}'")
                print(f"  ├─ 解析结果: {num}")
            
            if num is not None:
                result["angle_deg"] = min(max(num, 1), MAX_ANGLE_DEG)
                
                if debug:
                    if num != result["angle_deg"]:
                        print(f"  ├─ 角度裁剪: {num}° → {result['angle_deg']}°")
                    else:
                        print(f"  ├─ 角度: {result['angle_deg']}°")
        else:
            if debug:
                print(f"  ├─ 未匹配到角度")
        
        if debug:
            print(f"  └─ 输出: {result}")
        return result
    
    # Unrecognized command
    if debug:
        print(f"  ├─ 未识别命令")
        print(f"  └─ 输出: {result}")
    
    return result


# Simple test
if __name__ == "__main__":
    test_cases = [
        "forward five seconds",
        "backward ten seconds",
        "turn left ninety degrees",
        "turn right forty five degrees",
        "stop",
        "hello world",
    ]
    
    print("="*60)
    print("Parse English - Simplified Version Test")
    print("="*60)
    
    for text in test_cases:
        print(f"\nInput: '{text}'")
        result = to_structured(text, debug=True)
        print(f"→ {result}")
    
    print("\n" + "="*60)
