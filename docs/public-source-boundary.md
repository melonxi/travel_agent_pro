# 公开源码边界（Public Source Boundary）

本仓库以**本地 `main` 为权威代码源**，通过 GitHub 公开协作与展示。  
目标：**代码与工程文档可公开；私人材料与密钥永不进公开历史。**

最后对齐：`2026-07-17`（`origin/main` 已按本地历史 force 对齐，并剥离私人路径）。

---

## 1. 原则

| 原则 | 说明 |
|------|------|
| 单一主线 | 日常只在本地 `main` 开发；`git push origin main` 应为快进，**不要**再造第二套无关历史 |
| 密钥永不入库 | API key、真实 `config.yaml`、`.env` 只放本机，用 example 文件给模板 |
| 私人材料可留盘 | 学习笔记、面试材料等可保留在工作区，靠 `.gitignore` 排除，**不 `git add`** |
| 公开历史干净 | 一旦私人路径进过 commit，仅靠 ignore 不够，需要从历史剥离后再推（见 §5） |

错误做法：为了「少推一些文件」在远端另写一套历史 → 会再次出现无共同祖先的分叉。

---

## 2. 分类

### A. 必须公开（跟代码一起演进）

- `backend/`、`frontend/`、`scripts/`（可运行、可测试的工程代码）
- `docs/agent/`（agent 鸟瞰 / slices / deep）
- 工程向计划与复盘（如 `docs/reviews/*` 中的 D3/D4 修复计划）
- `config.example.yaml`、`README.md`、`AGENTS.md`、`PROJECT_OVERVIEW.md` 等协作入口
- 测试与 eval 用例（无真实用户隐私、无密钥）

### B. 本地可留、公开仓不跟踪（已在 `.gitignore`）

| 路径 | 用途 |
|------|------|
| `docs/learning/` | 个人学习笔记 |
| `docs/agent-interview/` | 面试专题与 walkthrough |
| `docs/superpowers/` | 个人 superpowers 计划/规格草稿 |
| `docs/mind/` | 个人思路备忘 |
| `docs/resume-bullets.md`、`docs/resume-bullets_*.md` | 简历 bullet 草稿 |
| `docs/agent-interview-question-bank.md` | 面试题库 |
| `docs/reviews/Agent 应用开发工程师模拟面试复盘 Round 1.md` | 模拟面试复盘 |

新增同类内容时：先加进 `.gitignore`，确认 `git status` 不可见，再开始写文件。

### C. 密钥与本机配置（已在 `.gitignore`）

- `.env`、`.env.local`
- `config.yaml`、`*.local.yaml`
- `data/`、运行时截图、测试结果目录等

模板用 `config.example.yaml`；文档里只写占位符，不写真实 key。

---

## 3. 日常工作流

```text
编辑代码 / 公开文档
  -> git status 确认无私人路径、无密钥
  -> commit
  -> git push origin main
```

私人笔记：

```text
写在 docs/learning/ 等 ignore 目录
  -> 不出现在 git status
  -> 不 commit、不 push
```

提交前自检：

```bash
git status -sb
git check-ignore -v docs/learning docs/agent-interview  # 应命中 .gitignore
git diff --cached | rg -i 'api[_-]?key|sk-|password|token=' && echo '可疑密钥，停止提交'
```

---

## 4. 远程约定

| Remote | 用途 |
|--------|------|
| `origin` → 公开 GitHub | 仅含可公开树；与本地 `main` 同步 |
| （可选）`private` 私有仓 | 若需要备份完整含私人历史的旧镜像，用私有仓或本机 bundle，**不要**推到公开 `origin` 的旁路分支 |

当前公开仓库：`https://github.com/melonxi/travel_agent_pro`（默认分支 `main`）。

---

## 5. 若误把私人文件提交了

1. **尚未 push**：`git rm -r --cached <路径>`，补 `.gitignore`，amend 或新 commit 去掉跟踪。  
2. **已经 push 到公开仓**：  
   - 从全历史剥离（如 `git filter-repo --invert-paths --path <路径>/`）  
   - `git push --force-with-lease origin main`  
   - 若路径里含真实密钥：在对应平台**轮换密钥**，不要假设删历史就够。  
3. 过滤前先做本机备份（`git bundle` / 目录拷贝），不要依赖 `/tmp` 长期保存。

---

## 6. 与 agent 文档的关系

- 改架构 / 数据流 / 工具等：仍按 `docs/agent/` 约定更新 slices/deep。  
- 改「什么可以公开」：更新本文件 + `.gitignore`，并在 commit message 中说明。  
- 本文件本身**应当公开**（协作约定的一部分）。

---

## 7. 变更记录

| 日期 | 说明 |
|------|------|
| 2026-07-17 | 确立边界：本地 main 为权威；私人 docs 从跟踪与历史剥离；force-with-lease 对齐 `origin/main` |
