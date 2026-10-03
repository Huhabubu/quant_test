# quant_test

用于快速验证量化策略想法的实验仓库。

当前实验：Binance USDⓈ-M 中，同一基础资产的 USDT 永续与 USDC 永续之间是否会出现 >18 bps 的瞬时可成交价差。

**v0 只监控和记录，不发送任何订单。**

## 1. 监控对象

程序启动时调用 Binance USDⓈ-M exchangeInfo，自动寻找同时满足以下条件的合约：

- status = TRADING
- contractType = PERPETUAL
- 同一 baseAsset 同时存在 AUSDT 和 AUSDC

币种列表不写死，会随 Binance 当前合约列表变化。

程序订阅：

- 所有匹配的 AUSDT bookTicker
- 所有匹配的 AUSDC bookTicker
- Spot USDCUSDT bookTicker，仅用于 USDC→USDT 估值换算

BBO（Best Bid/Offer）：订单簿当前最优买价和最优卖价。

## 2. 信号定义

### USDT_RICH

USDT 合约相对更贵，真实开仓方向对应：

- 卖出 AUSDT：使用 USDT 合约 Bid
- 买入 AUSDC：使用 USDC 合约 Ask

    edge_bps =
    (AUSDT_bid / (AUSDC_ask * USDCUSDT_mid) - 1) * 10000

### USDC_RICH

USDC 合约相对更贵：

- 卖出 AUSDC：使用 USDC 合约 Bid
- 买入 AUSDT：使用 USDT 合约 Ask

    edge_bps =
    ((AUSDC_bid * USDCUSDT_mid) / AUSDT_ask - 1) * 10000

默认 threshold = 18 bps。

18 bps 目前只是研究阈值：对应我们当前讨论的“四次 Taker 基础手续费量级”。它不是盈利阈值；未来还必须考虑平仓时 BBO、滑点、腿风险、资金费率等。

### FX 的重要假设

v0 使用 Spot USDCUSDT 的 BBO midpoint（中间价）将 USDC 合约报价换成 USDT 单位。

它现在只是 valuation reference（估值参考），程序没有第三条 USDCUSDT 执行腿。

后续如果决定实际交易，需要重新决定 FX 是否需要对冲，以及估值应使用 mid、Bid/Ask 还是账户级估值。

## 3. 一个信号是什么

不会每个行情包都重复打印。

例如：

    17 bps
    19 bps  <- START
    23 bps
    31 bps  <- max
    22 bps
    17 bps  <- END

这是一整个事件。

程序记录 start edge、max edge、end edge、duration、方向、两个合约 BBO、USDCUSDT BBO 和关键时间戳。

## 4. 时间戳语义

这是 v0 的重点。

### Binance futures 时间

USDⓈ-M bookTicker 提供：

- T：matching-engine transaction time（撮合引擎交易时间）
- E：event time（Binance 生成该推送事件的时间）

程序分别保存 trigger_exchange_tx_ms 和 trigger_exchange_event_ms。

### 本地时间

WebSocket 消息一交给 Python，就在 JSON 解码前立即记录：

- local_recv_wall_ns = time.time_ns()
- local_recv_mono_ns = time.monotonic_ns()

计算完价差以后再记录：

- local_detect_wall_ns
- local_detect_mono_ns

从而得到：

    processing_us
    = local_detect_mono_ns - local_recv_mono_ns

它表示当前 Python 进程从“拿到这条消息”到“完成本次信号判断”的本地处理时间。

### 信号的 event time

某一次 BBO 更新让 edge 首次从 <18 bps 跨到 >=18 bps，这条更新就是 trigger。

如果 trigger 来自 Futures：

- 信号 Binance 撮合时间 = trigger 的 T
- 信号 Binance 事件时间 = trigger 的 E
- 我们看到它的时间 = trigger 的 local_recv_wall_ns
- 我们判断出信号的时间 = local_detect_wall_ns

如果是 Spot USDCUSDT bookTicker 更新导致跨阈值，当前 Spot bookTicker 在本程序中没有 T/E，因此只记录本地 receive/detect 时间。

## 5. 防止以后误判“旧盘口 + 新盘口”

每个 START 快照同时保存：

- USDT 合约最后一个 T/E/recv
- USDC 合约最后一个 T/E/recv
- FX 最后一个 recv

并计算：

- pair_tx_skew_ms：USDT 与 USDC 两边 Binance T 的差
- pair_recv_skew_ms：两边本地收到时间的差
- usdt_age_ms
- usdc_age_ms
- fx_age_ms

v0 **先记录，不做 age/skew 硬过滤**。

原因是当前目标是观察真实分布；过早设 freshness 阈值可能把真正的 lead-lag 机会过滤掉。WebSocket 重连时会清空对应缓存，并终止跨重连的事件，避免明显的陈旧状态污染。

## 6. 输出

默认写到 data/。

### run_metadata_*.json

每次启动保存当次自动发现的合约列表、threshold、公式、时间戳定义和数据源。

### signal_transitions_YYYYMMDD.jsonl

保存每个事件的 START / END 和完整快照，用于后续精确检查时间和盘口。

### signal_events_YYYYMMDD.csv

每个完整事件一行，方便直接用 pandas / Excel 统计事件次数、最大 edge、持续时间、方向、trigger 和关键时间差。

## 7. 安装

Python 3.11+。

Windows PowerShell：

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt

## 8. 运行

先看当前有哪些双合约：

    python monitor.py --list-pairs

正式监控：

    python monitor.py

修改阈值：

    python monitor.py --threshold-bps 25

停止使用 Ctrl+C。程序会尽量把仍处于 active 的事件以 SHUTDOWN 原因落盘。

## 9. v0 成功标准

先跑 24 小时，只回答：

1. >18 bps 一共出现多少个独立事件？
2. 哪些币最多？
3. 每个事件最大扩到多少 bps？
4. 持续时间分布是多少？
5. USDT_RICH / USDC_RICH 哪边更多？
6. trigger 更多来自 USDT 还是 USDC？
7. 这些信号的两腿 T skew 和本地 receive skew 有多大？

只有这些结果证明机会值得继续，才进入下一阶段：保存事件前后更完整的行情路径、加入 Trade/Taker flow、加入多档 Depth、分析收敛路径，并模拟真实交易成本。
