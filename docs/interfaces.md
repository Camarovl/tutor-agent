# 接口契约 · 学习辅导 AI 助教（高等数学）

## 怎么用这个文件

- **接手任务时先读这里**，再打开对应源码。不需要问原作者"你这个函数怎么调"。
- 🔴 改了函数的名字 / 参数 / 返回值 → **必须同一次改动里更新本文件**，
  并把这部分改动单独提交一条：`docs: 更新接口契约（说明变了什么）`
- 契约与代码不一致时，**以代码为准并在群里报告**，不要按错的契约继续开发。
- 🚫 禁止绕过本文件去猜测别人函数的行为；有疑问在群里问人。
- 标 `[TODO]` 的部分由各模块实现后补全；未补全前不要凭猜测调用。

---

## 一、数据层 — `app/db.py`　维护人：C₁

🔴 **本文件之外不许写 SQL。** 其他模块要数据一律调用这里的函数；
`_tx()` 是 db.py 内部的事务封装，保持私有，不要跨模块使用。

本节函数的实现都归 C₁。唯一例外是 `mastery_table()`：
**行覆盖范围与返回列名由 C₁ 定死，实现放在第七节的 `profile.py`（C₂）** ——
因为它属于统计逻辑，C₁ 只提供 `practice_log` 的底层查询。

| 函数 | 说明 |
|---|---|
| `get_conn() -> sqlite3.Connection` | 返回已开启外键约束、支持字典式取值的连接。🔴 每个新连接都会执行 `PRAGMA foreign_keys = ON` |
| `init_db() -> None` | 按 schema.sql 建表。幂等，可反复调用 |
| `insert_chapters(list) -> int` | 灌入 15 个归类抽屉（不是知识点库） |
| `insert_wrong_question(data, image_path=None) -> int` | 写入错题及其知识点关联，返回新记录 id |
| `recent_wrong_questions(limit=20) -> list[dict]` | 最近录入的错题 |
| `wrong_question_count() -> int` | 错题总数 |
| `log_practice(kp_id, is_correct, question_ref=None, difficulty=None, duration_sec=None)` | 写练习记录 |
| `mastery_table() -> pandas.DataFrame` | 🔴 **列名已定死，见下方「mastery_table 返回列」** |
| `weak_points(n) -> list[dict]` | 排除样本不足后掌握度最低的 n 个 |
| `kp_error_distribution() -> list[dict]` | 错题按知识点分布 + 主要错因 |
| `cost_summary() -> dict` | 累计调用次数与 token 用量 |

### 表结构（六张，知识点是两级的）

```
chapter(id PK, name UNIQUE, brief)                    -- 15 个，一次性写死
knowledge_point(id PK, name UNIQUE, chapter_id FK,    -- 🔴 运行时自动新建
                explain, created_from, created_at)
wrong_question(id PK, question_text, student_answer, correct_answer,
               solution_steps, error_type, difficulty, source,
               image_path, created_at)
wq_kp(wq_id FK, kp_id FK)                             -- 多对多
practice_log(id PK, kp_id FK, question_ref, is_correct, difficulty,
             duration_sec, created_at)
llm_log(id PK, purpose, prompt_tokens, completion_tokens, retried,
        success, created_at)
```

### 🔴 `mastery_table()` 返回列（C₁ 在第 5 步定死，实现归 C₂）

C₂ 和 D 都依赖这一行，**改列名等于改接口**，必须按文件头的规矩同步本文件并单独提交一条 `docs:`。

| # | 列名 | 类型 | 含义 |
|---|---|---|---|
| 1 | `kp_id` | str | 知识点 id（`kp_` 开头） |
| 2 | `kp_name` | str | 知识点名称，如「洛必达法则」 |
| 3 | `chapter_id` | str | 所属章节 id（`ch_` 开头） |
| 4 | `chapter_name` | str | 章节中文名，界面直接展示用 |
| 5 | `attempts` | int | 练习总次数（`practice_log` 行数） |
| 6 | `correct` | int | 答对次数 |
| 7 | `correct_rate` | float | 基础正确率 = `correct / attempts`，0~1 |
| 8 | `difficulty_factor` | float | 难度加权系数：全易题 1.0 / 含中等 1.05 / 含难题 1.15 |
| 9 | `decay_factor` | float | 时间衰减系数：≤7 天 1.0 / 8~30 天 0.9 / >30 天 0.75 |
| 10 | `mastery` | float | 🔴 最终掌握度 = 第 7 列 × 第 8 列 × 第 9 列，**截断到 1.0** |
| 11 | `status` | str | 分级标签：`未掌握` / `基本掌握` / `已掌握` / `样本不足` |

约定：

- **行覆盖范围**：库里**全部**知识点都要出现，包括从没练过的（`attempts = 0`）。
  没练过的行第 7~10 列取 `0.0`，`status` 固定为 `样本不足` —— 否则界面上的表会缺行。
- **列顺序固定**为上表 1~11，D 可以直接按位置渲染。
- `status` 的取值是**中文标签**，与本文件第七节分级阈值表一一对应；
  C₂ 实现时按 `练过 ≥3 次` 的前置条件判定，不满足的一律 `样本不足`。
- `mastery` 的截断在最后一步做（先乘再截断），不是把 `difficulty_factor` 自己封顶。

---

## 二、知识点挂载 — `app/knowledge.py`　维护人：C₁

🔴 **这是整个"知识库从错题里长出来"设计的支点。**

| 函数 | 说明 |
|---|---|
| `chapter_candidates() -> list[dict]` | 15 个章节的 id / name / brief。唯一允许 AI 挂靠的层级 |
| `find_or_create(name_hint, chapter_id, context) -> str` | 🔴 核心：① 精确名字匹配 → ② 包含关系与关键词重叠 → ③ 命中就复用已有 id → ④ 都没命中才在该章节下新建 → ⑤ 返回知识点 id |
| `ensure_valid_chapter(chapter_id) -> bool` | 校验章节 id 合法 |
| `all_knowledge_points() -> list[dict]` | 现有全部知识点，供提示词注入候选列表 |

**为什么 find_or_create 不能跳过查重**：库里会同时出现「不定积分」「不定的积分」
「求不定积分」三条指同一件事的记录，掌握度统计算成三个不同知识点，功能③彻底作废。

---

## 三、AI 能力层 — `app/llm_client.py`　维护人：B

🔴 **全项目唯一允许发网络请求的文件。**

| 函数 | 说明 |
|---|---|
| `chat(messages, *, purpose, json_mode=False, schema_hint=None, validator=None, max_retry=2) -> LLMResult` | 主入口 |
| `vision(prompt_text, images, *, purpose, system=None, json_mode=False, schema_hint=None, validator=None) -> LLMResult` | 多模态 |
| `is_configured() -> bool` | 三项环境变量是否齐全 |
| `missing_config_keys() -> list[str]` | 缺哪几项 |
| `extract_json(text) -> dict \| None` | 容忍 markdown 包裹和前后废话 |

`LLMResult` 字段：`text / prompt_tokens / completion_tokens / reasoning_tokens / retried / success`
异常类型：`LLMError`（自定义，上层负责降级处理）

配置来源：环境变量 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（三项必填，无默认值）
可选：`LLM_TIMEOUT=120` / `LLM_MAX_TOKENS=4000` / `LLM_TEMPERATURE=0.0`

---

## 四、问答 — `app/qa.py`　维护人：B

| 函数 | 说明 |
|---|---|
| `retrieve_knowledge(question, limit=4) -> list[dict]` | 🔴 反向片段匹配。中文不能用 `split()` 切词 |
| `answer(question, subject, style, history=None) -> str` | 返回 markdown；越界问题按提示词要求拒绝 |
| `list_subject_topics() -> list[str]` | 界面展示"本系统覆盖哪些知识点" |

---

## 五、错题抽取 — `app/intake.py`　维护人：B

| 函数 | 说明 |
|---|---|
| `analyze_text(题干, 学生答案, 参考答案="") -> IntakeDraft` | 文本路径 |
| `analyze_image(image_bytes, extra_note="") -> IntakeDraft` | 拍照路径 |
| `commit(draft, image_path=None) -> int` | 🔴 用户确认后才落库 |

`IntakeDraft` 字段：`question_text / student_answer / correct_answer /
solution_steps / knowledge_ids / new_concepts / difficulty / error_type /
confidence / source / degraded / note`

`degraded=True` 表示模型没成功，界面要显示空表单让人手工填（主流程不断）。

### 抽取输出契约（模型必须严格遵守）

```json
{
  "question_text": "题干完整文字",
  "student_answer": "学生写的答案",
  "correct_answer": "正确答案",
  "solution_steps": ["推导步骤1", "推导步骤2"],
  "knowledge_ids": ["已有知识点id"],
  "new_concepts": [{"name": "建议的新知识点名", "chapter_id": "ch_xxx"}],
  "difficulty": "easy | medium | hard",
  "error_type": "枚举值之一",
  "confidence": 0.82
}
```

规则：
- `knowledge_ids` **优先复用**已有 id，最多 3 个
- `new_concepts` 只是**建议**，是否真的新建由 `find_or_create()` 决定（AI 不直接写库）
- `chapter_id` 必须是 15 个章节之一，🔴 禁止发明新章节
- `error_type` 只能是这五个值：

| 值 | 含义 | 判别 |
|---|---|---|
| `concept_confusion` | 概念混淆 | 公式/定义用错了 |
| `calculation_error` | 计算失误 | 思路对，中间算错 |
| `method_wrong` | 方法选错 | 用了不适用的解法 |
| `incomplete` | 审题不全 | 漏条件、漏解 |
| `unknown` | 判不出 | 兜底，界面转人工选择 |

---

## 六、校验器 — `app/schemas.py`　维护人：C₂

| 函数 | 说明 |
|---|---|
| `make_intake_validator(allowed_kp_ids) -> callable` | 输入解析后的 dict，合规返回 `None`，不合规返回错误描述字符串（会被回喂给模型重试） |
| `make_practice_validator(allowed_chapter_ids, expected_min=1) -> callable` | 校验生成的练习题 |

拦截项：不是合法 JSON / 缺必填字段 / 🔴 `knowledge_ids` 越界 / 超过 3 个 /
`new_concepts` 的 `chapter_id` 不在章节集内 / `error_type` 不在枚举 /
`solution_steps` 非空数组 / `confidence` 不在 0~1 / `difficulty` 不在三个值内

---

## 七、掌握度与练习 — `app/profile.py`　维护人：C₂

| 函数 | 说明 |
|---|---|
| `mastery_table() -> DataFrame` | 🔴 **实现归 C₂**。掌握度 = 基础正确率 × 难度加权 × 时间衰减；行覆盖范围与返回列名由 C₁ 定死（见第一节），改动列名等于改接口 |
| `summary_text() -> dict` | 界面顶部指标卡数据，键名见下方「summary_text 返回」 |
| `build_practice_session(count) -> (list[dict], str)` | 返回题目列表 + 来源说明（如「本次来自薄弱知识点」） |
| `grade(item, user_answer) -> bool` | 客观判分，规则见下方「题目结构与判分」 |
| `record_result(items, answers) -> list[bool]` | 判分 + 回写 + 错题回流；返回每题是否答对 |

### 🔴 `summary_text()` 返回（界面顶部指标卡，D 直接按这些键读）

| 键 | 类型 | 含义 |
|---|---|---|
| `kp_total` | int | 知识点总数（含从没练过的） |
| `wrong_total` | int | 错题总数 |
| `practice_total` | int | 练习总次数 |
| `weak_count` | int | 判定为「未掌握」的知识点个数 |
| `llm_calls` | int | 模型调用累计次数 |
| `llm_tokens` | int | 累计 token 用量 |

键名固定，增删键要同步本文件 —— D 的界面直接按下标读这几个键。

### 🔴 题目结构（`build_practice_session` 产出 / `question_bank.json` 存储 / `grade` 消费）

三方共用同一个形状，**任何一方私自加字段之前先改这一节**。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | 题目唯一标识，`qp_` 前缀 |
| `type` | str | `choice` 单选 / `fill` 填空。**v1 只做这两种**，不做多选和判断 |
| `chapter_id` | str | 归属章节，必须是 15 个之一 |
| `kp_ids` | list[str] | 考察的 1~3 个知识点 id |
| `stem` | str | 题干，markdown |
| `options` | dict[str, str] | `choice` 必填，如 `{"A":"…","B":"…","C":"…","D":"…"}`；`fill` 为空 dict |
| `answer` | str | `choice` 存选项编号（`"B"`）；`fill` 存标准答案文本 |
| `explanation` | str | 解析，作答后展示 |
| `difficulty` | str | `easy` / `medium` / `hard` |

示例：

```json
{
  "id": "qp_0001",
  "type": "choice",
  "chapter_id": "ch_mean_value",
  "kp_ids": ["kp_洛必达法则"],
  "stem": "求极限 $\\lim_{x\\to 0}\\frac{\\sin 3x}{x}$",
  "options": {"A": "0", "B": "1", "C": "3", "D": "不存在"},
  "answer": "C",
  "explanation": "等价无穷小：sin3x ~ 3x，故极限为 3。",
  "difficulty": "easy"
}
```

### `grade()` 判分规则

| 题型 | 规则 |
|---|---|
| `choice` | 与 `answer` 比对选项编号，**大小写与首尾空格宽容**（`"b"` 视为 `"B"`） |
| `fill` | 去掉所有空格后比对；等价写法（如 `1/2` 与 `0.5`）由 C₂ 自行决定是否支持，支持则写进本节 |
| 其他 | v1 不支持，遇到未知 `type` 直接返回 `False` 并记一条警告日志 |

判分必须是**纯函数**：同一个 `(item, user_answer)` 反复调用结果一致，不访问网络、不读写数据库。

系数（写在 SQL 里，确定性可调）：

```
掌握度 = 基础正确率 × 难度加权系数 × 时间衰减系数
```

- **难度加权**：全易题 1.0 / 含中等 1.05 / 含难题 1.15
- **时间衰减**：≤7 天 1.0 / 8~30 天 0.9 / >30 天 0.75
- 🔴 **封顶对象要说清楚**：最终掌握度截断在 1.0（即 100%），
  不是"难度系数本身封顶"——否则 ×1.15 永远被截掉，"难题答对更能证明掌握"就形同虚设。
  实际效果是：难题全对的知识点会顶到 100%，而易题为主的只能到 0.9×1.0 附近。

分级阈值（🔴 **前三档都必须带"练过 ≥3 次"这个前置条件**）：

| 条件 | 状态 |
|---|---|
| 练过 ≥3 次 且 掌握度 <40% | 🔴 未掌握 |
| 练过 ≥3 次 且 掌握度 <70% | 🟡 基本掌握 |
| 练过 ≥3 次 且 掌握度 ≥70% | 🟢 已掌握 |
| 练过 <3 次 | ⚪ 样本不足（不参与"最薄弱"排名） |

⚠️ 漏掉"≥3 次"前置条件的后果：一个只练过 1 次且答错的知识点会被直接判成
"未掌握"并霸占榜首 —— 正是 PLAYBOOK 明令要避免的那个缺陷。

---

## 八、界面 — `streamlit_app.py` + `app/ui_theme.py`　维护人：D

只允许调用上面各节的函数。🔴 **不许出现 SQL、不许发网络请求、不许引入前端框架。**

四个标签页：① 知识点问答 ② 错题录入 ③ 掌握度分析 ④ 针对性练习

### 界面分两层

| 文件 | 职责 | 约束 |
|---|---|---|
| `streamlit_app.py` | 交互与布局：组件、状态、调用后端 | 不写 SQL、不发网络 |
| `app/ui_theme.py` | 视觉层：注入 CSS 与主题色 | 只产出外观；不碰业务、不读写 `session_state` |
| `.streamlit/config.toml` | Streamlit 原生主题色 | 删掉不影响功能，只是回到默认配色 |

🔴 **可整段回退**：删掉 `ui_theme.py` 和 `.streamlit/config.toml`，界面应回到原生样式且功能完好。做不到这一点说明视觉层里混进了逻辑，要拆出来。

⚠️ 开场白只做页面渲染，不写进对话历史（否则发给模型的序列以 assistant 开头会报错）。

---

## 九、桩函数 — `app/stubs.py`　维护人：D

队友代码没好时用来跑通界面。🔴 返回的数据形状必须与本文件约定的完全一致。

### 切换机制（门面，不需要人工改 import）

`streamlit_app.py` 只写 `from app import stubs as backend`。`stubs.py` 里每个函数先找真实现
（`app.qa` / `app.intake` / `app.profile` / `app.knowledge` / `app.db`），**导入成功就转发，
否则回落桩**。所以队友的模块一进 main，页面自动切真数据，界面代码一行都不用改。

**导入期**的任何异常（`ImportError`、`SyntaxError`、模块顶层抛的 `RuntimeError` 等）都只回落桩并记进
`problems()`，不再向外抛——否则一个语法错误就会把页面打成 traceback。
**运行期**（真实现被调用时）的异常一个字都不接，照常向上抛：模型 401、SQL 写错不会被伪装成「桩在跑」。

### 三种运行模式（`TUTOR_BACKEND`）

只读这一个环境变量，不碰 `.env`、不读密钥；改完要重启进程（页面或命令）才生效。

| 值 | 行为 | 用途 |
|---|---|---|
| `auto`（默认） | 能导入就用真实现，导入不了回落桩 | 日常开发 |
| `stub` | 全部强制用桩，一个真实现都不调 | 演示：没配 API Key 也不报错，页面不出现任何真实数据 |
| `real` | 必须是真实现；缺一个就在页面顶部 `st.error` 列出函数名 | 交付前自查「到底接上了没」 |

非法值不会抛异常（否则整页白屏），只在 stderr 打一行警告并回落到 `auto`。

### 可观测性：`sources()` / `problems()` / `summary()`

门面静默回落有个致命缺点：队友的模块导入失败时页面看起来完全正常，
没人有理由去查。所以有三个函数把状态摊开：

```
sources()  -> {函数名: "real" | "stub"}        # 格式固定，界面靠它判断，别改
problems() -> {函数名: 原因}                    # 只列「模块存在但接不上」，正常缺文件不列
summary()  -> "真 9 / 桩 4（其中 1 个异常）"    # 一行汇总，异常为 0 时不带后半句
```

`problems()` 判三种情况：模块文件不存在 → 正常，不记录；模块存在但**导入报错** → 记录
（多半是 `from .prompts import X` 写错，或语法错误；原因里带异常类型，如
`模块存在但导入失败（SyntaxError）：invalid syntax (qa.py, line 3)`）；
模块能导入但没有这个函数 → 记录（签名/名字对不上）。

界面状态条显示 `◈ 接入 真 N / 桩 M`，有异常时同一行追加 `⚠️ X 个接入异常` 并红字列出，
下方 `查看 N 个函数的接入状态` 展开是「函数 / 当前 / 归谁实现 / 异常原因」逐行表。

查当前状态（别靠记忆）：

```powershell
python -c "from app import stubs; import json; print(json.dumps(stubs.sources(), ensure_ascii=False, indent=2)); print(stubs.summary()); print(stubs.problems())"
```

### 谁实现哪些函数（归属表）

🔴 **「当前是真还是桩」不写在本节里** —— 它随队友合并随时变化，写死必然过期。
想看当前值：跑上面那条命令，或看界面状态条。

| 函数 | 真来源模块 | 维护人 |
|---|---|---|
| `answer()` | `app.qa` | B |
| `analyze_text()` / `analyze_image()` / `commit()` | `app.intake` | B |
| `mastery_table()` / `summary_text()` / `build_practice_session()` / `grade()` / `record_result()` | `app.profile` | C₂ |
| `all_knowledge_points()` / `chapter_candidates()` | `app.knowledge` | C₁ |
| `recent_wrong_questions()` / `wrong_question_count()` | `app.db` | C₁ |
| `is_configured()` / `missing_config_keys()` | 本地读环境变量（不联网） | D |
| `sources()` / `problems()` / `summary()` / `mode()` / `facade()` / `db_ready()` | 本节自有工具函数 | D |

### 待对齐

`answer()` 的 `style` 参数目前由界面传中文 `"简洁"` / `"详细"` 原样透传。
B 的 `qa.py` 落地后若改成英文枚举，只改 `app/stubs.py` 门面一处，界面不动。
