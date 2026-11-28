"""一键建库 + 灌入归类抽屉与演示数据。

维护人：D（`seed_data.py` 归 D，见 app/README.md 与 docs/interfaces.md）。

用法：

    python seed_data.py                 # 建表 + 章节 + 演示数据（空库时才灌）
    python seed_data.py --skip-demo     # 只建表 + 章节（真实使用）
    python seed_data.py --reset-demo    # 删掉 data/app.db 重建，再灌演示数据

演示数据的规模（评审不需要自己造数据就能跑通主流程）：

    30 道错题（`data/demo_wrong_questions.json`）
    10 个知识点（由错题经 `knowledge.find_or_create()` 长出来，不预置）
    30 道题库题（`data/question_bank.json`，每个知识点 3 道）
    40 条模型调用日志（只用于让指标卡有数）
    每个知识点 3~8 条练习记录，其中 2 个只给 1~2 条（样本不足档）

可重复运行：已经有知识点时默认模式跳过，不清库、不覆盖真实记录。
随机数用固定种子，任何人跑出来的分布都一样 —— 演示可复现。

⚠️ 为什么章节只在空库时灌：`db.insert_chapters()` 的实现是先
`DELETE FROM chapter` 再插入，而 `knowledge_point.chapter_id` 的外键没有
`ON DELETE CASCADE`。所以库里有知识点之后再重灌章节会撞外键报错；
要重灌就整库重建（`--reset-demo`）。
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

# 🔴 Windows 控制台默认 GBK，打印中文/emoji 会抛 UnicodeEncodeError 导致退出码 1。
sys.stdout.reconfigure(encoding="utf-8")

from app import db, knowledge  # noqa: E402

ROOT = Path(__file__).resolve().parent
DEMO_FILE = ROOT / "data" / "demo_wrong_questions.json"
BANK_FILE = ROOT / "data" / "question_bank.json"

#: 固定随机种子：演示数据的分布必须可复现，任何人跑结果一致。
DEMO_SEED = 20261206

#: 模型调用日志条数（只为了让「累计调用 / 累计用量」不是 0）
LLM_LOG_ROWS = 40


# ============================================================
# 步骤 1：章节
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
# 步骤 2：演示数据
# ============================================================
def load_demo() -> list[dict]:
    with DEMO_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


def seed_demo(rng: random.Random) -> tuple[int, int]:
    """knowledge_point 为空时灌入整套演示数据；已有知识点就跳过。

    返回 (成功条数, 失败条数)。单条失败不打断整批，但会在结尾计数报出来 ——
    不能"看起来成功"。
    """
    existing = db.list_kps()
    if existing:
        print(f"· 演示数据：库里已有 {len(existing)} 个知识点，跳过（不覆盖真实记录）")
        return 0, 0

    items = load_demo()
    kp_ids: dict[str, str] = {}
    ok = failed = 0

    for item in items:
        kp_name, chapter_id = item["kp"]
        try:
            # 🔴 知识点必须从错题里长出来：走查重 → 命中复用 → 没命中才新建
            kp_id = knowledge.find_or_create(kp_name, chapter_id, context=None)
        except ValueError as exc:
            print(f"  ✗ 「{kp_name}」跳过：{exc}")
            failed += 1
            continue

        try:
            db.insert_wrong_question(
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
        except sqlite3.DatabaseError as exc:
            print(f"  ✗ 「{kp_name}」写库失败：{exc}")
            failed += 1
            continue

        kp_ids[kp_name] = kp_id
        ok += 1

    # ---- 练习记录：三档 + 两个「样本不足」 ----
    names = list(kp_ids)
    short = set(rng.sample(names, 2)) if len(names) >= 2 else set()
    for index, name in enumerate(names):
        kp_id = kp_ids[name]
        if name in short:
            total = rng.randint(1, 2)
            correct = rng.randint(0, total)
        else:
            band = index % 3
            total = rng.randint(3, 8)
            if band == 0:  # 未掌握：正确率压到 30% 以内，乘难度系数也不会越过 40%
                correct = rng.randint(0, max(0, int(total * 0.3)))
            elif band == 1:  # 基本掌握：40%~70%
                correct = rng.randint(int(total * 0.45), max(int(total * 0.45), int(total * 0.65)))
            else:  # 已掌握：70% 以上
                correct = rng.randint(int(total * 0.75), total)
        for attempt in range(total):
            db.log_practice(
                kp_id,
                is_correct=attempt < correct,
                question_ref=f"seed_{name}",
                difficulty=rng.choice(["easy", "medium", "hard"]),
            )

    # ---- 模型调用日志：只为了指标卡有数 ----
    for _ in range(LLM_LOG_ROWS):
        db.log_llm_call(
            purpose=rng.choice(["qa", "intake", "vision"]),
            prompt_tokens=rng.randint(200, 1200),
            completion_tokens=rng.randint(120, 900),
            retried=rng.randint(0, 1),
            success=rng.random() > 0.05,
        )

    return ok, failed


# ============================================================
# 步骤 3：打印汇总
# ============================================================
def print_summary() -> None:
    from app import profile  # 只读统计，用来确认四档分布真的有差异

    df = profile.mastery_table()
    counts = {label: int((df["status"] == label).sum()) for label in ("未掌握", "基本掌握", "已掌握", "样本不足")}
    weak = profile.weak_points(3)
    summary = profile.summary_text()
    bank_total = len(json.loads(BANK_FILE.read_text(encoding="utf-8")))

    print()
    print("=" * 56)
    print(f"数据库：{db.DB_PATH}")
    print(
        "掌握度分布：未掌握 {未掌握} · 基本掌握 {基本掌握} · 已掌握 {已掌握} · 样本不足 {样本不足}".format(**counts)
    )
    print("最薄弱三个：" + " · ".join(
        f"{row.get('kp_name')} {float(row.get('mastery') or 0):.2f}" for row in weak
    ))
    print(
        f"错题 {db.wrong_question_count()} 条 · 练习 {summary.get('practice_total', 0)} 条 · "
        f"题库 {bank_total} 道 · 调用日志 {summary.get('llm_calls', 0)} 条"
    )
    print("=" * 56)


# ============================================================
# 入口
# ============================================================
def _reset_database() -> None:
    """删掉数据库文件重建。只在 --reset-demo 时调用。"""
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()
        print(f"⚠️  已删除数据库：{db.DB_PATH}（--reset-demo）")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    skip_demo = "--skip-demo" in args
    reset_demo = "--reset-demo" in args

    if skip_demo and reset_demo:
        print("❌ --skip-demo 与 --reset-demo 不能同时使用。")
        return 2

    print("建库与灌数据：")
    if reset_demo:
        try:
            _reset_database()
        except OSError as exc:
            print(f"❌ 删除数据库失败：{exc}")
            return 1

    try:
        db.init_db()
    except FileNotFoundError:
        print("❌ 找不到 schema.sql —— 请确认在仓库根目录运行本脚本（不是 app/ 或 data/ 里）。")
        return 1
    except sqlite3.DatabaseError as exc:
        print(f"❌ 建表失败：{exc}")
        return 1
    print("· 表结构：就绪（schema.sql，幂等）")

    try:
        seed_chapters()
        if skip_demo:
            print("· 演示数据：--skip-demo，跳过")
            failed = 0
        else:
            if db.chapter_rows() and db.list_kps():
                print("· 提示：库里已有知识点；要重灌演示数据请加 --reset-demo")
            ok, failed = seed_demo(random.Random(DEMO_SEED))
            if ok:
                print(f"· 演示数据：写入 {ok} 道错题（知识点与练习记录同步生成）")
    except sqlite3.DatabaseError as exc:
        print(f"\n❌ 数据库操作失败：{exc}")
        return 1

    print_summary()
    if failed:
        print(f"❌ 有 {failed} 条演示数据没写进去（见上面 ✗ 开头的行），本次不算成功。")
        return 1
    print("完成。下一步：streamlit run streamlit_app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
