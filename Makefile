PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help install lint format format-check typecheck arch photobench-check test test-unit test-contract test-integration coverage check-docs web-check ci dev photobench-dev web-dev install-services start stop restart status

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n", $$1, $$2}'

# 前端依赖必须在这里装。只装 Python 依赖时，全新克隆跑 `make ci` 的 web-check 会在
# `npx tsc -b` 处去 npm 下载冒名包 tsc@2.0.4，打印 "This is not the tsc command you are
# looking for" —— 它确实 exit 1（不是静默通过），但报错指向的是 npx 而不是缺依赖，
# 后来人极易当成 npx 抖动而把前端检查删掉，于是又造出一个"看起来在检查、其实没有"。
# 用 `npm ci` 而不是 `npm install`：apps/web/package-lock.json 在版本控制里，
# `npm ci` 按 lock 装、lock 与 package.json 不一致时直接报错，比 install 可复现。
install: ## 建 venv 并安装全部依赖
	uv python install 3.11
	uv venv --python 3.11 --python-preference only-managed --clear --seed .venv
	$(PIP) install -q -e "apps/api[dev]"
	# PhotoBench 是**第二个发行包**（apps/photobench）。不装它，`photobench.*` 就
	# 解析不到——mypy（files 里含 apps/photobench/src）、pytest（testpaths 含它的
	# tests）与三个 arch 脚本会一起报「模块不存在」，而那种红指向的是环境而不是代码。
	$(PIP) install -q -e apps/photobench
	cd apps/web && npm ci
	# PhotoBench 前端是**第三个 npm 包**（apps/web 是第二个）。同一条理由：不装它，
	# 下面那条 photobench-web-check 会在「找不到 tsc」上红，而那种红指向的是环境不是代码。
	cd apps/photobench/web && npm ci

lint: ## ruff 检查
	.venv/bin/ruff check apps/api/src apps/api/tests apps/photobench/src apps/photobench/tests scripts

format: ## ruff 自动修复
	.venv/bin/ruff check --fix apps/api/src apps/api/tests apps/photobench/src apps/photobench/tests scripts
	.venv/bin/ruff format apps/api/src apps/api/tests apps/photobench/src apps/photobench/tests scripts

format-check: ## ruff 格式检查（只读）
	.venv/bin/ruff format --check apps/api/src apps/api/tests apps/photobench/src apps/photobench/tests scripts

typecheck: ## mypy strict（全包 + scripts）
	.venv/bin/mypy

# 三条互补的检查：lint-imports 管「谁不许 import 谁」，check_reachability 管
# 「每个模块在生产装配路径上有没有调用者」，check_method_reachability 管
# 「每个端口方法有没有调用者」——**粒度不同，不能合并**：模块级看不见
# 「模块接上了、里面某个方法没人调」，而那正是 facts 读路径烂掉的方式。S4 两次交付「代码在、没人调」的能力
# （零调用的纯规则、零调用的 SummarizeEvent），两次都是事后被用户点出来的——
# 靠人记记不住，所以把它变成可机械判定的东西。
arch: ## 分层契约检查 + 生产可达性（已在 ci 链上）
	.venv/bin/lint-imports
	$(PY) scripts/check_reachability.py
	$(PY) scripts/check_method_reachability.py

# PhotoBench 的三个 arch 检查要单独跑一遍并显式传 `--src`：那三个脚本的默认根是
# `apps/api/src`，而 PhotoBench 是**另一个包、另一个 src 根**（`apps/photobench/src`）。
# 不显式传就等于它完全不在门禁里——用户 2026-10-04 裁决 §3.5 要的正是「不给它开飞地」。
# 模块映射与文档校验同理：`--doc` 指到 photobench 自己的模块地图。
#
# 这里不重复跑 pytest / ruff：测试已在 `coverage` 那一步的默认收集里（testpaths 含
# `apps/photobench/tests`），ruff 已在 lint / format-check 里。
photobench-check: ## PhotoBench 门禁：可达性 + 端口可达性 + 模块地图（已在 ci 链上）
	$(PY) scripts/check_reachability.py --src apps/photobench/src
	$(PY) scripts/check_method_reachability.py --src apps/photobench/src
	$(PY) scripts/check_module_map.py --src apps/photobench/src --doc docs/architecture/photobench-module-map.md

test-unit: ## 单元测试
	.venv/bin/pytest apps/api/tests/unit -q

test-contract: ## 契约测试
	.venv/bin/pytest apps/api/tests/contract -q

test-integration: ## 集成测试
	.venv/bin/pytest apps/api/tests/integration -q

test: ## 全部测试
	.venv/bin/pytest -q

# S1 Task 13 已合入：domain/application 有代码，本目标实测 95.6%（阈值 85%），门禁为绿。
coverage: ## domain + application 覆盖率门禁（设计 §6.4：≥85%）
	.venv/bin/pytest --cov --cov-report=term-missing

check-docs: ## 文档防腐校验
	$(PY) scripts/check_capability_map.py
	$(PY) scripts/check_module_map.py
	$(PY) scripts/export_openapi.py --check
	# PhotoBench 是第二个发行包，有自己的一份 openapi 契约（S9c · T925 的一半）。
	# 只跑 sentrix 那份等于说「第二个包的服务面没有契约」——前端（S10）从哪一份生成
	# 客户端，取决于这两条都有人守。
	$(PY) scripts/export_photobench_openapi.py --check

# 注意：所有目标必须从仓库根执行。从 apps/api/ 下跑 pytest 时 coverage 配置
# 不会被加载，fail_under=85 会静默失效（该目录下有独立的 pyproject.toml）。
# coverage 本身跑的就是全量测试，所以链上不再单列 test——同一套测试不必跑两遍。
# web-check 排在链尾：后端检查更快、失败信息更直接，前端构建最慢也最该放在最后。
ci: lint format-check typecheck arch photobench-check check-docs coverage web-check photobench-web-check ## CI 门禁
	@echo "ci: 全部通过"

# 前端门禁。类型检查必须用 `tsc -b`，不能用 `tsc --noEmit`：apps/web/tsconfig.json 是
# {"files": [], "references": [...]} 结构，普通 tsc 不遍历 project references，
# `npx tsc --noEmit --listFiles` 实测命中 0 个文件、恒 exit 0——它看起来在检查，其实永远通过。
# 四件事分开跑而不是 `npm run build`：类型 / lint / 测试 / 构建的失败信息各自直接，
# 且 `npx vite build` 省掉 build 脚本里那次重复的 tsc -b。
# vitest 排在 vite build **之前**：构建最慢，而测试慢几十毫秒就能报出「逻辑错了」，
# 让它先说话，人不用等完一次打包才知道。
# 第一条存在性断言是给"跳过 install / node_modules 被删"兜底的：此时 npx 会去下冒名包
# tsc@2.0.4 并打印 "This is not the tsc command you are looking for"，那句话不指向缺依赖。
# 断言先拦下来，只说该做什么。
web-check: ## 前端门禁：tsc -b + oxlint + vitest + vite build
	@test -d apps/web/node_modules || { echo "缺少 apps/web/node_modules，请先跑 make install"; exit 1; }
	# **`--force` 不是保险丝，是必须的。** `tsc -b` 的增量检查只在「它已经知道的文件
	# 被改动」时触发：**新加一个匹配 include 的源文件不会让它重建**，于是那个文件根本
	# 不被类型检查。实测复现（2026-10-03）：干净构建 → 新增一个用 `node:fs` 的 .ts →
	# 不带 --force 跑 `tsc -b` 退出码 0，带 --force 才报 TS2591。
	# F7 那个 `source-hygiene.test.ts` 就是这么带着三个类型错误进仓库的。
	cd apps/web && npx tsc -b --force
	cd apps/web && npx oxlint
	cd apps/web && npx vitest run
	cd apps/web && npx vite build

photobench-web-check: ## PhotoBench 前端门禁：同 web-check 四步
	@test -d apps/photobench/web/node_modules || { echo "缺少 apps/photobench/web/node_modules，请先跑 make install"; exit 1; }
	# 与 web-check 逐条同源，包括那条 `--force`：`tsc -b` 的增量检查只在「它已经知道的
	# 文件被改动」时触发，**新加一个匹配 include 的源文件不会让它重建**，于是那个文件
	# 根本不被类型检查。F7 就是这么带着三个类型错误进过仓库的。
	cd apps/photobench/web && npx tsc -b --force
	cd apps/photobench/web && npx oxlint
	cd apps/photobench/web && npx vitest run
	cd apps/photobench/web && npx vite build

dev: ## 起 2.0 API（9598）
	$(PY) -m uvicorn --factory sentrix.main:create_app --host 0.0.0.0 --port 9598 --app-dir apps/api/src

# PhotoBench 的入口也是工厂（`photobench.main` 没有模块级 app：那会让任何 import 带上
# 建目录与起后台扫描线程的副作用）。端口 9597 出自设计文档 §5.1 的端口表；1.0 跑 8771。
photobench-dev: ## 起 2.0 PhotoBench（9597）
	$(PY) -m uvicorn --factory photobench.main:create_app --host 0.0.0.0 --port 9597 --app-dir apps/photobench/src

web-dev: ## 起 2.0 Web（9501）
	cd apps/web && npm run dev -- --port 9501

photobench-web-dev: ## 起 PhotoBench 前端（9502）
	cd apps/photobench/web && npm run dev

# S11 · T1102：两个 `systemd --user` 单元。单元文件在仓库 `deploy/systemd/`（与它启
# 动的代码同源），`install-services` 只做「拷贝 + daemon-reload」——**仓库里那份是
# 权威**，改完重跑一次即可。不用 `--user` 之外的 sudo：`--user` 单元归当前用户管。
# 停/起是**每个服务独立**的（两个单元互不依赖），所以下面几个目标对两个单元一起发
# 命令，单服务操作直接 `systemctl --user {start,stop,restart} sentrix-api`。
SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user
SERVICE_UNITS := sentrix-api.service sentrix-photobench.service

install-services: ## 装 systemd --user 单元到 ~/.config/systemd/user/（S11）
	install -d $(SYSTEMD_USER_DIR)
	install -m 0644 deploy/systemd/sentrix-api.service $(SYSTEMD_USER_DIR)/
	install -m 0644 deploy/systemd/sentrix-photobench.service $(SYSTEMD_USER_DIR)/
	# **模型侧车单独装、单独管。** 设计 §2.1 明写 2.0 不管理模型进程的生命周期，
	# 所以它不进 SERVICE_UNITS——`make start/stop/restart` 不该碰模型。
	# 但它的单元文件仍然与代码同源：2026-10-08 与 10-10 两次侧车自己消失，
	# 而整轮评测 354/370 次检索静默降级，「谁来拉起它」不能只写在 docstring 里。
	install -m 0644 deploy/systemd/sentrix-wemm-sidecar.service $(SYSTEMD_USER_DIR)/
	systemctl --user daemon-reload

start: ## 起 2.0 两个 --user 服务（9598/9597）
	systemctl --user start $(SERVICE_UNITS)

stop: ## 停 2.0 两个 --user 服务
	systemctl --user stop $(SERVICE_UNITS)

restart: ## 重启 2.0 两个 --user 服务
	systemctl --user restart $(SERVICE_UNITS)

status: ## 看 2.0 两个 --user 服务状态
	systemctl --user status $(SERVICE_UNITS) --no-pager
