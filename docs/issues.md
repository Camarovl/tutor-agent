# 踩坑记录

开发过程中遇到的问题和解决方式。排障时先翻这份。

---

## 1. LangChain 全家桶不可用（pydantic v1/v2 混装）

**现象**

```
RuntimeError: no validator found for <class 'langchain_core.language_models.base.BaseLanguageModel'>,
see `arbitrary_types_allowed` in Config
```

报错发生在 `from langchain.memory import ConversationBufferMemory`，但调用栈显示真正失败的是
同包内我们**完全没用到**的 `ConversationSummaryMemory`。

**原因**：`langchain` 0.2.x 内部用 pydantic v1，`langchain-core` 0.3.x 起用 v2。
Python 执行 `from package import X` 会跑完整个包的 `__init__.py`，因此被连坐。

**尝试过的方案**

| 方案 | 结果 |
|---|---|
| 只绕开 `langchain.memory`，改用 `langchain_openai.ChatOpenAI` | ❌ 换成 `PydanticUserError: __modify_schema__ is not supported in Pydantic v2 ... in class SecretStr`，连 import 都过不去 |
| 升级 langchain 全家桶 | ⚠️ 理论可行，但会导致五台机器版本漂移，与「requirements.txt 版本写死」冲突 |
| **改用 openai SDK 直连** | ✅ 采用。依赖链最短，上下文拼装全可见 |

**教训**：依赖版本要一次性对齐并写死，别让每个人的环境各自演化。

---

## 2. 模型返回空内容（思考型模型的 token 陷阱）

**现象**：请求成功、无异常，但界面什么都不显示。打印完整响应才看清：

```
finish_reason: length
completion_tokens: 800
reasoning_tokens: 799      ← 全部预算花在内部推理上，可见输出为零
```

**原因**：带思维链的模型会先在内部"想"很久再作答。`max_tokens` 设小了，它想完就没额度了。

**处理**：默认值提到 `LLM_MAX_TOKENS=4000`；同时在 `llm_client.chat()` 里显式检测这个症状，
抛出可读的错误信息而不是静默返回空串：

```python
if not result.text:
    last_err = (f"模型返回空内容（finish_reason={finish}，"
                f"reasoning_tokens={result.reasoning_tokens}/{result.completion_tokens}）。"
                "若使用的是带思维链的模型，请调大 LLM_MAX_TOKENS，或改用非思考模型。")
```

`.env.example` 和侧边栏提示里也都写了这条 —— 这是最容易让人以为"代码坏了"的坑。

---

## 3. jinja 模板拒绝消息序列

**现象**：**只有第一次提问失败**，之后重试就正常了。

```
Error code: 400 - {'error': 'Error rendering prompt with jinja template:
"No user query found in messages."'}
```

**定位过程**：把不同消息结构逐个发给本地服务对比——

| payload | 结果 |
|---|---|
| `[system, user]` | ✅ OK |
| `[system, assistant, user]` | ❌ 400 |
| `[user]` | ✅ OK |

结论：模板不接受以 assistant 开头的对话轮。而开场白是硬编码的第一条历史消息：

```python
st.session_state["messages"] = [{"role": "assistant", "content": "你好，我是学习助手…"}]
```

Streamlit 每次交互都会从头重跑脚本，首轮请求就可能带上它。

**处理**：开场白只做界面渲染、不写入历史，保证 payload 恒为 `[system, user, ...]`。
从根上消除了这类报错。

---

## 4. AI 会自己发明知识点名称 🔴

**现象**：同一类题目被归成「二次函数图像」「二次函数的图象」「抛物线性质」三条。

**后果**：掌握度统计按 ID 分组，会把一个知识点算成三个，功能③彻底作废。

**原因**：让模型自由命名标签，它每次措辞都可能不同。

**处理**：三层防线

```
候选集注入 prompt（只允许选已有 id）
   → 校验器拦截越界 ID
      → 失败时把具体原因回喂给模型重试（不是盲目重问）
         → 仍失败则降级为人工选择
```

同时新建知识点前必须查重（精确匹配 → 包含关系 → 关键词重叠），
避免运行时长出同义条目。

---

## 5. SQLite 外键静默失效

**风险点**：SQLite 的 `PRAGMA foreign_keys = ON` **是按连接的，不是按数据库的**。
建库时设过一次，之后每个新连接仍然是关闭状态。

后果很隐蔽：可以插入指向不存在章节的错题，程序不报错，直到统计时才发现数据脏了。

**处理**：收口到 `db.get_conn()` 一处，每个连接都执行一遍；
界面层的知识点下拉框只从数据库读取现有 id，双向保证不会出现越界引用。

---

## 6. 从没练过的知识点霸占"最薄弱"榜首

**现象**：刚建的知识点没有任何练习记录，正确率算出来是 NULL 或 0，
于是永远排在"最薄弱前三"，导致练习一直推没人练过的题。

**处理**：SQL 三级排序 + 「样本不足」单独一档

```sql
ORDER BY
  CASE WHEN 掌握度 IS NULL THEN 1 ELSE 0 END,     -- ① 没数据的沉底
  CASE                                            -- ② 档位序
    WHEN 练习数 >= 3 AND 掌握度 < 40 THEN 0
    WHEN 练习数 >= 3 AND 掌握度 < 70 THEN 1
    WHEN 练习数 >= 3                 THEN 2
    ELSE                                  3       -- 样本不足排最后
  END,
  掌握度 ASC;                                     -- ③ 同档内升序
```

并且除法一律用 `NULLIF(COUNT(...), 0)` —— SQLite 除零返回 NULL 而不报错，
会一路污染到前端。

---

## 7. `NULLS LAST` 在旧版 SQLite 报语法错误

**现象**：本机跑得好好的排序语句，换一台机器直接

```
sqlite3.OperationalError: near "LAST": syntax error
```

**原因**：`NULLS FIRST/LAST` 需要 SQLite ≥ 3.30，而某些 Python 发行版自带的库版本更旧。

**处理**：统一改用兼容写法

```sql
ORDER BY CASE WHEN xxx IS NULL THEN 1 ELSE 0 END, xxx ASC
```

已验证两种写法输出完全一致。

---

## 8. Windows GBK 控制台导致脚本崩溃

**现象**：换一台中文 Windows 机器照文档执行第一条命令就失败，退出码 1：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f534'
```

**原因**：中文 Windows 控制台默认 GBK，而脚本输出含 emoji（✅❌🔴）。
实测 `chcp 65001` 也救不了 —— Python 仍按 locale 编码 stdout。

**处理**：每个入口脚本开头加

```python
import sys
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        PASS, FAIL = "[OK]", "[X]"      # 退回 ASCII 标记
```

这是"在别人机器上跑不起来"最常见的成因之一，成本极低但必须做。

---

## 9. PowerShell 传中文导致 commit message 乱码

**现象**：仓库历史里出现过

```
docs: ?? main ????????? PR ???? (#2)
```

不是终端显示问题 —— 乱码真的写进了 commit 对象。

**原因**：PowerShell 向原生程序传中文参数时会发生编码转换失败，
实测字节数从 24 降到 8（每个汉字变成一个 `?`）。

**处理**：涉及中文的提交信息一律先用 UTF-8 无 BOM 写入临时文件，
再 `git commit -F <file>`，并用 .NET 进程以 UTF-8 显式解码回读验证。

同理：任何含反引号或中文的文件改写都走脚本文件，不经 shell 传参 ——
`` `app `` 会被解析成 BEL 控制字符，实际写出过 `\x07pp/llm_client.py` 这种坏路径。

---

## 10. 校验器自身的 bug（被自己的测试抓到）

**现象**：写完练习题校验器跑单测，一道合法题被判非法。

题目四选一、答案 `"C"`、选项有 A/B/C/D —— 本该通过。问题出在：

```python
if str(it["answer"]) not in opts:     # opts 是 {"A": "...", "B": "..."}
```

大小写不一致时（模型返回 `"c"`）会误拒，而且错误信息写成"选项不足"，指错了方向。

**修正**：

```python
keys = {str(k).strip().upper() for k in opts}
if str(it["answer"]).strip().upper() not in keys:
    return f"标准答案 {it['answer']!r} 不是有效的选项编号，可选编号为 {sorted(keys)}"
```

**教训**：校验层自己也需要测试。现在 `tests/offline_check.py` 里有专门分支覆盖这一点。

---

## 11. 演示数据的排行榜没有规律

**现象**：第一版用纯随机生成练习记录，结果掌握度排行全是 50% 上下，
看不出"哪个知识点薄弱"，演示效果等于零。

**处理**：改成手工设定分布 —— 明确指定两三个知识点落在 33%~40%（🔴 未掌握）、
几个 57%~67%（🟡）、几个 ≥75%（🟢），另留一两个次数 <3 用来验证「样本不足」降权逻辑。
并固定随机种子，保证任何人跑出来的结果一致。

---

## 12. 高数公式识别准确率依赖模型 ⚠️ 持续存在

积分号 `∫`、极限 `lim`、上下限、分数结构的 OCR 出错率明显高于简单公式。

**应对**：不追求全自动。官方要求「识别结果允许用户确认或修正后再入库」，
所以②页设计了完整的可编辑确认表单 —— 识别错就手动改，改完照样正常入库和统计。

**已知残留**：图片旋转、手写潦草、多题混拍三种情况识别率明显下降，未做预处理。

---

## 13. 错题本表格把 LaTeX 源码原样显示出来 ⚠️ 待修

**现象**

②错题录入页下方「最近录入」表格里，题干一列显示成

```
求极限 $\lim_{x\to 0}\frac{\sin 3x}{x}$
```

用户看到的是反斜杠命令，不是数学式子。

**原因**

`streamlit_app.py` 的 `_recent_intake()` 用 `st.dataframe` 渲染表格，
而 **`st.dataframe` 只认纯文本，不跑 KaTeX（LaTeX 渲染引擎）**。
另一边 `st.latex` 虽然能渲染，但塞进表格格子会把布局撑破，两者不能混用。

同一份数据在不同控件里的表现不一致：

| 控件 | 能否渲染 LaTeX |
|---|---|
| `st.markdown` / `st.latex` / `st.chat_message` | ✅ 能 |
| `st.dataframe` / `st.caption` / `st.text_area` / `st.text_input` | ❌ 不能，原样输出源码 |

这是「数据本身没问题、换个控件就露馅」的典型：数据库里存的 LaTeX 是对的，
问答页和练习页都显示正常，唯独表格这一处破功。

**处理方向（归 D，界面层）**

表格不做精确排版，只在显示时降级成可扫读的纯文本近似：

```
$\lim_{x\to 0}\frac{\sin 3x}{x}$   ->   lim(x→0)(sin 3x)/(x)
$\int 2x\,dx$                      ->   ∫ 2x dx
```

约定：

- 转换函数放视觉层 `app/ui_theme.py`，命名 `plain_math(text) -> str`
- 🔴 **只改显示，不动数据库里的 `question_text`** —— 存进去仍是 LaTeX，
  这样以后换到能渲染的控件（详情页、导出）时不用重新清洗数据
- 要精确排版的地方（确认表单、练习页）仍然走 `st.latex` / `st.markdown`

**踩坑提醒**

写转换时有个顺序陷阱：`\lim_{x\to 0}` 必须先整体识别，
再做通用的下标转换。否则会先变成 `lim_(x→0)`，就再也匹配不上极限规则了。

**已确认需要的用例**（都是真实数据）

```
$\lim_{x\to 0}\frac{\sin 3x}{x}$      ->  lim(x→0)(sin 3x)/(x)
$\lim_{x\to 0}\dfrac{\sin 3x}{x}$     ->  lim(x→0)(sin 3x)/(x)
计算不定积分 $\int 2x\,dx$              ->  计算不定积分 ∫ 2x dx
$y' + y = 0$ 的通解是？                 ->  y' + y = 0 的通解是？
$\frac{a+b}{c}+\sqrt{x^2+1}$          ->  (a+b)/(c)+(x^2+1)
```

**状态**：待修（界面层，D）。`docs/limitations.md` 里不必记 —— 这属于显示缺陷，不是能力缺失。
