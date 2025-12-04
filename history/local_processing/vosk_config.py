"""
Common configuration for Vosk speech recognition.
Shared grammar lists and constants.
"""

# Minimal grammar for basic commands
GRAMMAR_MINIMAL = [
    "forward", "backward", "turn left", "turn right",
    "turn around",
    "left", "right", "stop"
]

# Extended grammar including numbers and units for full command recognition
GRAMMAR_EXTENDED = [
    # Actions
    "forward", "backward", "turn left", "turn right", "turn around",
    "left", "right", "stop",
    # Prepositions and units
    "for", "second", "seconds", "degree", "degrees", "around",
    # Numbers (0-10 and common values)
    "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine", "ten",
    # Common angles and durations
    "fifteen", "twenty", "thirty", "forty", "forty five", "forty-five",
    "sixty", "ninety", "one hundred", "one hundred eighty"
]

# Default: use extended grammar for better recognition
GRAMMAR_DEFAULT = GRAMMAR_EXTENDED

