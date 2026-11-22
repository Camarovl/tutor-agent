"""一键建库 + 灌入归类抽屉与演示数据。

维护人：D（`seed_data.py` 归 D，见 app/README.md 与 docs/interfaces.md）。

用法：

    python seed_data.py

做什么（四步，都不删已有数据）：

  1. 按 `schema.sql` 建表 —— 幂等，反复调用不报错
  2. 章节：`chapter` 表为空时，灌入 `data/chapters.json` 的 15 个归类抽屉
  3. 演示数据：`knowledge_point` 表为空时，从 3 道错题里「长出」知识点，
     再挂上练习记录 —— 🔴 走 `knowledge.find_or_create()`，不直接 insert_kp，
     因为「知识点从真实错题里长出来」是本项目的核心设计（AGENTS.md 红线 6）
  4. 打印各类条数 + 最近错题 + 最薄弱三个知识点

可重复运行：已经有数据时对应步骤直接跳过，不清库、不覆盖。

⚠️ 为什么章节只在空库时灌：`db.insert_chapters()` 的实现是先
`DELETE FROM chapter` 再插入，而 `knowledge_point.chapter_id` 的外键**没有**
`ON DELETE CASCADE`。所以库里有知识点之后再重灌章节会撞外键报错。
这条属 C₁ 的文件，D 不改，只在检测到这种状态时给出提示。
"""

from __future__ import annotations

import sys

# 🔴 Windows 控制台默认 GBK，打印中文/emoji 会抛 UnicodeEncodeError 导致退出码 1。
#    必须在其它 import 之前（或至少在第一次 print 之前）改掉。
sys.stdout.reconfigure(encoding="utf-8")

import sqlite3  # noqa: E402 —— 只用来把底层异常翻译成人话，不写任何 SQL

from app import db, knowledge  # noqa: E402

# ============================================================
# 演示数据 —— 3 道错题，知识点由 find_or_create 长出来
#
# 字段与 docs/interfaces.md 第五节的抽取契约一一对应；
# practice 是 (练习总次数, 其中答对次数)，刻意安排成三档，让第 9 步的
# 排行榜能看出「未掌握 / 基本掌握 / 已掌握」的差别。
# ============================================================
DEMO_WRONG_QUESTIONS = [
    {
        "question_text": "求极限 $\\lim_{x\\to 0}\\dfrac{\\sin 3x}{x}$",
        "student_answer": "1",
        "correct_answer": "3",
        "solution_steps": [
            "当 x→0 时，sin 3x 与 3x 是等价无穷小",
            "原式 = lim(x→0) 3x / x = 3",
        ],
        "error_type": "method_wrong",
        "difficulty": "easy",
        "kp": ("等价无穷小替换", "ch_function_limit"),
        "practice": (6, 2),
    },
    {
        "question_text": "计算不定积分 $\\int 2x\\,dx$",
        "student_answer": "$x^2$",
        "correct_answer": "$x^2 + C$",
        "solution_steps": [
            "被积函数 2x 的一个原函数是 x^2",
            "不定积分要带上任意常数 C，答案写成 x^2 + C",
        ],
        "error_type": "concept_confusion",
        "difficulty": "easy",
        "kp": ("不定积分的换元", "ch_indefinite_integral"),
        "practice": (4, 3),
    },
    {
        "question_text": "求微分方程 $y' + y = 0$ 的通解",
        "student_answer": "$y = Ce^{x}$",
        "correct_answer": "$y = Ce^{-x}$",
        "solution_steps": [
            "这是一阶线性齐次方程，特征方程 r + 1 = 0",
            "特征根 r = -1，故通解 y = Ce^{-x}",
        ],
        "error_type": "calculation_error",
        "difficulty": "medium",
        "kp": ("一阶线性微分方程", "ch_ode_basic"),
        "practice": (5, 5),
    },
]


# ============================================================
# 步骤 2：章节
# ============================================================
def seed_chapters() -> int:
    """chapter 表为空时灌入 15 个归类抽屉；已存在就跳过。返回本次写入数。"""
    existing = db.chapter_rows()
    if existing:
        print(f"· 章节：已有 {len(existing)} 个，跳过")
        return 0
    chapters = db.load_chapters_from_json()
    written = db.insert_chapters(chapters)
    print(f"· 章节：灌入 {written} 个归类抽屉")
    return written


# ============================================================
# 步骤 3：演示数据
# ============================================================
def seed_demo() -> bool:
    """knowledge_point 为空时灌入演示数据；已有知识点就跳过。返回是否执行。"""
    existing = db.list_kps()
    if existing:
        print(f"· 演示数据：库里已有 {len(existing)} 个知识点，跳过（不覆盖真实记录）")
        return False

    for item in DEMO_WRONG_QUESTIONS:
        kp_name, chapter_id = item["kp"]

        # 🔴 走查重路径：命中已有条目就复用，没命中才在章节下新建
        kp_id = knowledge.find_or_create(kp_name, chapter_id, context=None)

        wq_id = db.insert_wrong_question(
            {
                "question_text": item["question_text"],
                "student_answer": item["student_answer"],
                "correct_answer": item["correct_answer"],
                "solution_steps": item["solution_steps"],
                "error_type": item["error_type"],
                "difficulty": item["difficulty"],
                "source": "text",
                "kp_ids": [kp_id],
            }
        )

        total, correct = item["practice"]
        for index in range(total):
            db.log_practice(
                kp_id,
                is_correct=index < correct,
                question_ref=f"seed_wq_{wq_id}",
                difficulty=item["difficulty"],
            )

        print(
            f"· 错题 #{wq_id} → 知识点「{kp_name}」({chapter_id})，"
            f"练习 {total} 次（对 {correct} 次）"
        )
    return True


# ============================================================
# 步骤 4：打印统计
# ============================================================
def print_summary() -> None:
    chapters = db.chapter_rows()
    kps = db.list_kps()
    wrong_total = db.wrong_question_count()

    print()
    print("=" * 52)
    print(f"数据库：{db.DB_PATH}")
    print(f"章节 {len(chapters)} · 知识点 {len(kps)} · 错题 {wrong_total}")

    recent = db.recent_wrong_questions(3)
    if recent:
        print("最近录入：")
        for row in recent:
            names = "、".join(k["name"] for k in row.get("knowledge_points", [])) or "—"
            stem = (row.get("question_text") or "").replace("\n", " ")[:34]
            print(f"  #{row.get('id')} {stem} … [{names}]")

    print("最薄弱三个知识点：")
    weak = _weak_points(3)
    if not weak:
        print("  （待 C₂ 的 profile.weak_points() 落地后显示，第 9 步）")
    else:
        for index, row in enumerate(weak, start=1):
            print(
                f"  {index}. {row.get('kp_name')}"
                f"（{row.get('status')}，掌握度 {float(row.get('mastery') or 0):.0%}）"
            )
    print("=" * 52)


def _weak_points(n: int) -> list[dict]:
    """best-effort：C₂ 的实现还没进 main 时返回空列表，而不是让脚本崩掉。"""
    try:
        from app.profile import weak_points
    except ImportError:
        return []
    try:
        return list(weak_points(n))
    except Exception:  # noqa: BLE001 —— 演示脚本不该因为展示环节失败而中断
        return []


# ============================================================
# 入口
# ============================================================
def main() -> int:
    print("建库与灌数据：")
    try:
        db.init_db()
        print("· 表结构：就绪（schema.sql，幂等）")

        seed_chapters()
        if db.chapter_rows() and db.list_kps():
            print("· 提示：库里已有知识点；要重灌章节请先删掉 data/app.db 再跑一遍")
        seed_demo()
    except sqlite3.DatabaseError as exc:
        print(f"\n❌ 数据库操作失败：{exc}")
        print("  如果提示 FOREIGN KEY constraint failed，说明库里已有知识点而章节被重灌，")
        print("  删掉 data/app.db 后重新运行本脚本即可。")
        return 1

    print_summary()
    print("完成。下一步：streamlit run streamlit_app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
