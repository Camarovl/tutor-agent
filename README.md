# 学习辅导 AI 助教 · 高等数学

面向大学《高等数学》课程的智能辅导应用：把做错的题沉淀成知识点画像，再按薄弱环节给出讲解与练习。

> ⚠️ **当前状态**：仓库已完成初始化（目录结构、依赖、数据骨架、接口契约）。
> 功能代码正在开发中，本文档会随进度更新。

## 📍 新组员从这里开始

```bash
git clone https://github.com/Camarovl/tutor-agent.git
cd tutor-agent
```

然后按顺序读三份东西：

| 顺序 | 文件 | 做什么 |
|---|---|---|
| 1 | `plan/PROJECT_PLAYBOOK.md` 第四节 | 看清自己是 A / B / C₁ / C₂ / D / E 中的哪个、每步该做什么 |
| 2 | `plan/DSH_DAILY_PROMPTS.md` 第 0 节 | 整段复制发给自己的 AI（第一次对话必发） |
| 3 | `docs/interfaces.md` | 🔴 你要调用的函数长什么样 —— 跨人协作的唯一依据 |

环境和 git 操作看 [git-test 仓库的 README](https://github.com/Camarovl/git-test)，这里不重复。

### 目录结构

```
根目录 / app/ / tests/ / data/     代码与其配置（写功能的地方）
docs/                              interfaces.md 契约 + design/issues/limitations/collaboration
plan/                              排期与话术（组长用，写功能时不用翻）
AGENTS.md                          🤖 组员的 AI 自动读取的规则文件
```

⚠️ `plan/` 是过程资料，交付前可整体删除；其余都是评分对象。

### 当前开发进度

仓库已具备开工条件，但**业务代码尚未开始写** —— 那是各人从第 4 步起各自的工作。

| 部分 | 状态 |
|---|---|
| 目录骨架、依赖、忽略规则、Dockerfile | ✅ 就位 |
| 15 个归类抽屉 `data/chapters.json` | ✅ 就位 |
| 函数契约 `docs/interfaces.md` | ✅ 九节骨架就位，`[TODO]` 由各维护人补 |
| `app/` 下九个模块 | ⏳ 待第 4 步按分工创建 |
| `schema.sql`、`streamlit_app.py`、`seed_data.py` | ⏳ 待第 4~5 步 |
| `tests/offline_check.py` | ⏳ 待第 5 步由 C₂ 创建 |

所以照第三节执行时，**现在只有前两步能跑通**（装依赖、配 `.env`）；
第 3~5 步要等对应文件被创建出来，那正是第 4 步要做的第一件事。

---

## 一、项目说明

### 解决什么问题

直接拿通用对话模型当助教，效果停留在"这道题答案是什么"。真正的学习需要闭环：
讲懂一个知识点 → 记下做错的题 → 针对薄弱环节再练 → 看到自己的进度。

我们把学科限定为**高等数学**，因为收窄范围才能把知识点组织起来、让错题归类合理、
使生成的练习真正落在薄弱点上。

### 四个功能板块

| # | 功能 | 说明 |
|---|---|---|
| ① | **知识点问答** | 结构化讲解（定义 / 通俗解释 / 例子 / 常见误区），回答限定在课程范围内 |
| ② | **错题录入与自动归类** | 手输或拍照 → 抽取题干、判定错因、挂载知识点 → **用户确认后入库** |
| ③ | **掌握度分析** | 按知识点统计正确率，结合难度与时间衰减给出掌握度估计，标出薄弱点 |
| ④ | **针对性练习** | 依据薄弱知识点出题，作答后判分并回写，做错的题回流错题本 |

### 核心设计

🔴 **知识点库不预先穷举，而是从真实错题里长出来。**

- 第一层：15 个教材章节（`data/chapters.json`，一次性写死）—— 只是归类抽屉的名字
- 第二层：具体知识点（洛必达法则、分部积分法…）由系统在问答和错题归类时自动创建并挂靠

这样既不必人工列几十个知识点，又保证归类不会散架。配套约束是新建前必须查重，
否则会出现「不定积分」「不定的积分」两条并存，掌握度统计作废。

**大模型只负责两件事**：解释知识点、把非结构化输入转成结构化 JSON。
统计、判分、分级全部是普通 Python + SQL —— 结果可复现、成本低、出错易定位。

---

## 二、技术栈与架构

```
Streamlit        界面（纯 Python，无前端框架）
SQLite           单文件数据库，裸 SQL，不用 ORM
openai SDK       走 OpenAI 兼容协议对接任意模型服务
Dockerfile       容器化交付
Python 3.11+
```

🔴 不使用 LangChain / LlamaIndex 等 AI 框架，直接调用 openai SDK。

### 分层

```
streamlit_app.py    界面入口，只做展示交互
                    🔴 不含业务逻辑、不发网络请求、不写 SQL
app/
  llm_client.py     🔴 全项目唯一允许发网络请求的文件
  prompts.py        所有提示词集中在这里
  schemas.py        校验大模型返回的 JSON 是否合规
  db.py             🔴 全项目唯一碰数据库的文件，SQL 一律 ? 参数化
  knowledge.py      知识点的查找 / 相似度匹配 / 新建挂靠
  qa.py             ① 知识点问答
  intake.py         ② 错题录入与归类
  profile.py        ③④ 掌握度与练习
  stubs.py          桩函数（队友代码未就绪时先用它跑界面）
schema.sql          建表语句
data/               chapters.json（15 章）、question_bank.json（题库）
seed_data.py        演示数据一键灌入
tests/              offline_check.py 离线自检（不需要联网和模型服务）
docs/               interfaces.md 接口契约、design.md、issues.md、limitations.md
```

好处：换模型供应商时只改 `.env` 三个变量，代码一行不动。

详细决策理由见 [docs/design.md](docs/design.md)。

---

## 三、环境搭建与启动

### 1. 装依赖

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell
# source .venv/bin/activate     # Linux / macOS
pip install -r requirements.txt
```

### 2. 配置模型服务（🔴 必须，代码里没有默认地址）

```powershell
Copy-Item .env.example .env     # PowerShell；Linux/macOS 用 cp .env.example .env
```

编辑 `.env`，填三项：

```ini
LLM_BASE_URL=                   # API 地址，以 /v1 结尾
LLM_API_KEY=                    # 密钥
LLM_MODEL=                      # 模型名，与供应商文档写法完全一致
LLM_MAX_TOKENS=4000             # 带思维链的模型需要较大值
```

也可以不设文件、直接导出环境变量：

```powershell
$env:LLM_BASE_URL="https://api.deepseek.com/v1"
$env:LLM_API_KEY="sk-你的密钥"
$env:LLM_MODEL="deepseek-chat"
```

🔴 `.env` 已被 `.gitignore` 排除，**切勿提交真实密钥**。

### 3. 初始化演示数据 ⏳ 待第 5 步

```bash
python seed_data.py
```

输出各类数据条数和当前最薄弱的三个知识点。脚本可重复运行。

⏳ 脚本由 D 在第 5 步创建。在那之前这一步会报「找不到文件」，属正常。

### 4. 启动 ⏳ 待第 4 步

```bash
streamlit run streamlit_app.py
```

浏览器打开 http://localhost:8501 。

⏳ 入口文件由 D 在第 4 步创建（先做四个标签页的空壳）。

### 5. 离线自检（不需要模型服务）⏳ 待第 5 步

```bash
python tests/offline_check.py
```

覆盖数据完整性、掌握度查询边界、输出校验器、判分逻辑。
🔴 每天开工先跑一次 —— 红了说明主干是坏的，先报告别硬写。

⏳ 脚本由 C₂ 在第 5 步创建；在那之前仓库里还没有它。

---

## 四、Docker 运行

```powershell
# PowerShell
docker build -t tutor-agent .
docker run -d -p 8501:8501 -e LLM_BASE_URL="https://..." `
           -e LLM_API_KEY="sk-..." -e LLM_MODEL=xxx `
           -v "${PWD}\data:/app/data" --name tutor tutor-agent
docker exec tutor python seed_data.py
```

```bash
# Linux / macOS
docker build -t tutor-agent .
docker run -d -p 8501:8501 -e LLM_BASE_URL="https://..." \
           -e LLM_API_KEY="sk-..." -e LLM_MODEL=xxx \
           -v "$(pwd)/data:/app/data" --name tutor tutor-agent
docker exec tutor python seed_data.py
```

| 项 | 值 |
|---|---|
| 端口 | 8501 |
| 持久化路径 | 容器内 `/app/data/app.db`，通过 `-v` 映射到宿主机 `./data` |
| 环境变量 | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 用 `-e` 传入，镜像内不含任何密钥 |
| 首次进入容器 | 需执行一次 `python seed_data.py` 建库灌数据 |

⚠️ **Dockerfile 尚未经过实机构建验证**（开发环境没有安装 Docker）。
首次在装了 Docker 的机器上 `docker build` 时请记录报错并修正，这条已列入
[docs/limitations.md](docs/limitations.md) 的待补项。

---

## 五、功能清单与验证方式

| 编号 | 操作 | 期望结果 |
|---|---|---|
| V1 | ① 页提问一个课程概念 | 返回四段结构：一句话定义 / 通俗解释 / 具体例子 / 常见误区 |
| V2 | ① 页提问非本课内容 | 🔴 被礼貌拒绝，并说明只覆盖高等数学 |
| V3 | ② 页手输一道错题 + 错误答案 | 抽出题干、给出正确答案、归到候选知识点、判定错因 |
| V4 | ② 页修改自动识别的知识点后入库 | 以修改结果为准（确认环节生效） |
| V5 | ③ 页查看掌握度表 | 全部知识点出现（含从没练过的），薄弱项标红且不霸榜 |
| V6 | ④ 页做题并故意答错 | 判分正确、该知识点掌握度下降、题目回流错题本 |
| V7 | `python tests/offline_check.py` | 全部通过（共 19 项），退出码 0 |
| V8 | `docker run` 后浏览器打开 8501 | 完整可用 |

状态：V1~V6、V8 待对应功能完成后逐项验证；V7 的自检脚本在第 5 步创建后才可运行。

---

## 六、团队分工

| 代号 | 职责 | 主要负责文件 |
|---|---|---|
| **A 组长** | 定范围、催进度、拍板砍功能、创建 PR 与合并、项目汇报主讲 | `docs/`、`README.md` |
| **B** | 模型调用封装、提示词设计与调优、抽取与问答逻辑 | `app/llm_client.py`、`prompts.py`、`qa.py`、`intake.py` |
| **C₁** 🔴先动 | 数据地基：表结构、连接管理、章节灌入、知识点动态挂载与查重 | `schema.sql`、`db.py`、`knowledge.py`、`data/chapters.json` |
| **C₂** | 统计与判定：输出校验器、掌握度估计、出题选池、判分回写、离线自检脚本 | `app/schemas.py`、`profile.py`、`tests/offline_check.py` |
| **D** | 界面交互、桩函数体系、演示数据 | `streamlit_app.py`、`stubs.py`、`seed_data.py`、`data/question_bank.json` |
| **E** | 依赖版本、忽略规则、Docker、部署上线、汇报材料排版 | `Dockerfile`、`requirements.txt`、`.gitignore`、`.dockerignore`、`.env.example` |

💡 本项目 **A 与 E 由同一人承担**。职责仍按两个角色分开列，是为了看清哪些属于配置类工作 ——
提交时也请分成两条 commit（一条 `docs:`、一条 `chore:`）。

协作规矩见 [docs/collaboration.md](docs/collaboration.md)，
跨人同步依据是 [docs/interfaces.md](docs/interfaces.md)（函数契约）。

---

## 七、已知问题与未完成部分

见 [docs/limitations.md](docs/limitations.md)。踩坑记录见 [docs/issues.md](docs/issues.md)。
