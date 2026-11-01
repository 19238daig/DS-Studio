"""Shared text guards for the metric-binding boundary.

answers.py (chat answers) and reporting.py (report prose) enforce the same
anti-hallucination contract: numbers shown to users must come from registered
metrics. The placeholder token, the Chinese numeral alphabet and the numeric
lexeme scanner are the single source here; each module keeps its own NUMBER
variant because chat answers and report prose draw the line in slightly
different places by design.
"""
from __future__ import annotations

import re

# {{metric:analysis_id:metric_key}} — the only way a raw number may appear.
TOKEN = re.compile(r'\{\{metric:([a-zA-Z0-9_-]+):([^{}:]+)\}\}')

CN_NUMBER = '零〇一二两三四五六七八九十百千万亿'

# Complete numeric lexemes (sign, decimals, exponent) — used where existing
# numbers are compared or counted rather than rejected.
NUMERIC_LEXEME = re.compile(r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?')
