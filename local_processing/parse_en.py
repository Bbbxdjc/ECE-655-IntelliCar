import re

# Basic number words
ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9
}

TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19
}

TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90
}

# Complete lookup table (for backward compatibility and fast lookup)
WORDS2NUM = {
    **ONES, **TEENS, **TENS,
    "hundred": 100, "one hundred": 100, "one hundred eighty": 180,
    "forty five": 45, "forty-five": 45,
}

# Configuration constants
MAX_DURATION = 30000  # milliseconds
MIN_DURATION = 0      # milliseconds
# No default values - return None if not recognized


def _num(tok):
    """
    Convert word or digit string to integer.
    Supports compositional parsing: "twenty three", "one hundred twenty", etc.
    """
    t = tok.lower().strip().replace("-", " ")
    
    # Try direct digit
    if t.isdigit():
        return int(t)
    
    # Try direct lookup
    if t in WORDS2NUM:
        return WORDS2NUM[t]
    
    # Try compositional parsing (e.g., "twenty three")
    parts = t.split()
    if len(parts) == 0:
        return None
    
    # Handle "hundred" constructions
    if "hundred" in parts:
        result = 0
        i = 0
        while i < len(parts):
            if parts[i] == "hundred":
                result *= 100
                i += 1
            elif parts[i] in ONES:
                result += ONES[parts[i]]
                i += 1
            elif parts[i] in TENS:
                result += TENS[parts[i]]
                i += 1
            elif parts[i] in TEENS:
                result += TEENS[parts[i]]
                i += 1
            else:
                i += 1
        if result > 0:
            return result
    
    # Handle tens + ones (e.g., "twenty three")
    if len(parts) == 2:
        if parts[0] in TENS and parts[1] in ONES:
            return TENS[parts[0]] + ONES[parts[1]]
        if parts[0] in ONES and parts[1] in TENS:
            return ONES[parts[0]] + TENS[parts[1]]
    
    # Handle single word
    if len(parts) == 1:
        if parts[0] in ONES:
            return ONES[parts[0]]
        if parts[0] in TENS:
            return TENS[parts[0]]
        if parts[0] in TEENS:
            return TEENS[parts[0]]
    
    return None


def _extract_speed(text: str):
    """
    Extract speed level from text.
    Supports: "speed L0/L1/L2/L3", "level 0/1/2/3", "at speed L2", etc.
    Returns: "L0", "L1", "L2", "L3", or None
    """
    # Match patterns like: "speed L2", "level 2", "at speed L1"
    m = re.search(r"(?:at\s+)?(?:speed|level)\s+(?:L)?([0-3])", text, re.IGNORECASE)
    if m:
        level = m.group(1)
        return f"L{level}"
    return None


def to_structured(text: str, debug=False):
    """
    Parse English command text into structured JSON format.
    
    Args:
        text: Input text from speech recognition
        debug: If True, print detailed parsing steps
    
    Returns format compatible with cloud processing:
    {
        "action": "forward|backward|left|right|stop",
        "duration_ms": int|null,
        "angle_deg": int|null,
        "speed": str|null
    }
    """
    if debug:
        print(f"  ├─ 输入文本: '{text}'")
    
    t = " ".join(text.lower().split())
    
    if debug:
        print(f"  ├─ 规范化后: '{t}'")
    
    # Extract speed first (applies to all movement commands)
    speed = _extract_speed(t)
    
    if debug:
        print(f"  ├─ 提取速度: {speed if speed else '无'}")
    
    # Default: unrecognized (null)
    out = {
        "action": None,
        "duration_ms": None,
        "angle_deg": None,
        "speed": None
    }
    
    # Check for stop command
    if "stop" in t:
        out["action"] = "stop"
        out["speed"] = None  # Stop command doesn't have speed
        if debug:
            print(f"  ├─ 识别为停止命令")
            print(f"  └─ 输出: {out}")
        return out
    
    # Check for forward/backward (support synonyms: go/move forward)
    has_forward = any(x in t for x in ["forward", "go forward", "move forward"])
    has_backward = any(x in t for x in ["backward", "go backward", "move backward"])
    
    if has_forward or has_backward:
        out["action"] = "forward" if has_forward else "backward"
        
        if debug:
            print(f"  ├─ 识别为: {'前进' if has_forward else '后退'}命令")
        
        # Match number before time units, capture multi-word numbers greedily
        # Stop at time unit or speed/level keywords
        m = re.search(r"(?:(?:go|move)\s+)?(?:forward|backward)(?:\s+for)?\s+(.+?)(?:\s+(?:seconds?|secs?|s)|(?:\s+(?:at\s+)?(?:speed|level))|$)", t)
        if m:
            num_text = m.group(1).strip()
            if debug:
                print(f"  ├─ 正则匹配到数字部分: '{num_text}'")
            dur = _num(num_text)
            if debug:
                print(f"  ├─ 数字解析结果: {dur}")
            
            if dur is not None:
                # Only set if we successfully parsed a number
                duration = int(dur * 1000)
                # Enforce duration bounds
                out["duration_ms"] = max(MIN_DURATION, min(duration, MAX_DURATION))
                
                if debug:
                    if duration != out["duration_ms"]:
                        print(f"  ├─ 时长裁剪: {duration}ms → {out['duration_ms']}ms (边界约束)")
                    else:
                        print(f"  ├─ 最终时长: {out['duration_ms']}ms")
            else:
                if debug:
                    print(f"  ├─ 数字解析失败，时长保持None")
        else:
            if debug:
                print(f"  ├─ 未匹配到数字部分，时长保持None")
        
        out["angle_deg"] = None
        out["speed"] = speed  # Apply extracted speed
        
        if debug:
            print(f"  └─ 输出: {out}")
        
        return out
    
    # Check for left turn
    if "left" in t:
        out["action"] = "left"
        
        if debug:
            print(f"  ├─ 识别为: 左转命令")
        
        # Match number before angle units, capture multi-word numbers greedily
        m = re.search(r"(?:left|turn\s+left)\s+(.+?)(?:\s+(?:degrees?|deg)|(?:\s+(?:at\s+)?(?:speed|level))|$)", t)
        if m:
            num_text = m.group(1).strip()
            if debug:
                print(f"  ├─ 正则匹配到数字部分: '{num_text}'")
            ang = _num(num_text)
            if debug:
                print(f"  ├─ 数字解析结果: {ang}")
            
            if ang is not None:
                # Only set if we successfully parsed a number
                # Enforce angle bounds (1-360)
                out["angle_deg"] = max(1, min(int(ang), 360))
                
                if debug:
                    if int(ang) != out["angle_deg"]:
                        print(f"  ├─ 角度裁剪: {int(ang)}° → {out['angle_deg']}° (边界约束)")
                    else:
                        print(f"  ├─ 最终角度: {out['angle_deg']}°")
            else:
                if debug:
                    print(f"  ├─ 数字解析失败，角度保持None")
        else:
            if debug:
                print(f"  ├─ 未匹配到数字部分，角度保持None")
        
        out["duration_ms"] = None
        out["speed"] = speed  # Apply extracted speed
        
        if debug:
            print(f"  └─ 输出: {out}")
        
        return out
    
    # Check for right turn
    if "right" in t:
        out["action"] = "right"
        
        if debug:
            print(f"  ├─ 识别为: 右转命令")
        
        # Match number before angle units, capture multi-word numbers greedily
        m = re.search(r"(?:right|turn\s+right)\s+(.+?)(?:\s+(?:degrees?|deg)|(?:\s+(?:at\s+)?(?:speed|level))|$)", t)
        if m:
            num_text = m.group(1).strip()
            if debug:
                print(f"  ├─ 正则匹配到数字部分: '{num_text}'")
            ang = _num(num_text)
            if debug:
                print(f"  ├─ 数字解析结果: {ang}")
            
            if ang is not None:
                # Only set if we successfully parsed a number
                # Enforce angle bounds (1-360)
                out["angle_deg"] = max(1, min(int(ang), 360))
                
                if debug:
                    if int(ang) != out["angle_deg"]:
                        print(f"  ├─ 角度裁剪: {int(ang)}° → {out['angle_deg']}° (边界约束)")
                    else:
                        print(f"  ├─ 最终角度: {out['angle_deg']}°")
            else:
                if debug:
                    print(f"  ├─ 数字解析失败，角度保持None")
        else:
            if debug:
                print(f"  ├─ 未匹配到数字部分，角度保持None")
        
        out["duration_ms"] = None
        out["speed"] = speed  # Apply extracted speed
        
        if debug:
            print(f"  └─ 输出: {out}")
        
        return out

    # Unrecognized command, return null
    if debug:
        print(f"  ├─ 未识别的命令，返回null")
        print(f"  └─ 输出: {out}")
    
    return out
