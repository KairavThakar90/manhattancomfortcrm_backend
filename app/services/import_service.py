import re
from datetime import datetime
from typing import Tuple, Optional

def parse_cost_v2(val: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Parses a cost string. Evaluates math expressions.
    Returns (float_value, error_message)
    """
    if not val:
        return None, None
    val = val.strip().replace('$', '').replace(',', '')
    if not val:
        return None, None
    
    # Check for text (skip spaces, digits, decimal point, plus, minus)
    if re.search(r'[a-zA-Z]', val):
        return None, f"Contains random text: {val}"
        
    try:
        parts = re.split(r'(\+|\-)', val.replace(' ', ''))
        total = 0.0
        current_op = '+'
        for p in parts:
            if p in ('+', '-'):
                current_op = p
            elif p:
                if current_op == '+':
                    total += float(p)
                else:
                    total -= float(p)
        return total, None
    except Exception as e:
        return None, f"Could not calculate math: {val}"


def check_date_v2(val: str) -> Optional[str]:
    """
    Returns an error message if the date format is invalid text, else None.
    """
    if not val:
        return None
    val = val.strip()
    match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', val)
    if not match:
        return f"Invalid date format: {val}"
    return None


def parse_date(val: str) -> Optional[datetime.date]:
    """
    Extracts a date from the string.
    """
    if not val:
        return None
    val = val.strip()
    match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', val)
    if match:
        m, d, y = match.groups()
        if len(y) == 2:
            y = "20" + y
        try:
            return datetime(int(y), int(m), int(d)).date()
        except:
            pass
    return None
