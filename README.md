# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/warranty_claims/`：组件装配/维修版本的保修条款、索赔受理冻结、证据保全、有版本的调查与责任分摊决定、复开与剩余保修延续；
- `fixtures/`：离线验收使用的评估协议与结构化观测；
- `tests/`：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m battery_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m battery_assurance.acceptance --workspace .
PYTHONPATH=src python3 -m component_quality.acceptance
PYTHONPATH=src python3 -m warranty_claims.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量和翻新整包保修索赔流程，不访问外部网络。

## 保修责任与索赔管理

- 每个组件在装配、维修、更换版本上关联责任方、覆盖范围、起止条件、排除条款，并用 `term_effect`（`continue` 沿用剩余期限 / `reset` 维修件重新起算 / `new_full` 更换件全新期限）表达维修动作对责任期限的影响；
- 索赔受理时不可变地冻结故障证据、故障当时的整包配置、所有权人和逐组件有效条款（含 SHA-256 摘要，数据库触发器禁止改写），并为每位在保责任方建立独立证据保全；
- 调查、补证要求、责任分摊、和解、驳回均为只追加的有版本决定；拟定与通知客户分离，**已通知客户的结论不可改写**；
- 结论通知后到达的迟到检测只能登记并触发复开（或基于未决争议复开），复开追加新修订、原结论保留；同一故障全局只赔付一次，部分责任方确认不会提前释放其他方的证据保全；
- `GET /packs/{pack_id}/warranty` 查询更换组件后的剩余保修延续，`GET /claims/{claim_id}/explanation` 向客服说明承担主体及依据、未决争议、已通知结论与剩余保修。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m warranty_claims.api --database warranty-claims.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
