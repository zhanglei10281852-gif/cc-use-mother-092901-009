# 国际标准意见协同

面向智能网联汽车国际合作工作组的标准意见协同后端：为提案、条款版本、代表资格、
意见、处置决定和表决建立清晰关系，保证每条文本变更可溯源、每次表决可重现。

## 架构

```
src/standards_collaboration/
  contracts.py   # 基础契约（条款版本、代表团选票）
  domain.py      # 领域模型：实体、枚举、事件、异常
  service.py     # 核心服务：处置、计票、发布包（方法即 API）
  api.py         # JSON REST 薄层（标准库 http.server）
tests/
  test_contracts.py  # 契约测试
  test_service.py    # 服务层场景测试
  test_api.py        # HTTP 端到端冒烟
```

## 核心规则

- **意见处置**：接受 / 拒绝 / 合并 / 拆分 / 替代 / 重新讨论；被处置意见进入
  终态，被拒绝或被替代的意见可重开。合并、拆分、替代产生的新意见记录
  `derived_from` 依赖链。
- **文本溯源**：接受意见产生条款新版本，版本记录父版本、来源处置与来源意见，
  最终文本吸收了哪些建议随时可查。
- **表决**：轮次开启时锁定条款版本，迟到修订不影响本轮；计票按关闭时点有效的
  代表授权，每代表团一票，重复投票幂等、改票取最新；平票记为 `tied`（未通过），
  可开启新一轮表决。授权撤回只影响后续轮次，已关闭轮次的结果永久冻结。
- **发布包**：封存时对条款版本、计票、意见矩阵、异议（被拒绝意见与反对票，
  均含理由）做不可变快照并计算 SHA-256 摘要；封存期间条款锁定，撤销后方可
  产生新版本；已撤销的发布包仍可完整重现并校验摘要。

## 报告 API

- `GET /rounds/{id}/comment-matrix` — 指定轮次的意见矩阵（按关闭时点重建）
- `GET /rounds/{id}/tally` — 指定轮次的计票结果（已关闭轮次返回冻结快照）
- `GET /clauses/{id}/diff?from=1&to=2` — 两个版本的文本差异与来源链
- `GET /releases/{id}/reproduction` — 发布包完整重现（含异议、理由、摘要校验）

其余端点见 `api.py` 的 `ApiHandler.ROUTES`；服务层方法签名见 `service.py`。

## 运行

```bash
python -m unittest discover -s tests -v   # 测试
python -m compileall -q src tests run_cli.py  # 编译检查
python run_cli.py                          # 命令行冒烟
python -m standards_collaboration.api      # 启动 HTTP API（需 PYTHONPATH=src）
```
