# 每日信号复盘

日常查看使用本地实时入口。将 `--data-dir` 换成正在运行的 signal_service 使用的行情目录：

```powershell
python -B -m mu_strategy.commands.render_signal_review --data-dir data\live --serve
```

打开输出的地址（默认 `http://127.0.0.1:8769/`）。页面每 30 秒读取新记录，支持立即更新、暂停，以及日期、标的和状态筛选；刷新会保留筛选与展开详情。查看器只监听本机，`Ctrl+C` 关闭，`--port` 可更换端口。

- 相邻、同标的、同日期和同状态的正常等待合并显示，展开可看每次扫描。状态变化或间隔超过 10 分钟会分组；待复核、阻断和失败单列。
- 编辑筛选后的 2 秒内或选中文本时暂缓更新，并显示提示；只保留控件焦点不会持续暂停。
- 连接失败会显示“连接中断 · 显示上次快照”，恢复后继续读取。
- 默认最近 7 个北京时间自然日；可用 `--days` 或 `--from-date` / `--to-date` 设置窗口，最多 366 天。筛选只影响明细，上方统计保持不变。

保存某个时点的静态页面：

```powershell
python -B -m mu_strategy.commands.render_signal_review --data-dir data\live --output reports\live\signal-review.html
```

导出文件不会自动更新。输出必须是数据目录之外的 `.html`；写入失败保留旧文件。

本页只展示服务、扫描和通知中现有的记录，复用原有格式检查和只读访问，不额外审计送达生命周期或跨事件历史完整性。`sources_readable` / 退出码 0 表示来源读取完成，不代表历史完整或服务健康；读取失败、读取上限或输出失败返回 2，并在页面说明。

扫描最多读取 100,000 轮，各类明细最多显示最后 2,000 条，截断时显示提示。邮件状态按本次查询展示，SMTP 接受不等于收件箱回执。来源与查询命令默认折叠。

此入口属于 [#99](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/99) 的日常查看范围；实验冻结、人工反馈和前瞻评估另行推进。
