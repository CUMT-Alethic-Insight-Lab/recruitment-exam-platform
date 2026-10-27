import json
import re
from datetime import date

from .constants import POLITICAL_STATUSES

CLUB_NAME = "Alethic Insight人工智能社团"


def parse_profile(raw):
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def trusted_identity(conn, student_id):
    member = conn.execute("SELECT * FROM membership WHERE student_id = ?", (student_id,)).fetchone()
    app = conn.execute("SELECT name FROM application WHERE student_id = ? ORDER BY created_at DESC, campaign_id DESC LIMIT 1", (student_id,)).fetchone()
    acct = conn.execute("SELECT real_name FROM account WHERE student_id = ?", (student_id,)).fetchone()
    name = member["name"] if member else (app["name"] if app else (acct["real_name"] if acct else ""))
    if name in {"测试账号", "适度且健康地爬行"}:
        name = ""
    return {"name": name,
            "student_id": student_id,
            "club_name": CLUB_NAME,
            "club_position": member["club_position"] if member else ""}


def completion_errors(profile, identity):
    errors = {}
    labels = {"birth_date": "出生年月", "political_status": "政治面貌", "college": "所在学院全称",
              "class_name": "所在班级（专业全称＋年级-班级）", "enroll_year": "入学年份",
              "grad_year": "预计毕业年份", "phone": "手机号"}
    for key, label in labels.items():
        if not profile.get(key):
            errors[key] = f"请填写{label}"
    if not identity.get("name") or not re.fullmatch(r"[0-9]{8}", identity.get("student_id", "")):
        errors["identity"] = "姓名或学号尚未由管理员确认，请联系管理员核对（昵称不作为姓名）"
    college = str(profile.get("college") or "").strip()
    if college and (len(college) < 5 or not college.endswith(("学院", "学部")) or college in {"计算机学院", "计科学院", "人工智能学院", "矿业学院"}):
        errors["college"] = "请填写学校公布的学院全称，如：计算机科学与技术学院/人工智能学院；不要使用简称"
    class_name = str(profile.get("class_name") or "").strip()
    class_match = re.fullmatch(r"([^\d\s]{2,})[0-9]{4}-[0-9一二三四五六七八九十百零〇两]+班", class_name)
    if class_name and (not class_match or class_match[1] in {"计科", "人智", "机科", "矿业", "安工", "地信", "过控"}):
        errors["class_name"] = "请按专业全称＋年级-班级填写，如：智能采矿工程2021-1班（不可填计科2401班等简称）"
    birth = profile.get("birth_date")
    now = date.today()
    if birth:
        if not isinstance(birth, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", birth):
            errors["birth_date"] = "出生年月须为 YYYY-MM"
        elif not f"{now.year - 100:04}-01" <= birth <= f"{now.year - 10:04}-{now.month:02}":
            errors["birth_date"] = "请核对出生年月（年龄应为 10–100 岁）"
    if profile.get("political_status") and profile["political_status"] not in POLITICAL_STATUSES:
        errors["political_status"] = "请选择模板中的政治面貌"
    for key in ("enroll_year", "grad_year"):
        y = profile.get(key)
        if y is not None and (type(y) is not int or not 2000 <= y <= 2040):
            errors[key] = "年份须为 2000–2040 的四位整数"
    enroll, grad = profile.get("enroll_year"), profile.get("grad_year")
    if type(enroll) is int and type(grad) is int and grad < enroll:
        errors["grad_year"] = "预计毕业年份不能早于入学年份（无需固定相差四年）"
    if birth and "birth_date" not in errors and type(enroll) is int and enroll < int(birth[:4]) + 10:
        errors["enroll_year"] = "请核对入学年份与出生年月"
    if profile.get("phone") and not re.fullmatch(r"1[0-9]{10}", str(profile["phone"])):
        errors["phone"] = "手机号须为 1 开头的 11 位数字"
    if not (profile.get("email") or profile.get("school_email")):
        errors["email"] = "请至少填写一个邮箱：优先常用邮箱，其次学校邮箱"
    for key in ("email", "school_email"):
        value = profile.get(key)
        if value and (not isinstance(value, str) or len(value) > 80 or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value)):
            errors[key] = "请填写有效邮箱"
    return errors


def completion_status(conn, acct, profile=None):
    profile = parse_profile(acct["profile"]) if profile is None else profile
    errors = completion_errors(profile, trusted_identity(conn, acct["student_id"]))
    return {"profile_completed": bool(acct["profile_completed"]) and not errors,
            "profile_errors": errors}
