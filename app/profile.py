"""功能③④的统计层：掌握度估计、薄弱点排序、练习选池与判分。

维护人：C₂。契约：docs/interfaces.md 第七节。

掌握度不是简单除法，而是三个确定性因子的乘积：

    掌握度 = 基础正确率 × 难度加权系数 × 时间衰减系数

    难度加权：全易题 1.0 / 含中等 1.05 / 含难题 1.15
    时间衰减：距上次练习 ≤7 天 1.0 / 8~30 天 0.9 / >30 天 0.75

封顶的是**最终掌握度**（截断到 1.0），不是难度系数本身 —— 否则 1.15 永远
会被先截掉，“难题答对更能证明掌握”就形同虚设。

分级阈值（练过 ≥3 次才参与分级，否则一律「样本不足」）：

    <40% 未掌握 / <70% 基本掌握 / ≥70% 已掌握

⚠️ 数据层说明：
    mastery 的聚合、系数计算与分级由 db.mastery_rows() 完成（SQL 在 db.py），
    llm 用量由 db.llm_usage() 提供；本文件只负责 DataFrame 转换、类型与四舍五入。
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from fractions import Fraction
from pathlib import Path

import pandas as pd

from . import db

#: 契约第七节定死的 11 列，顺序不能改（界面按位置渲染）
MASTERY_COLUMNS = [
    "kp_id",
    "kp_name",
    "chapter_id",
    "chapter_name",
    "attempts",
    "correct",
    "correct_rate",
    "difficulty_factor",
    "decay_factor",
    "mastery",
    "status",
]

#: 完全没有可用题目时返回给界面的话术（只描述现状与下一步，不带技术细节）
_NO_PRACTICE_QUESTIONS = "题库暂时没有可用的题目，可以先录入错题，系统会按薄弱知识点出题"


def mastery_table() -> pd.DataFrame:
    """返回全部知识点的掌握度表（含从没练过的，11 列顺序固定）。

    没练过的行：attempts/correct/correct_rate/difficulty_factor/decay_factor/
    mastery 全部为 0.0，status 固定「样本不足」——契约要求这些行必须出现，
    否则界面上的表会缺行。
    """
    rows = db.mastery_rows()
    if not rows:
        return pd.DataFrame(columns=MASTERY_COLUMNS)

    df = pd.DataFrame(rows, columns=MASTERY_COLUMNS)

    # db.mastery_rows() 已经把系数、mastery、status 算好；这里只做类型与四舍五入。
    for column in ("correct_rate", "difficulty_factor", "decay_factor", "mastery"):
        df[column] = df[column].astype(float).round(4)

    df["attempts"] = df["attempts"].astype(int)
    df["correct"] = df["correct"].astype(int)
    return df


def weak_points(n: int) -> list[dict]:
    """排除「样本不足」后，掌握度最低的 n 个知识点（供功能④出题）。

    不只看「未掌握」：如果库里只有两个薄弱点而 n=3，第三个会从「基本掌握」
    或「已掌握」里补 —— 但绝不会拿「样本不足」来凑数，否则系统会一直推
    没人练过的题（这正是契约要求排除样本不足的原因）。
    """
    if n <= 0:
        return []
    df = mastery_table()
    if df.empty:
        return []
    ranked = df[df["status"] != "样本不足"].sort_values("mastery")
    return ranked.head(int(n)).to_dict("records")


def summary_text() -> dict:
    """界面顶部指标卡的 6 个键（键名由契约第七节定死）。

    llm 用量由 db.llm_usage() 提供；没有调用记录时是 0。
    """
    df = mastery_table()
    usage = db.llm_usage()
    return {
        "kp_total": int(len(df)),
        "wrong_total": int(db.wrong_question_count()),
        "practice_total": int(df["attempts"].sum()) if not df.empty else 0,
        "weak_count": int((df["status"] == "未掌握").sum()) if not df.empty else 0,
        "llm_calls": int(usage.get("calls", 0)),
        "llm_tokens": int(usage.get("tokens", 0)),
    }


def build_practice_session(
    count: int = 3, exclude_ids: list[str] | None = None
) -> tuple[list[dict], str]:
    """按薄弱知识点从本地题库选一组题（不调用模型）。

    返回的第二个值是给用户看的「来源说明」：只描述题目从哪来，不含文件名、
    命令、内部进度术语；题目被跳过的技术原因打到 stderr，不进入界面字符串。

    选池规则：
      1. 只保留 kp_ids 都能在数据库里找到的题，避免练完写 practice_log 时外键失败；
      2. 按「命中薄弱知识点的个数」降序，同分按题目 id 稳定排序；
      3. `exclude_ids` 里的题（通常是上一轮刚做过的）排到最后，新题优先；
         新题不够时用旧题补足，并在来源说明里写清楚；
      4. 题库不够 count 题就返回现有的，并说明「题库暂时只有这些」。
    """
    count = int(count)
    if count <= 0:
        return [], "题目数量必须大于 0"
    seen_ids = {str(item) for item in (exclude_ids or []) if item}

    bank_path = Path(__file__).resolve().parent.parent / "data" / "question_bank.json"
    try:
        bank = json.loads(bank_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[profile] 练习题库读取失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return [], _NO_PRACTICE_QUESTIONS
    if not isinstance(bank, list) or not bank:
        return [], _NO_PRACTICE_QUESTIONS

    existing = {row["id"] for row in db.list_kps()}
    valid: list[dict] = []
    skipped: list[str] = []
    for item in bank:
        if not isinstance(item, dict) or not item.get("id"):
            skipped.append("缺少题目 id")
            continue
        kp_ids = item.get("kp_ids")
        if not isinstance(kp_ids, list) or not kp_ids:
            skipped.append(f"{item.get('id')}: 缺少 kp_ids")
            continue
        missing = [
            kp_id
            for kp_id in kp_ids
            if not isinstance(kp_id, str) or kp_id not in existing
        ]
        if missing:
            skipped.append(f"{item.get('id')}: kp_ids 不在库中 {missing}")
            continue
        valid.append(item)

    if skipped:
        print(
            "[profile] 练习题库跳过不可用题目：" + "；".join(skipped[:5]),
            file=sys.stderr,
        )
    if not valid:
        return [], _NO_PRACTICE_QUESTIONS

    weak_ids = {row["kp_id"] for row in weak_points(count)}

    def weak_hits(item: dict) -> int:
        return sum(1 for kp_id in item["kp_ids"] if kp_id in weak_ids)

    ranked = sorted(valid, key=lambda item: (-weak_hits(item), str(item["id"])))
    fresh = [item for item in ranked if str(item["id"]) not in seen_ids]
    reused = [item for item in ranked if str(item["id"]) in seen_ids]

    chosen = [dict(item) for item in fresh[:count]]
    reused_count = 0
    if len(chosen) < count and reused:
        need = count - len(chosen)
        chosen += [dict(item) for item in reused[:need]]
        reused_count = min(need, len(reused))

    if len(chosen) < count:
        note = f"本次生成了 {len(chosen)} 道（题库暂时只有这些）"
    elif reused_count:
        note = (
            f"本次生成了 {len(chosen)} 道"
            f"（题库里的新题已经练完，其中 {reused_count} 道是上次做过的）"
        )
    elif any(weak_hits(item) > 0 for item in chosen):
        note = "本次来自薄弱知识点"
    else:
        note = "本次来自题库"
    return chosen, note


_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _normalize_answer(value) -> str:
    """把学生答案归一成可比较的形式（只用于判分，不改数据）。

    · 全角转半角（NFKC），去掉所有空白
    · π/pi、×/*、÷//、各种减号统一
    · 上标数字转 ^：x² → x^2（学生常直接打上标）
    """
    # 上标必须在 NFKC 之前处理：NFKC 会把 ² 直接压成 2，就分不出是幂次了
    raw_text = re.sub(
        r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+",
        lambda m: "^" + m.group(0).translate(_SUPERSCRIPT_DIGITS),
        str(value or ""),
    )
    text = unicodedata.normalize("NFKC", raw_text)
    text = (
        text.replace("π", "pi")
        .replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
    )
    return re.sub(r"\s+", "", text).lower()


def _as_fraction(value: str) -> Fraction | None:
    """纯数字答案转分数：'3/2'、'1.5'、'6/4' 都能落到同一个值。"""
    text = _normalize_answer(value)
    if not text or not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)(/\d+)?", text):
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def _normalize_interval(value: str) -> str | None:
    """区间类答案归一：'0 到 1' / '[0,1]' / '(0,1)' / '0~1' 都看成同一个区间。"""
    text = _normalize_answer(value)
    if not text:
        return None
    if not re.search(r"[\[\](){}]|,|到|~", text):
        return None
    text = re.sub(r"[\[\](){}]", "", text)
    text = text.replace("到", ",").replace("~", ",")
    if "," not in text:
        return None
    return ",".join(part for part in text.split(",") if part)


def grade(item: dict, user_answer) -> bool:
    """客观判分（纯函数、确定性，不读库、不联网）。

    choice：忽略大小写与首尾空格；
    fill  ：先做字符串归一，再依次尝试「数值等价」「区间等价」——
            '3/2' 与 '1.5'、'6/4' 视为相同；'0 到 1' 与 '[0,1]' 视为相同；
            'π/2' 与 'pi/2' 视为相同（π 归一成 pi）；
    未知题型：False。
    """
    if not isinstance(item, dict):
        return False
    expected = str(item.get("answer") or "").strip()
    got = str(user_answer or "").strip()
    if not expected:
        return False

    kind = item.get("type")
    if kind == "choice":
        return got.upper() == expected.upper()
    if kind == "fill":
        if _normalize_answer(got) == _normalize_answer(expected):
            return True
        left, right = _as_fraction(expected), _as_fraction(got)
        if left is not None and right is not None and left == right:
            return True
        left_iv, right_iv = _normalize_interval(expected), _normalize_interval(got)
        if left_iv is not None and right_iv is not None and left_iv == right_iv:
            return True
        return False
    return False


def _as_number(value) -> float | None:
    """能转成数字就返回 float，否则 None（只用于错因的启发式判断）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _infer_error_type(item: dict, user_answer) -> str:
    """不调模型的错因推断：空答案 → incomplete，数值不等 → calculation_error。

    判不出来就落 unknown；界面会提示“判不出”，不会伪造确定性。
    """
    got = str(user_answer or "").strip()
    expected = str(item.get("answer") or "").strip()
    if not got:
        return "incomplete"
    got_number = _as_number(got)
    expected_number = _as_number(expected)
    if (
        got_number is not None
        and expected_number is not None
        and got_number != expected_number
    ):
        return "calculation_error"
    return "unknown"


def record_result(items: list[dict], answers) -> list[bool]:
    """判分 + 写 practice_log + 做错的题回流错题本。

    answers 允许是 {题目 id: 用户答案}（界面用这个），也兼容按位置给的列表。
    每道题的每个 kp_id 各写一条练习记录，掌握度按知识点分别累计。
    """
    results: list[bool] = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            results.append(False)
            continue

        qid = item.get("id")
        if isinstance(answers, dict):
            user_answer = answers.get(qid, "")
        elif isinstance(answers, (list, tuple)):
            user_answer = answers[index] if index < len(answers) else ""
        else:
            user_answer = ""

        ok = grade(item, user_answer)
        kp_ids = item.get("kp_ids")
        kp_ids = kp_ids if isinstance(kp_ids, list) else []

        for kp_id in kp_ids:
            db.log_practice(
                kp_id,
                ok,
                question_ref=qid,
                difficulty=item.get("difficulty"),
            )

        if not ok:
            explanation = item.get("explanation")
            steps = (
                [explanation]
                if isinstance(explanation, str) and explanation.strip()
                else []
            )
            wrong_data = {
                "question_text": item.get("stem") or "",
                "student_answer": str(user_answer or ""),
                "correct_answer": str(item.get("answer") or ""),
                "solution_steps": steps,
                "error_type": _infer_error_type(item, user_answer),
                "difficulty": item.get("difficulty"),
                "source": "practice",
                "kp_ids": kp_ids,
            }
            source_id = qid.strip() if isinstance(qid, str) else ""
            if source_id:
                # 同一道题反复做错只累加 error_count，不重复建错题
                db.upsert_wrong_question_by_source(
                    source_id, wrong_data, image_path=None
                )
            else:
                # 没有题目 id 的兜底路径，无法去重，保持普通插入
                db.insert_wrong_question(wrong_data, image_path=None)

        results.append(ok)
    return results
