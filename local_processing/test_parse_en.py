"""
Unit tests for simplified parse_en.py module.
Tests only grammar-defined numbers and basic command parsing.
"""

import unittest
from parse_en import to_structured, _parse_number, MAX_DURATION_MS, MAX_ANGLE_DEG


class TestParseEn(unittest.TestCase):

    def test_parse_number(self):
        """Test number parsing with grammar-defined numbers only."""
        # Basic numbers
        self.assertEqual(_parse_number("one"), 1)
        self.assertEqual(_parse_number("five"), 5)
        self.assertEqual(_parse_number("ten"), 10)
        
        # Common values
        self.assertEqual(_parse_number("fifteen"), 15)
        self.assertEqual(_parse_number("twenty"), 20)
        self.assertEqual(_parse_number("thirty"), 30)
        self.assertEqual(_parse_number("forty"), 40)
        self.assertEqual(_parse_number("forty five"), 45)
        self.assertEqual(_parse_number("forty-five"), 45)
        self.assertEqual(_parse_number("sixty"), 60)
        self.assertEqual(_parse_number("ninety"), 90)
        self.assertEqual(_parse_number("one hundred"), 100)
        self.assertEqual(_parse_number("one hundred eighty"), 180)
        
        # Digits
        self.assertEqual(_parse_number("5"), 5)
        self.assertEqual(_parse_number("90"), 90)
        
        # Not in grammar (should return None)
        self.assertIsNone(_parse_number("eleven"))
        self.assertIsNone(_parse_number("twenty three"))
        self.assertIsNone(_parse_number("hello"))

    def test_forward_commands(self):
        """Test forward movement commands."""
        # Basic forward
        result = to_structured("forward five seconds")
        self.assertEqual(result["action"], "forward")
        self.assertEqual(result["duration_ms"], 5000)
        self.assertIsNone(result["angle_deg"])
        self.assertIsNone(result["speed"])
        
        # Different durations
        result = to_structured("forward ten seconds")
        self.assertEqual(result["action"], "forward")
        self.assertEqual(result["duration_ms"], 10000)
        
        result = to_structured("forward twenty seconds")
        self.assertEqual(result["action"], "forward")
        self.assertEqual(result["duration_ms"], 20000)
        
        # Without duration (no number matched)
        result = to_structured("forward")
        self.assertEqual(result["action"], "forward")
        self.assertIsNone(result["duration_ms"])

    def test_backward_commands(self):
        """Test backward movement commands."""
        result = to_structured("backward five seconds")
        self.assertEqual(result["action"], "backward")
        self.assertEqual(result["duration_ms"], 5000)
        self.assertIsNone(result["angle_deg"])
        self.assertIsNone(result["speed"])
        
        result = to_structured("backward thirty seconds")
        self.assertEqual(result["action"], "backward")
        self.assertEqual(result["duration_ms"], 30000)

    def test_left_turn_commands(self):
        """Test left turn commands."""
        result = to_structured("turn left ninety degrees")
        self.assertEqual(result["action"], "left")
        self.assertEqual(result["angle_deg"], 90)
        self.assertIsNone(result["duration_ms"])
        self.assertIsNone(result["speed"])
        
        result = to_structured("left forty five degrees")
        self.assertEqual(result["action"], "left")
        self.assertEqual(result["angle_deg"], 45)
        
        result = to_structured("turn left one hundred eighty degrees")
        self.assertEqual(result["action"], "left")
        self.assertEqual(result["angle_deg"], 180)

    def test_right_turn_commands(self):
        """Test right turn commands."""
        result = to_structured("turn right ninety degrees")
        self.assertEqual(result["action"], "right")
        self.assertEqual(result["angle_deg"], 90)
        self.assertIsNone(result["duration_ms"])
        self.assertIsNone(result["speed"])
        
        result = to_structured("right sixty degrees")
        self.assertEqual(result["action"], "right")
        self.assertEqual(result["angle_deg"], 60)

    def test_stop_command(self):
        """Test stop command."""
        result = to_structured("stop")
        self.assertEqual(result["action"], "stop")
        self.assertIsNone(result["duration_ms"])
        self.assertIsNone(result["angle_deg"])
        self.assertIsNone(result["speed"])

    def test_boundary_limits(self):
        """Test duration and angle boundary enforcement."""
        # Max duration (30 seconds = 30000ms)
        result = to_structured("forward one hundred seconds")
        self.assertEqual(result["action"], "forward")
        self.assertEqual(result["duration_ms"], MAX_DURATION_MS)  # capped at 30000
        
        # Angle bounds (1-360)
        # Note: our grammar doesn't have numbers > 360, so this is theoretical
        # but the code has the safety check

    def test_unrecognized_commands(self):
        """Test handling of unrecognized commands."""
        result = to_structured("hello world")
        self.assertIsNone(result["action"])
        self.assertIsNone(result["duration_ms"])
        self.assertIsNone(result["angle_deg"])
        self.assertIsNone(result["speed"])
        
        result = to_structured("jump")
        self.assertIsNone(result["action"])

    def test_speed_always_null(self):
        """Test that speed is always null (not used)."""
        # Speed is removed from the system
        result = to_structured("forward five seconds")
        self.assertIsNone(result["speed"])
        
        result = to_structured("turn left ninety degrees")
        self.assertIsNone(result["speed"])
        
        result = to_structured("stop")
        self.assertIsNone(result["speed"])

    def test_case_insensitivity(self):
        """Test case-insensitive parsing."""
        result1 = to_structured("FORWARD FIVE SECONDS")
        result2 = to_structured("forward five seconds")
        self.assertEqual(result1, result2)
        
        result1 = to_structured("Turn Left Ninety Degrees")
        result2 = to_structured("turn left ninety degrees")
        self.assertEqual(result1, result2)

    def test_whitespace_handling(self):
        """Test normalization of extra whitespace."""
        result1 = to_structured("forward   five    seconds")
        result2 = to_structured("forward five seconds")
        self.assertEqual(result1, result2)


def run_tests():
    """Run all tests with formatted output."""
    print("="*60)
    print("Parse English Module Test Suite (Simplified)")
    print("="*60)
    
    # Create test suite
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestParseEn)
    
    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # Summary
    print("\n" + "="*60)
    if result.wasSuccessful():
        print("✅ All tests PASSED!")
    else:
        print(f"❌ {len(result.failures)} test(s) FAILED")
        print(f"❌ {len(result.errors)} test(s) ERROR")
    print("="*60)
    
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    exit(0 if success else 1)
