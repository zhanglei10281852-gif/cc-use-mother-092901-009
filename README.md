# 国际标准意见协同

面向国际标准化工作组的意见协同后端：管理提案、条款版本、代表授权、意见、处置决定与表决，
保证文本来源可追溯、计票按当时有效授权进行、发布包封存后可完整重现。

## 领域关系

```
Proposal 1─n Clause 1─n ClauseVersion   每个版本携带 Provenance（来源：初始/处置/修订）
Delegation 1─n Credential               代表授权，按逻辑时刻生效/撤回
Clause 1─n Comment 1─n Disposition      意见可依赖/合并/拆分/替代；处置：接受/拒绝/重新讨论
Clause 1─n VoteRound 1─n Ballot         每轮绑定一个条款版本，开启时快照有效授权
Proposal 1─n Release                    封存后不可变，可撤销，撤销后可再封存
```

## 核心规则

- **来源可追溯**：每次文本变更产生新的 `ClauseVersion`，记录 base 版本、来源意见与处置。
- **按当时授权计票**：轮次开启时快照各代表团有效代表；授权撤回只影响之后开启的轮次。
- **选票幂等**：`(轮次, 代表团)` 唯一；相同内容重复提交返回首次记录（HTTP 200），不同内容 409。
- **平票处理**：赞成 > 反对 → 通过；反对 > 赞成 → 否决；相等 → 平票，条款进入重新讨论；弃权不计入多数。
- **意见依赖**：接受意见前，其依赖（沿替代/合并/拆分链解析到终态意见）必须全部已接受。
- **迟到修订**：轮次关闭后的修订只产生新版本，不回写已确认的计票结果；表决中的条款禁止改文本。
- **发布包**：封存时快照提案全部状态（含异议与理由）并计算 SHA-256；撤销不改写已封存内容，可再封存新版本并链接前一发布包。

## 运行

```bash
pip install fastapi uvicorn pytest httpx   # 依赖
python run_api.py                          # 启动 API（127.0.0.1:8000，文档见 /docs）
python -m unittest discover -s tests -v    # 测试（pytest 亦可）
python -m compileall -q src tests run_cli.py run_api.py
python run_cli.py                          # 契约冒烟
```

## 主要 API

| 方法与路径 | 说明 |
| --- | --- |
| `POST /proposals`、`POST /proposals/{id}/clauses` | 建提案与条款（初始版本） |
| `POST /delegations/{code}/credentials`、`POST /credentials/{id}/withdraw` | 授权与撤回 |
| `POST /proposals/{id}/comments` | 提意见（`depends_on` 声明依赖，支持多语言） |
| `POST /comments/{id}/supersede` / `POST /comments/merge` / `POST /comments/{id}/split` | 替代 / 合并 / 拆分 |
| `POST /comments/{id}/accept` / `reject` / `reopen` | 处置：接受（产生新版本）/ 拒绝 / 重新讨论 |
| `POST /clauses/{id}/rounds`、`POST /rounds/{id}/ballots`、`POST /rounds/{id}/close` | 开轮、投票（反对票须附异议）、关闭计票 |
| `GET /clauses/{id}/rounds/{n}/comment-matrix` | 指定轮次的意见矩阵（意见×处置 × 代表团×选票 × 计票） |
| `GET /rounds/{id}/tally` | 计票结果 |
| `GET /clauses/{id}/diff?from_version=&to_version=` | 文本差异（含两版来源链） |
| `POST /proposals/{id}/releases`、`POST /releases/{id}/revoke` | 封存 / 撤销发布包 |
| `GET /releases/{id}`、`GET /releases/{id}/objections` | 完整重现发布包 / 异议与理由 |

错误映射：`404` 不存在，`409` 状态冲突（含重复选票、依赖未满足），`403` 无表决权，`422` 参数校验。
