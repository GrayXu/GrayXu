# 历史用量保留与恢复

CCUsage 上报可能只包含仍保存在本机的部分会话。缺失日期补零并不表示过去的真实用量为零。

上报和公开统计均以配置时区（默认 `Asia/Shanghai`）的昨天为截止日期。`last 1d` 指昨天，7 日和 30 日汇总也截至昨天，不包含今天的部分用量。发送窗口的天数仍表示完整自然日的数量。

发布定时器在上海时间 00:35 执行，README 工作流在 00:45 执行，给每半小时上报的发送端留出跨日后的补报时间；离线机器仍需恢复上报后才能补齐。

服务端对同一日期、来源和机器的字段逐项取 `max(old, incoming)`。不同机器、不同来源仍然求和，重复上报不会累加。

CPA 来源仍按数据库当前内容刷新；每日发布总量保留 `max(previous_total, current_source_sum)`。因此清理源日志或收到不完整上报不会降低已发布的总量。

历史公开文件只有精确总量，没有来源拆分。恢复只抬高每日总量，不修改来源明细；恢复后总量可能大于当前 CCUsage 与 CPA 明细之和，这是历史明细缺失的结果。

## 从数据分支恢复

先同步包含完整历史的仓库，在写数据库前做好 SQLite 一致性备份。工具默认只读，报告包含日期、恢复前后数值及最大值对应的 Git 提交。

```bash
git fetch origin token-data
PYTHONPATH=src python -m token_heatmap.recover \
  --repo . \
  --database /path/to/token_usage.sqlite \
  --since-commit <first-valid-accounting-commit> \
  --report /path/to/recovery-preview.json
```

`--since-commit` 仅扫描该提交及其后续历史。需要保留已有去重或统计口径修正时，应从修正完成后的数据提交开始；不指定则扫描数据分支全部历史。仓库若为浅克隆，先补全历史。

核对报告后，使用同样参数加 `--apply` 写入。写入在一个事务内完成，重复执行不会重复增加总量。此命令只修复本地统计库，随后执行现有同步服务发布数据并触发 README 工作流。

```bash
PYTHONPATH=src python -m token_heatmap.recover \
  --repo . \
  --database /path/to/token_usage.sqlite \
  --since-commit <first-valid-accounting-commit> \
  --apply \
  --report /path/to/recovery-applied.json
PYTHONPATH=src python -m unittest discover -s tests -v
```
