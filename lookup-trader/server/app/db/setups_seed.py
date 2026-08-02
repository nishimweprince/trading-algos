"""The setup vocabulary the operator labels with.

Mirrors the Autochartist pattern set — chart patterns, Fibonacci patterns and key
levels — plus the candlestick setups this project started with. `default_side` is
1/-1 only where the pattern carries an inherent direction; continuation patterns
resolve in the direction of the prior trend and are left NULL.
"""

from __future__ import annotations

# (setup_id, name, default_side, category)
SEED_SETUPS: list[tuple[str, str, int | None, str]] = [
    # --- Autochartist chart patterns ---
    ("head_shoulders", "Head and Shoulders", -1, "chart_pattern"),
    ("inv_head_shoulders", "Inverse Head and Shoulders", 1, "chart_pattern"),
    ("double_top", "Double Top", -1, "chart_pattern"),
    ("double_bottom", "Double Bottom", 1, "chart_pattern"),
    ("triple_top", "Triple Top", -1, "chart_pattern"),
    ("triple_bottom", "Triple Bottom", 1, "chart_pattern"),
    ("triangle_ascending", "Ascending Triangle", 1, "chart_pattern"),
    ("triangle_descending", "Descending Triangle", -1, "chart_pattern"),
    ("triangle_symmetrical", "Symmetrical Triangle", None, "chart_pattern"),
    ("wedge_rising", "Rising Wedge", -1, "chart_pattern"),
    ("wedge_falling", "Falling Wedge", 1, "chart_pattern"),
    ("channel_up", "Channel Up", 1, "chart_pattern"),
    ("channel_down", "Channel Down", -1, "chart_pattern"),
    ("rectangle", "Rectangle / Trading Range", None, "chart_pattern"),
    ("flag_bullish", "Bullish Flag", 1, "chart_pattern"),
    ("flag_bearish", "Bearish Flag", -1, "chart_pattern"),
    ("pennant_bullish", "Bullish Pennant", 1, "chart_pattern"),
    ("pennant_bearish", "Bearish Pennant", -1, "chart_pattern"),
    ("broadening_formation", "Broadening Formation", None, "chart_pattern"),
    # --- Autochartist Fibonacci patterns ---
    ("fib_abcd", "ABCD", None, "fibonacci"),
    ("fib_gartley", "Gartley", None, "fibonacci"),
    ("fib_butterfly", "Butterfly", None, "fibonacci"),
    ("fib_bat", "Bat", None, "fibonacci"),
    ("fib_crab", "Crab", None, "fibonacci"),
    ("fib_three_drives", "Three Drives", None, "fibonacci"),
    # --- Autochartist key levels ---
    ("key_level_breakout", "Key Level Breakout", None, "key_level"),
    ("key_level_approach", "Approaching Key Level", None, "key_level"),
    # --- candlestick setups ---
    ("bull_engulfing", "Bullish Engulfing", 1, "candlestick"),
    ("bear_engulfing", "Bearish Engulfing", -1, "candlestick"),
    ("pin_bar_long", "Bullish Pin Bar", 1, "candlestick"),
    ("inside_break", "Inside Bar Break", None, "candlestick"),
]
