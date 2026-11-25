-- ============================================================
-- 学习辅导 AI 助教 · 数据库结构
--
-- 六张表，知识点是两级的：
--   第一级 chapter          15 个归类抽屉，一次性写死
--   第二级 knowledge_point  运行时由 AI 在章节下自动新建（必须先查重）
--
-- 🔴 FOREIGN KEY 是按连接生效的。本文件第一行只是给 sqlite3 CLI 用，
--    程序里每次 get_conn() 都会重新执行一次，见 app/db.py。
-- ============================================================

PRAGMA foreign_keys = ON;


-- ------------------------------------------------------------
-- 1. 章节（归类抽屉）
--    固定的 15 行，来自 data/chapters.json。
--    🔴 这不是知识库，只是挂靠点的名字，AI 不许新增顶层分类。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chapter (
    id    TEXT PRIMARY KEY,              -- ch_function_limit 形式
    name  TEXT NOT NULL UNIQUE,          -- 中文名，如「导数与微分」
    brief TEXT                           -- 一句话简介
);


-- ------------------------------------------------------------
-- 2. 知识点（第二级，运行时长出来）
--    🔴 chapter_id 必须是已存在的章节 → 保证「自由生长但不乱」
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_point (
    id           TEXT PRIMARY KEY,       -- kp_ + 短的稳定标识
    name         TEXT NOT NULL UNIQUE,   -- 如「洛必达法则」
    chapter_id   TEXT NOT NULL,
    explain      TEXT,                   -- 讲解/定义，可为空
    created_from TEXT,                   -- 来源：qa / intake，便于回溯
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (chapter_id) REFERENCES chapter(id)
);

CREATE INDEX IF NOT EXISTS idx_kp_chapter ON knowledge_point(chapter_id);


-- ------------------------------------------------------------
-- 3. 错题
--    solution_steps 存 JSON 数组的文本；image_path 只存相对路径。
--
--    错题本按「题」去重，不是流水账：
--    同一道题反复做错只累加 error_count、刷新 last_seen_at，
--    否则同一题会把 wrong_question 计数与掌握度重复放大。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wrong_question (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    question_text  TEXT NOT NULL,
    student_answer TEXT,
    correct_answer TEXT,
    solution_steps TEXT,                 -- JSON 数组文本，如 ["步骤1","步骤2"]
    error_type     TEXT,                 -- concept_confusion / calculation_error / method_wrong / incomplete / unknown
    difficulty     TEXT,                 -- easy / medium / hard（英文，供难度加权）
    source         TEXT,                 -- text / image
    image_path     TEXT,                 -- 相对路径，不存二进制
    source_question_id TEXT,             -- 题目标识，如 qp_0001；手工录入为 NULL
    error_count    INTEGER NOT NULL DEFAULT 1,  -- 累计做错次数
    last_seen_at   TEXT,                 -- 最近一次做错时间
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK (difficulty IS NULL OR difficulty IN ('easy','medium','hard')),
    CHECK (error_type IS NULL OR error_type IN
           ('concept_confusion','calculation_error','method_wrong','incomplete','unknown'))
);

CREATE INDEX IF NOT EXISTS idx_wq_created ON wrong_question(created_at DESC);

-- ⚠️ idx_wq_source（source_question_id 上的部分唯一索引）不写在这里。
--    老库上表的这些列可能还不存在，在本文件里建会直接报 no such column。
--    它由 app/db.py 的 _ensure_indexes() 在补列之后创建，见 init_db()。


-- ------------------------------------------------------------
-- 4. 错题 ↔ 知识点（多对多中间表）
--    一道题可能考多个知识点；一个知识点对应多道错题。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wq_kp (
    wq_id INTEGER NOT NULL,
    kp_id TEXT    NOT NULL,
    PRIMARY KEY (wq_id, kp_id),
    FOREIGN KEY (wq_id) REFERENCES wrong_question(id) ON DELETE CASCADE,
    FOREIGN KEY (kp_id) REFERENCES knowledge_point(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_wqkp_kp ON wq_kp(kp_id);


-- ------------------------------------------------------------
-- 5. 练习记录
--    掌握度 = 基础正确率 × 难度加权 × 时间衰减，全部从这里算。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS practice_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kp_id        TEXT    NOT NULL,
    question_ref TEXT,                   -- 题目来源标识，可为空
    is_correct   INTEGER NOT NULL,       -- 1 对 / 0 错
    difficulty   TEXT,
    duration_sec INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (kp_id) REFERENCES knowledge_point(id) ON DELETE CASCADE,
    CHECK (is_correct IN (0, 1)),
    CHECK (difficulty IS NULL OR difficulty IN ('easy','medium','hard'))
);

CREATE INDEX IF NOT EXISTS idx_pl_kp_time ON practice_log(kp_id, created_at DESC);


-- ------------------------------------------------------------
-- 6. 模型调用成本日志
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose           TEXT,              -- qa / intake / practice ...
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    retried           INTEGER DEFAULT 0,
    success           INTEGER DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK (success IN (0, 1))
);
