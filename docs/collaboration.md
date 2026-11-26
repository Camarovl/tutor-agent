# 协作规矩

多人各自使用自己的电脑开发，靠 GitHub 同步。以下规矩是**硬要求**，不是建议。

🤖 本文的机器可读版本是根目录的 `AGENTS.md`，组员的 AI 助手会自动读取它。

---

## 一、日常动作

| 时机 | 做什么 |
|---|---|
| 开工第一件事 | `git pull`（在自己的分支上先同步 main） |
| 做完一小件事 | 🔴 **每次完成一部分功能就 commit 一次**，`git add <文件>` → `git commit -m "说清这次改了什么"` |
| 收工最后一件事 | `git push` 到自己的分支，然后同步一声 |
| 心里没底时 | `git status` |

**每天同步一次 main** 的理由：你和别人各憋两周不合，最后合并时冲突多到没法收拾。
小步同步 = 每次只处理一点点冲突。这是团队协作里最值钱的习惯。

```bash
git switch feature/login
git pull origin main        # 把主干最新进展并进来
```

---

## 二、谁能做什么 🔴

| 动作 | 权限归属 |
|---|---|
| 在自己分支上 commit / push | ✅ 每个人 |
| **创建 Pull Request** | **只有维护者（组长）** |
| **点 Merge** | **只有维护者（组长）** |
| 直推 `main` | ❌ 机器已拦截（分支保护），谁都推不动 |
| `git push --force` / `git reset --hard` 后推送 | 🚫 **全组禁用** |

贡献者的职责链到 push 就结束：

```bash
git switch -c feature/xxx
# ...干活...
git add <文件> && git commit -m "feat: ..."
git push -u origin feature/xxx
# 然后同步一句「feature/xxx 推好了，本地跑通了」，等维护者合并
```

⚠️ GitHub 网页上那个绿色的 **Merge pull request** 按钮对协作者也是可点的（Write 权限自带），
拦靠的是这条约定而不是机器。**手滑点了就是事故。** 同理别点 Close pull request。

也别让自己的 AI 替你合并 —— 它有你的凭据，完全有能力绕过流程。

---

## 三、跨人对接：靠文件，不靠对话

| 同步方式 | 做法 |
|---|---|
| 唯一的契约文件 | `docs/interfaces.md` —— 函数名、参数、返回值、维护人 |
| 改了函数签名 | 🔴 **同一次改动里更新契约文件**，并单独提交一条 `docs: 更新接口契约（xxx）` |
| 要用队友的功能 | 读契约 → 不够清楚再读源码 → 还有疑问就在群里问人 |
| 队友的代码还没写好 | 在 `app/stubs.py` 造桩函数返回预期形状的数据，先把界面跑起来 |
| 每天收工 | 让 AI 生成三句话变更总结发到群里 |

🚫 **禁止**：让两个 AI 互相传话、把自己的 AI 输出截图让队友的 AI 解读、
绕过契约文件猜测别人函数的行为。

---

## 四、文件所有权（防冲突第一手段）

一个文件只有一个负责人，别人要改先打招呼。

| 文件 | 负责人 |
|---|---|
| `streamlit_app.py`、`app/ui_theme.py`、`.streamlit/config.toml`、`app/stubs.py`、`seed_data.py`、`data/question_bank.json` | D |
| `app/llm_client.py`、`app/prompts.py`、`app/qa.py`、`app/intake.py` | B |
| `schema.sql`、`app/db.py`、`app/knowledge.py`、`data/chapters.json` | C₁ |
| `app/schemas.py`、`app/profile.py`、`tests/offline_check.py` | C₂ |
| `Dockerfile`、`.dockerignore`、`requirements.txt`、各 ignore 文件 | E |
| `README.md`、`AGENTS.md`、`docs/*`（契约除外） | A |
| `docs/` | A |

🔴 **不要用格式化工具重排整个文件** —— 会产生几百行无关 diff，冲突时几乎无法手工合并。

---

## 五、提交规范

```bash
git switch -c <feature|fix|docs>/xxx     # 建分支（小写、无空格、无中文）
git add <相关文件>                        # 只加该加的文件
git commit -m "<type>: <说清做了什么>"
git push -u origin <当前分支>
```

type 取值：`feat` 新功能 · `fix` 修 bug · `docs` 文档 · `refactor` 重构 ·
`test` 测试 · `chore` 构建杂务

```
✅ "feat(m2): 错题归类加知识点ID越界校验与带原因重试"
✅ "fix(sql): 掌握度查询改用 NULLIF 防止除零返回 NULL"
❌ "update" / "111" / "改了点东西"
```

每周每人至少 **3 个真实功能提交**（feat/fix/refactor 才算，docs 和 chore 不计）。
仓库会被直接 clone 下来运行，提交历史就是开发过程本身 —— 攒到最后一次性上传，
等于交了一个看不出怎么做出来的东西。

---

## 六、绝对不提交的东西

```
.env                    ← 真实密钥
data/*.db               ← 跑 seed_data.py 会重新生成，提交上去只会天天冲突
assets/uploads/         ← 用户上传的图片
__pycache__/ .venv/     ← 缓存与环境
.idea/ .vscode/         ← IDE 配置
原始数据集 / 模型权重      ← 体积大且官方明确禁止
```

提交前扫一遍**全部历史**确认没有密钥泄漏：

```powershell
git check-ignore -v .env                                        # 有输出 = 已被忽略
git log -p | Select-String -Pattern "sk-[A-Za-z0-9]{20,}"       # 扫历史而非只看当前文件
```

⚠️ 第二条为什么要扫历史：**曾经提交过又删掉的，仍留在 git 记录里**，
别人 clone 后翻历史就能看到。真发生了泄漏要立刻去供应商后台吊销那把 Key，光删文件没用。

---

## 七、AI 助手的使用约定

**默认只要方案，不动代码**。确定要改了才说"按这个执行"：

```text
第一步：只分析，给方案，不改代码
第二步：我选定方案后你再执行
第三步：执行完先跑一遍验证，把结果给我看
第四步：确认没问题我才让你 commit
```

价值不在于选对方案，而在于**在它动手前发现它理解错了**。

**跑通之后必补一段讲解**：

```text
逐段用大白话解释刚才写的代码。标出：
① 哪里调用了外部服务或读写数据库
② 出错会怎样、有没有兜底
③ 哪些数字是规则阈值
然后出 3 道题考我。
```

理由很硬：AI 写完这一遍，下次改它的还是你。看不懂自己交出去的东西，
出了 bug 只能让 AI 重新猜一遍 —— 那比少做一条功能糟得多。

碎改动留在本地 commit 或 `git stash`，**攒够了、跑通了才 push**。
一句话：**commit 可以频繁，push 要有分寸。**

---

## 八、卡住了怎么办

| 情况 | 处理 |
|---|---|
| 遇到 merge 冲突 | 让 AI **列出双方内容**，人来决定取舍。**不要让它自动选一边** |
| 报错看不懂 | 整段红字原样粘给 AI，问「这是什么意思，该怎么办」—— 是问方案，不是让它直接改 |
| 截图搜不到答案 | 粘贴文本更准，而且 AI 能直接帮你排查 |
| 命令行连不上 GitHub | 浏览器能打开网页 ≠ 命令行能 push。检查代理软件是否常驻运行 |
| 需要撤销已推送的改动 | 用 `git revert <commit>`，绝不用 force push |
| 卡住超过 90 分钟 | 必须求助，不许闷头耗 |
| 想加登录/用户系统/RAG | 先问组长。这些通常是坑，见 `docs/limitations.md` |
