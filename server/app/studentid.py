"""学号解析：前两位院系号 + 第 3、4 位入学年份 + 后四位序号。"""

import re

_STUDENT_ID_RE = re.compile(r"^\d{8}$")


def parse_student_id(sid: str) -> dict:
    """解析 8 位数字学号 → {valid, dept, enroll_year, serial}；非法时仅 valid=False。"""
    if not isinstance(sid, str) or not _STUDENT_ID_RE.fullmatch(sid):
        return {"valid": False, "dept": "", "enroll_year": None, "serial": ""}
    return {
        "valid": True,
        "dept": sid[:2],
        "enroll_year": 2000 + int(sid[2:4]),
        "serial": sid[4:],
    }
