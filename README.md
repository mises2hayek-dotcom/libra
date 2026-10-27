# Python 项目版本控制实操指南

`git 2.55` / `python 3.13` 验证可用。本仓库本身就是一个「版本控制就绪」的样板：目录布局、忽略规则、自动检查钩子都已配置好，可直接复制改用到真实项目。

---

## 0. 一次性初始化（新项目）

```bash
mkdir myproj && cd myproj
git init -b main                 # 指定主分支名，避免 master/main 分歧
python -m venv .venv
source .venv/Scripts/activate    # Windows bash；Linux/macOS 用 .venv/bin/activate
pip install -U pip
```

顺序很重要：**先建 `.gitignore`，再装依赖，再 `git add`**。否则 `.venv/` 里几万个文件会先进暂存区，清理很麻烦。

```bash
git add .gitignore .gitattributes pyproject.toml README.md
git commit -m "chore: 初始化项目骨架"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

`-u` 只需一次，之后 `git push` / `git pull` 即可。

---

## 1. 三个文件决定仓库整洁度

| 文件                     | 作用           | 不写的后果                        |
| ---------------------- | ------------ | ---------------------------- |
| `.gitignore`           | 声明永不追踪的路径    | venv、缓存、密钥被提交，仓库膨胀且泄密        |
| `.gitattributes`       | 统一换行符与二进制判定  | Windows/macOS 协作时全文件 diff 变红 |
| `pyproject.toml` + 锁文件 | 声明依赖并提供可复现环境 | 别人 clone 后跑不起来               |

`.gitignore` 三条铁律：

1. **依赖目录不入库**：`.venv/`、`venv/`、`node_modules/` 一律忽略——重建只需一条命令。
2. **缓存与产物不入库**：`__pycache__/`、`.pytest_cache/`、`build/`、`dist/`、`*.egg-info/`。
3. **密钥不入库**：`.env`、`*.pem`、`*.key`。只提交 `.env.example` 模板。

依赖声明入库、依赖实现不入库，这是最基本的分界线。

---

## 2. 日常循环：改 → 挑 → 提 → 推

```bash
git status                  # 先看有什么变了
git diff                    # 看具体改动（未暂存）
git add -p src/demo/calc.py # 逐块挑选，把无关改动挡在外面
git commit -m "feat(calc): 新增 safe_divide 并处理除零"
git push
```

关键习惯：**用 `git add -p` 而不是 `git add .`**。一次提交只包含一个逻辑变更，回滚、review、定位 bug 才有意义。

原子提交的判断标准：提交信息能用一句话说清"做了什么"，且这个提交单独 revert 不会破坏其他功能。

---

## 3. 提交信息规范（Conventional Commits）

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

| type       | 用于       |
| ---------- | -------- |
| `feat`     | 新功能      |
| `fix`      | 修 bug    |
| `refactor` | 重构，行为不变  |
| `perf`     | 性能优化     |
| `test`     | 测试相关     |
| `docs`     | 文档       |
| `chore`    | 构建、依赖、配置 |
| `ci`       | 流水线      |

反例：`update`、`fix bug`、`修改`、`1`。正例：`fix(api): 修复空列表导致的 IndexError`。

写"为什么"，不写"改了什么"——代码本身已经说明改了什么。

---

## 4. 分支与协作

```bash
git switch -c feat/safe-divide     # 开分支（新版命令，替代 checkout -b）
# ... 若干次 commit ...
git push -u origin feat/safe-divide
# 在平台上开 Pull Request → review → 合并
git switch main && git pull        # 合并后同步
git branch -d feat/safe-divide     # 删本地分支
```

| 策略          | 适合           | 要点                         |
| ----------- | ------------ | -------------------------- |
| Trunk-based | 小团队、持续交付     | 分支存活 < 2 天，频繁合并，靠开关控制未完成功能 |
| GitHub Flow | 大多数项目        | 一切从 `main` 拉出，PR 合并即发布     |
| Git Flow    | 版本化交付、需维护多版本 | 分支多、流程重，新项目不建议             |

分支命名：`feat/`、`fix/`、`chore/`、`hotfix/` 前缀 + 短横线描述。

---

## 5. 自动化守卫：pre-commit + CI

本地钩子挡住低级错误，CI 挡住"绕过钩子"的情况。

```bash
pip install pre-commit
pre-commit install          # 写入 .git/hooks/pre-commit
pre-commit run --all-files  # 手动全量跑一次
```

本仓库的 `.pre-commit-config.yaml` 已含：

- `ruff` + `ruff-format` —— lint 与格式化（替代 flake8 + isort + black 三件套，速度快一个量级）
- `nbstripout` —— 提交前清空 notebook 输出，diff 才可读
- `check-added-large-files` —— 拦截 >10MB 文件
- `detect-private-key` —— 拦截私钥误提交

CI 最小闭环：`ruff check` → `mypy src` → `pytest`。三者全绿才允许合并。

---

## 6. 撤销与救火手册

| 想撤什么              | 命令                               | 危险度                 |
| ----------------- | -------------------------------- | ------------------- |
| 工作区未暂存的改动         | `git restore <file>`             | 低（改动丢失）             |
| 已 `add` 但未 commit | `git restore --staged <file>`    | 无（改动保留）             |
| 上一次的提交信息          | `git commit --amend -m "新信息"`    | 低（勿用于已推送）           |
| 上一次提交的内容          | `git reset --soft HEAD~1` 后重新提交  | 低（仅本地）              |
| 已推送的错误提交          | `git revert <sha>`               | 无（生成反向提交，安全）        |
| 找"丢失"的提交          | `git reflog`                     | 无（救命命令，别慌）          |
| 临时切分支             | `git stash -u` → `git stash pop` | 无                   |
| 彻底丢弃本地改动          | `git reset --hard origin/main`   | **高，先 `git stash`** |

两条纪律：

1. **已推送的公共分支不用 `reset --hard` / `--force`**，只用 `revert`。
2. 慌乱时先 `git reflog` 和 `git stash`，几乎不会真的丢东西。

---

## 7. 三类特殊文件

**Jupyter notebook**：输出会让 diff 无法阅读。用 `nbstripout`（见钩子配置）自动清空；重要逻辑尽量迁到 `.py`，notebook 只做展示。

**大数据 / 模型权重**：Git 不适合存二进制大文件。

```bash
# 方案 A：Git LFS，文件仍随仓库走
git lfs install && git lfs track "*.pt"

# 方案 B：DVC，数据与代码分离，远端存 S3/OSS
dvc init && dvc add data/raw.csv
```

**密钥**：一旦提交即视为泄露，改密码 + 轮换，历史可以用 `git filter-repo` 清，但成本高。**预防成本远低于补救成本。**

---

## 8. 团队约定清单（可直接抄进 CONTRIBUTING.md）

- [ ] 主分支保护：禁止直接 push，必须 PR + 至少 1 人 review
- [ ] PR 粒度：一次解决一个问题，diff 尽量 < 400 行
- [ ] 提交粒度：可独立 revert
- [ ] 提交信息遵循 Conventional Commits
- [ ] 合并前 CI 全绿（lint / type / test）
- [ ] 依赖变更必须同时更新锁文件
- [ ] 禁止提交 `.env`、密钥、本地绝对路径
- [ ] 发布打 tag：`git tag -a v0.1.0 -m "release 0.1.0" && git push --tags`

---

## 9. 动手验证

```bash
cd python-vcs-starter
git log --oneline --graph --all
python -m pytest -q
```

---

## 附：最小命令速查

```bash
git init -b main            # 初始化
git status -sb              # 简洁状态
git add -p                  # 逐块暂存
git commit -m "..."         # 提交
git switch -c feat/x        # 建并切分支
git switch main             # 切回
git merge feat/x            # 合并
git pull --rebase           # 拉取并线性整理历史
git log --oneline --graph   # 看历史
git blame <file>            # 看每行谁改的
git bisect start            # 二分定位引入 bug 的提交
```
