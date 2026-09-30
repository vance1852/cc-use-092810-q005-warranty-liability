# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/warranty_claims/`：翻新电池组件的版本化保修条款、装配/维修配置版本、索赔受理冻结、有版本的责任决定、证据保全与剩余保修延续；
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

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量和翻新电池保修索赔流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m warranty_claims.api --database warranty-claims.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 保修责任与索赔管理

`warranty_claims` 处理"整包由不同来源组件构成、且经历过第三方维修"时的责任界定：

- **版本化条款**：每个组件类别挂带版本的保修条款（责任方、覆盖范围、起止条件、排除条款、责任上限）；每个装配/维修配置版本在槽位上逐字记录当时挂载的条款版本，维修是否改变责任期限以记录为准。
- **受理即冻结**：开索赔时在同一事务内冻结故障证据指纹、故障时点的最新配置、当时所有权人，以及全部槽位的条款副本与故障时点有效性判定（active / expired / not_started / indeterminate）。
- **有版本的决定**：调查、补证、责任分摊、和解、驳回、复开、证据放行、维持结案均只追加版本；责任分摊可被新版本取代，旧版本保留。
- **三道硬约束**：故障指纹唯一受理且 `fault_payouts` 唯一赔付；已通知客户的结论不可改写，迟到检测只能复开并产生新版本（维持结案不再付款）；证据保全按责任方持有，索赔整体终局前不允许释放任何一方，部分责任方确认份额也不释放。
- **客服解释视图**：`GET /claims/{id}/explanation` 说明索赔由哪些主体承担及其条款依据、哪些争议尚未确认、保全仍向哪些责任方持有，以及更换组件后的剩余保修延续登记。
