#!/usr/bin/env python3
"""
Unit tests for parse_en.py
Tests number parsing, unit parsing, speed extraction, and command parsing.
"""

from parse_en import to_structured, _num, _extract_speed


def test_number_parsing():
    """Test _num function with various inputs."""
    print("Testing number parsing...")
    
    test_cases = [
        # Basic numbers
        ("five", 5),
        ("ten", 10),
        ("fifteen", 15),
        ("twenty", 20),
        ("ninety", 90),
        
        # Teens
        ("eleven", 11),
        ("twelve", 12),
        ("thirteen", 13),
        ("nineteen", 19),
        
        # Compositional (tens + ones)
        ("twenty three", 23),
        ("forty five", 45),
        ("sixty seven", 67),
        ("eighty nine", 89),
        
        # With hyphens
        ("forty-five", 45),
        ("twenty-one", 21),
        
        # Digits
        ("5", 5),
        ("90", 90),
        ("180", 180),
        
        # Edge cases
        ("zero", 0),
        ("one hundred", 100),
        ("one hundred eighty", 180),
    ]
    
    passed = 0
    failed = 0
    
    for input_text, expected in test_cases:
        result = _num(input_text)
        if result == expected:
            passed += 1
            print(f"  [PASS] '{input_text}' -> {result}")
        else:
            failed += 1
            print(f"  [FAIL] '{input_text}' -> expected {expected}, got {result}")
    
    print(f"Number parsing: {passed} passed, {failed} failed\n")
    return failed == 0


def test_speed_extraction():
    """Test _extract_speed function."""
    print("Testing speed extraction...")
    
    test_cases = [
        ("forward five seconds speed L2", "L2"),
        ("go forward at speed L1", "L1"),
        ("turn left level 3", "L3"),
        ("backward speed L0", "L0"),
        ("forward level 2", "L2"),
        ("stop", None),
        ("forward five seconds", None),
    ]
    
    passed = 0
    failed = 0
    
    for input_text, expected in test_cases:
        result = _extract_speed(input_text)
        if result == expected:
            passed += 1
            print(f"  [PASS] '{input_text}' -> {result}")
        else:
            failed += 1
            print(f"  [FAIL] '{input_text}' -> expected {expected}, got {result}")
    
    print(f"Speed extraction: {passed} passed, {failed} failed\n")
    return failed == 0


def test_command_parsing():
    """Test to_structured function with various commands."""
    print("Testing command parsing...")
    
    test_cases = [
        # Forward/backward with various units
        {
            "input": "forward five seconds",
            "expected": {"action": "forward", "duration_ms": 5000, "angle_deg": None, "speed": None}
        },
        {
            "input": "backward ten secs",
            "expected": {"action": "backward", "duration_ms": 10000, "angle_deg": None, "speed": None}
        },
        {
            "input": "go forward for three seconds",
            "expected": {"action": "forward", "duration_ms": 3000, "angle_deg": None, "speed": None}
        },
        {
            "input": "move backward twenty seconds",
            "expected": {"action": "backward", "duration_ms": 20000, "angle_deg": None, "speed": None}
        },
        
        # Turns with various units
        {
            "input": "turn left ninety degrees",
            "expected": {"action": "left", "duration_ms": None, "angle_deg": 90, "speed": None}
        },
        {
            "input": "right forty five deg",
            "expected": {"action": "right", "duration_ms": None, "angle_deg": 45, "speed": None}
        },
        {
            "input": "left one hundred eighty degrees",
            "expected": {"action": "left", "duration_ms": None, "angle_deg": 180, "speed": None}
        },
        
        # With speed
        {
            "input": "forward five seconds speed L2",
            "expected": {"action": "forward", "duration_ms": 5000, "angle_deg": None, "speed": "L2"}
        },
        {
            "input": "turn right ninety degrees level 3",
            "expected": {"action": "right", "duration_ms": None, "angle_deg": 90, "speed": "L3"}
        },
        
        # Compositional numbers
        {
            "input": "forward twenty three seconds",
            "expected": {"action": "forward", "duration_ms": 23000, "angle_deg": None, "speed": None}
        },
        {
            "input": "left sixty seven degrees",
            "expected": {"action": "left", "duration_ms": None, "angle_deg": 67, "speed": None}
        },
        
        # Defaults
        {
            "input": "forward",
            "expected": {"action": "forward", "duration_ms": 2000, "angle_deg": None, "speed": None}
        },
        {
            "input": "left",
            "expected": {"action": "left", "duration_ms": None, "angle_deg": 90, "speed": None}
        },
        
        # Stop
        {
            "input": "stop",
            "expected": {"action": "stop", "duration_ms": None, "angle_deg": None, "speed": None}
        },
        {
            "input": "halt",
            "expected": {"action": "stop", "duration_ms": None, "angle_deg": None, "speed": None}
        },
        
        # Boundary cases
        {
            "input": "forward fifty seconds",
            "expected": {"action": "forward", "duration_ms": 30000, "angle_deg": None, "speed": None}  # Capped at MAX
        },
    ]
    
    passed = 0
    failed = 0
    
    for test in test_cases:
        result = to_structured(test["input"])
        expected = test["expected"]
        
        if result == expected:
            passed += 1
            print(f"  [PASS] '{test['input']}'")
        else:
            failed += 1
            print(f"  [FAIL] '{test['input']}'")
            print(f"        Expected: {expected}")
            print(f"        Got:      {result}")
    
    print(f"Command parsing: {passed} passed, {failed} failed\n")
    return failed == 0


def test_edge_cases():
    """Test edge cases and boundary conditions."""
    print("Testing edge cases...")
    
    test_cases = [
        # Missing units (implicit seconds)
        ("forward five", 5000),  # Implicitly "five seconds"
        ("right thirty", 30),    # Implicitly "thirty degrees"
        
        # Unrecognized command
        ("unknown command", "stop"),
        
        # Mixed case
        ("FORWARD FIVE SECONDS", 5000),
        ("TuRn LeFt NiNeTy DeGrEeS", 90),
    ]
    
    passed = 0
    failed = 0
    
    for input_text, expected_value in test_cases:
        result = to_structured(input_text)
        
        if isinstance(expected_value, int):
            # Check duration or angle
            actual = result.get("duration_ms") or result.get("angle_deg")
            if actual == expected_value:
                passed += 1
                print(f"  [PASS] '{input_text}'")
            else:
                failed += 1
                print(f"  [FAIL] '{input_text}' -> expected {expected_value}, got {actual}")
        else:
            # Check action
            if result["action"] == expected_value:
                passed += 1
                print(f"  [PASS] '{input_text}'")
            else:
                failed += 1
                print(f"  [FAIL] '{input_text}' -> expected action '{expected_value}', got '{result['action']}'")
    
    print(f"Edge cases: {passed} passed, {failed} failed\n")
    return failed == 0


def main():
    """Run all tests."""
    print("="*60)
    print("Parse English Module Test Suite")
    print("="*60)
    print()
    
    all_passed = True
    all_passed &= test_number_parsing()
    all_passed &= test_speed_extraction()
    all_passed &= test_command_parsing()
    all_passed &= test_edge_cases()
    
    print("="*60)
    if all_passed:
        print("All tests PASSED!")
        print("="*60)
        return 0
    else:
        print("Some tests FAILED. Review output above.")
        print("="*60)
        return 1


if __name__ == "__main__":
    exit(main())

