"""落差計算：parse '0.3%' / '185K' / '50.9' / '+18.2% (NTD)' 這類字串，算 actual - forecast。"""
import re

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_value(s):
    if not s:
        return None
    m = _NUM.search(s)
    if not m:
        return None
    v = float(m.group())
    tail = s[m.end():m.end() + 1].upper()
    if tail in _MULT:
        v *= _MULT[tail]
    return v


def _surface(s):
    """回傳 (表面數值, 尾綴)：'110K' -> (110, 'K')，落差用表面單位算比較好讀。"""
    if not s:
        return None, ""
    m = _NUM.search(s)
    if not m:
        return None, ""
    tail = s[m.end():m.end() + 1].upper()
    return float(m.group()), (tail if tail in _MULT else "")


def annotate(event, direction):
    """就地補 surprise 欄位：{delta, verdict, good}。"""
    (a, sa), (f, sf) = _surface(event.get("actual")), _surface(event.get("forecast"))
    if a is None or f is None or sa != sf:
        a, f = parse_value(event.get("actual")), parse_value(event.get("forecast"))
        sa = ""
    if a is None or f is None:
        event["surprise"] = None
        return
    delta = a - f
    delta_s = f"{delta:+,.2f}".rstrip("0").rstrip(".") + sa
    if abs(delta) < 1e-9:
        verdict = "inline"
    elif delta > 0:
        verdict = "above"
    else:
        verdict = "below"
    good = None
    if direction == "higher_is_good":
        good = delta > 0 if verdict != "inline" else None
    elif direction == "higher_is_bad":
        good = delta < 0 if verdict != "inline" else None
    event["surprise"] = {"delta": delta_s, "verdict": verdict, "good": good}
