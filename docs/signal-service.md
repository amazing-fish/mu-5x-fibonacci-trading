# 持续刷新与信号服务

`python -m mu_strategy.commands.signal_service` 将唯一行情刷新命令与 Stage 0 扫描按顺序运行，记录健康状态，供人工查询及后续邮件通知消费。默认只扫描 `MU-USDT-SWAP`，每 300 秒一个周期。它不读取账户凭证、不创建 broker、不发送订单或邮件，也不注册系统任务。

## 首次运行与查询

在已安装项目且测试通过的 Python 环境中，从仓库根目录运行：

```powershell
python -B -m mu_strategy.commands.signal_service run --once --data-dir data\live
python -B -m mu_strategy.commands.signal_service status --data-dir data\live
```

`run --once` 会访问 OKX 公共行情并发布 trusted generation。运行前停止同一数据目录的旧刷新计划、手动刷新和旧扫描 loop，确保只有一个调度者。服务锁只排除本服务的重复实例，不锁住独立刷新命令。`status` 只读既有状态并探测 OS 文件锁，不建目录、不刷新数据、不改健康记录。

`run --once` 的 stdout 是本轮完成时的健康快照；命令结束后 `status` 应报告 `stopped`。退出码：本轮健康为 0，故障/配置/状态不可读为 2，已有实例为 3；`status` 当前运行且最近本轮健康为 0，其余为 2。内部 `scan-once` 的 0 只表示扫描和写盘完成，数据阻断也可返回 0；调度者必须解析完整结果。

重复 `--symbol` 配置显式 watchlist，例如 `--symbol MU-USDT-SWAP --symbol BTC-USDT-SWAP`；不读取动态榜单，别名归一化并去重。`--refresh-days` 默认 180，`--scan-days` 默认 28 且不能超过刷新天数。周期、超时和天数必须为正整数。没有交易启用参数。

180 天是请求的滚动历史窗口；合约历史不足时，只保留实际已有数据，并在 manifest 中保留 `partial_available_history` 和实际覆盖天数，不填造上市前 K 线，也不把请求天数改小。如果 180 天窗口已完整，但首个存储月恰好是月中上市的月份，首次完整历史刷新会额外查询 OKX 公共合约信息的 `listTime`：只有首根 5m K 线与上市所在桶一致、15m/1h 起点与完整 5m 比较窗口一致时，才使用已有的 `.partial-<起点时间>.csv` 首月格式。首次发布的对应数据集记录 `verified_listing_start:listing_time_ms=...`，逻辑覆盖仍如实标记 `complete`。

例如 MU 于 2026-03-04 上市，2026-09-06 请求 180 天时，不再因无法取得 3 月 1 日的上市前数据而失败。没有可信上市信息、返回起点晚于应有首根 K 线、时间矛盾或请求失败时仍拒绝发布；不会把接口截断当作上市边界。普通月初回看完整和增量刷新无需额外查询上市时间；重启后继续复用已验证的首月分段。已有 strict gate、数据新鲜度、连续性和 built/native 校验保持不变。

增量复用与首次校验共用上一 generation 的 5m 时间窗口末端。15m/1h 尚未完整形成的尾部 K 线不会移动这个窗口，避免非整点运行误判覆盖不足并丢失首月分段的复用依据。缺少可用的 5m 基准时重新拉取完整历史，不自行放宽覆盖判定。

## 运行顺序与健康含义

每轮先持久化活动阶段，再启动有界的独立进程：

1. `mu_strategy.commands.refresh_market_data` 是唯一行情 writer，默认超时 240 秒。
2. writer 退出后启动 `signal_service scan-once`，默认超时 60 秒。worker 为整个 watchlist 固定一个 trusted context，使用现有 `trading_strict` reader 和权威 `ScanCycle`；manifest 不可读时仍为每个请求标的记录类型化数据失败。
3. 将完成的刷新、扫描、持久化结果和健康事件一起原子发布到 `health.json`。

失败会在下一个周期重试，不立即重试。周期按 monotonic clock 计算，慢周期跳过错过的时点，不积压或重叠运行。刷新失败仍允许只读检查上一 generation；可能得到允许扫描的缓存，但整体健康仍保留刷新失败。成功刷新与允许扫描的 generation 不一致会报告 `data.publication_changed`，提示检查其他 writer。

| 维度 | 含义 |
|---|---|
| `runtime` | `not_started`、`starting`、`running`、`stopped`、`interrupted`、`unresponsive`；锁、持久化阶段与 deadline 共同判定 |
| `last_cycle.refresh` | 正常发布、降级发布、失败或超时，保留 run ID、attempt/usability 和退出码 |
| `data_at_last_scan` | **上次扫描时**的 trusted gate、原因和检查时间；不是查询时重新证明数据新鲜 |
| `last_cycle.scan` | worker 完成状态及 Stage 0 cycle；逐标的区分数据阻断、扫描失败、正常无信号、READY |
| `last_cycle.scan.persistence` | 观测写盘是否成功；写盘失败保留原决策，不重扫 |
| `healthy` / `problems` | 本次进程已有完成轮次、运行未逾期且四个维度均正常；WAIT/策略过滤/READY 本身不算故障 |
| `consecutive_failures` | 连续完成但不健康的轮数；成功恢复归零，重启相同 watchlist 保留计数 |

deadline 是当前子进程超时或下一轮计划时间，加 30 秒状态更新余量。时钟倒退到最后更新时间之前也标为不响应。硬停机后，查询端看到锁已释放、journal 仍为运行中，就报告 `interrupted`。重启后的首轮完成前不会复用历史结果宣布健康。

## 存储与通知接入

首次运行会同步每个新建目录项的父目录，确认状态目录可持久化后才启动 worker；父目录同步失败会停止本次运行。启动先取得实例锁、发布新 run identity，再取得存活锁。查询以两份相同的 journal 快照夹住存活探测；发现变化则重取，最多探测 3 次，持续变化会返回暂不可用而不报告停机。完成轮次绑定 `service_run_id`，重启不能用新进程的锁为旧轮次背书，即使两次启动的毫秒时间相同。

初始 journal 已发布但尚未取得存活锁、尚无本次完成轮次的 idle 阶段，在 30 秒启动 deadline 内为不健康的 `starting`；超过 deadline 仍未取得存活则为 `interrupted`。这个短暂阶段不应触发已运行服务的故障通知。

`HealthStore.snapshot()` 提供上述一致性读取。持续变化导致查询返回 `runtime=unavailable`、`error_code=health_snapshot_unstable`、退出码 2；消费者应稍后重新查询，不将其当作已确认停机。

路径由解析后的绝对数据目录唯一派生。例如 `data/live` 对应同级 `data/live-signal-service/`，默认被 Git 忽略：

- `health.json`：schema v1 权威快照，严格读取；损坏、未知字段/版本、重复 JSON 字段、错误 watchlist 覆盖或跨数据目录状态均拒绝。
- `service.lock`：实例排他锁；`supervisor.lock`：存活检测锁，由 supervisor 独占、查询共享探测。并发查询不会把另一个查询当成 supervisor，首次启动可等待短暂探测结束。进程退出由 OS 释放锁；文件存在不代表仍在运行，不应删除锁文件“解锁”。
- `observations.jsonl`：现有 Stage 0 cycle 日志及其 `.invalid` 标记协议。
- `service.log`：诊断日志，5 MiB 自动轮转，最多 3 个备份。
- `data-health.html`：刷新命令生成的数据健康页面。

健康快照与最近 100 个状态事件同文件提交；临时文件 flush/fsync 后，以 `os.replace` 为可见提交点，再同步目录。失败时读者只看到上一份或下一份完整快照。提交点之后的同步失败仍使服务退出；已可见的新快照不会退回旧版本。查询必须同时检查锁，不能只读历史结果。

事件含单调 `sequence`，类型为 `started`、`restarted`、`fault`、`recovered`、`stopped` 和 `interruption_acknowledged`。相同故障重复轮次只累加计数；故障集合变化或恢复才追加事件。`status --after-event N` 返回 N 之后的事件；游标超过最新序号或落后于保留范围会明确失败，消费者需核对当前状态后重新建立游标，不得静默跳过。通知端应分别消费事件与查询得到的停止/不响应状态，因为被强杀的进程不能自行追加故障事件。

状态是服务健康证据，观测日志是扫描证据，诊断日志不是权威提交点。[邮件消费者](email-alerts.md) 使用同目录的 `email.sqlite3` 保存游标、去重及送达状态，默认 dry-run。不能将健康、READY 或事件当下单授权，也不能把上次门禁判定当永久有效信号。

观测日志与健康状态发布共用 `observations.jsonl.lock`，与入场邮件最终核验及 SMTP 调用互斥。慢邮件可能推迟扫描写盘、健康检查点发布和后续周期调度；服务仍受配置的超时预算约束，trusted-data writer 锁不变。升级时同时停止并更新服务 supervisor、扫描 writer 和邮件消费者，不能让旧 writer 绕过发布锁；离线备份或清理前确认这些进程均已停止，运行中不删除锁文件。

## Windows 常驻操作手册

系统时钟在两次运行之间倒退时，旧轮次仍按原始时间保留，新运行使用当前时钟并凭 `service_run_id` 区分历史。事件顺序以 `sequence` 为准，不以墙上时钟排序；不会改写 trusted freshness 判定。当前运行内部的时钟倒退仍导致不响应或校验失败，需停止后重新启动。

显式 `recover` 收尾旧 journal 时，只将记账更新时间保持不小于旧值，恢复事件保留当前墙上时钟。随后新运行仍取实际当前时钟，不会等旧时间追平才启动。

先按 `--once` 验证一次。确认解释器、仓库路径、数据目录可写，旧刷新调度已停止，再选择前台持续运行：

```powershell
python -B -m mu_strategy.commands.signal_service run --data-dir data\live
```

正常停止使用该终端的 Ctrl+C。调度者等待已启动子进程退出后记录 `stopped` 并释放锁。超时子进程由 `subprocess.run` 终止并等待回收；诊断只写固定错误码，不复制 provider stderr 或异常中的敏感信息。

如需 Windows Task Scheduler，由操作者另外启用任务：操作程序填已验证的 Python **绝对路径**；参数填 `-B -m mu_strategy.commands.signal_service run --data-dir "<absolute data directory>"`；Start in 填仓库绝对路径。使用可写该目录的同一用户，选择“如果任务已运行，则不启动新实例”，不要每 5 分钟创建进程；循环由进程负责。自动重启只在退出后进行，不并行启动。首次在任务历史和 `status` 中确认解释器导入、账户路径和状态成功；Task Scheduler 的 Running 不等于服务健康。本文与测试不实际创建或启用任务。

Task Scheduler 的 End、强杀、系统重启或电源故障可能使 worker 与 supervisor 的生命周期分离。若 journal 停在 `refresh` 或 `scan`，再次 `run` 返回 `interrupted_cycle_requires_recovery`，不会自动清除：

1. 停止该调度任务；检查与该数据目录对应的旧 supervisor 和两个 worker，确认全部退出。仅 supervisor 不在不足以证明 writer 已退出。
2. 保留故障日志，查询状态，检查可信 pointer/generation，不删除健康文件或缓存来规避门禁。
3. 确认后执行恢复命令，再单次验证并恢复调度：

```powershell
python -B -m mu_strategy.commands.signal_service recover --data-dir data\live --confirm-workers-stopped
python -B -m mu_strategy.commands.signal_service run --once --data-dir data\live
```

该确认只承认旧 worker 已退出，不修改市场数据或证明数据恢复。上次停在 idle 可以直接重启，因为该阶段没有未完成 worker。健康文件损坏时拒绝覆盖；先停机、保留原文件并定位磁盘或人工编辑问题，再恢复，不能自动当作首次启动。

`service.log` 自动轮转。`observations.jsonl` 当前不支持在线轮转：计划维护时正常停机，确认所有 worker 退出且日志可被 `JsonlObservationRepository.read_cycles()` 严格读取，再整体归档日志；有 `.invalid` 标记时先处理日志无效状态，不单独删除标记。健康状态和事件游标继续保留。trusted generations 的保留策略按既有数据管理流程处理，不属于本服务。

## 验证与验收边界

```powershell
python -B -m unittest tests.test_signal_service
python -B -m unittest discover -s tests
```

测试使用临时数据、固定时钟、fake refresh 结果和真实离线 worker，覆盖锁冲突/进程死亡、重启、超时、恢复、提交失败和四种观察结果；不发送网络请求或启用常驻任务。一次通过不代表长期可靠：#99 仍需至少 20 个交易日运行记录及前瞻复盘，Task Scheduler 启用与 SMTP 送达应另行验证。
