# 位域消息目录服务与总线观测仓

Python 3.11，纯标准库。两个实质源码模块：

| 模块 | 职责 |
| --- | --- |
| `bitcatalog/catalog.py` | 目录定义模型、校验（同时活动 + 物理位重叠证明）、位域编译、payload 编码/解码、可发布目录产品的序列化与加载 |
| `bitcatalog/warehouse.py` | SQLite 观测仓：把每帧绑定到不可变目录产品、整帧拒绝与错误定位、保存原始字节与活动信号、半开窗口精确有理数聚合 |
| `bitcatalog/reference.py` | 独立逐位参考模型（与编译掩码码完全不同的实现），仅用于测试/核对 |

## 协议语义（本题合同）

* 消息：`bus`、`frame_id`、固定 `payload_bytes`（1..64）、信号表，可选一个无符号无缩放 `selector`。
* 信号：唯一 `name`、非负 `start`、`width`（1..32）、`order`、`signed`、非零有理数 `factor` 与有理数 `offset`。
* **little**：`start` 为全局位号；全局位 `k` = 第 `k//8` 字节的低位 `k%8`；连续物理位依次是值的低→高位。
* **big**：`start=(byte_index, bit_index)`，`bit_index` 从该字节最高位 0 数到最低位 7；越过字节末尾后从下一字节最高位继续；连续物理位依次是值的高→低位。
* 超出正文的定义（任一字节/位越界、`width>32`、正文长度不符）整条拒绝。
* 有符号量按 `width` 位二进制补码解释；物理值 = `raw * factor + offset`，全程 `fractions.Fraction` 精确计算（JSON 中的十进制文本/浮点按十进制字面量解析，不走二进制浮点）。
* 普通信号始终活动；分支信号带一个或多个 selector 整数，仅当本帧 selector 命中时存在。
* 发布时拒绝任何**可能同时活动**且物理位重叠的信号对，包含跨字节与 little/big 两种位序混合；互斥分支（分支集合不相交）可复用同一批位。selector 自身不得与任何活动位重叠。
* 编码入口：给定活动信号 raw 值与 selector，生成真实 payload；未分配位恒为 0；写完立即用编译提取路径回读校验。

## 目录产品

`build_product()` 产生产品指纹（对规范化定义 JSON 的 SHA-256）与编译对象；`CatalogProduct.save()` 写单个 JSON 文件（含嵌入定义、编译摘要、哈希）。`load_product()` 加载时重算哈希，内容被改动即拒绝（`bad_product_hash`）。同一份原始 hex 用 v1 与 v2 两个产品分别解释，各自的结果互不干扰、可长期复查。

## 观测仓

* 输入 `frame_event_id`、`bus`、`frame_id`、发生时刻（整数纳秒或 ISO-8601，保留 9 位小数）、真实 hex 正文；每批绑定一个产品。
* 错误长度 / 未知消息 / 错误编码 → **整帧拒绝**，保留错误码与位置（hex 里为 nibble 偏移；长度错误为期望字节数）；被拒绝帧同样保存原字节（若可解析）与错误信息，但不产生任何观测行。
* 成功帧保存原始字节、selector、以及**只有本帧活动的信号**。不活动信号不写行——既不是 NULL 也不是 0，因此不可能进入均值分母。
* 查询 `query_window(product, signal, start_ns, end_ns)`：半开窗口 `[start, end)`，返回次数、精确和、精确均值，以及每一条源帧身份（事件 ID、bus、frame_id、时间、原始 hex、selector）。
* 存储布局：文本 ID 只存一次（`products`、`signals` 表），400 万行的观测表只存整数，主键 `(product_id, signal_id, occurred_at_ns, frame_pk)` 为 `WITHOUT ROWID`；`signals` 表按 `(product, bus, frame_id, name)` 建立信号身份，同名但属于不同消息的信号不会在聚合中混在一起（查询时若仍有歧义会要求显式 `bus`/`frame_id`）；数值以精确分子/分母对存储（超出 int64 时退化为十进制文本）。
* `batch_size` 只影响事务/内存粒度，不影响结果（测试与核对脚本都验证这一点）。

## 复跑

```bash
# 1) 单元/属性测试（35 项）
python3.11 -m unittest discover -s tests -v

# 2) 生成完整交付物：200 消息 × 50 信号、100000 帧、v1/v2 两个目录产品、数据仓
python3.11 tools/build_artifacts.py

# 3) 独立核对：逐位参考模型 × 编译码、抽取帧比对、批大小不变性、重开重建
python3.11 tools/verify.py

# 4) 演示
python3.11 tools/demo.py
```

快速小规模冒烟：

```bash
python3.11 tools/build_artifacts.py --out /tmp/art_small --messages 12 --frames 3000
python3.11 tools/verify.py --out /tmp/art_small --sample 200 --batch-sample 200
```

## 交付物（`artifacts/`）

| 文件 | 内容 |
| --- | --- |
| `catalog_v1.json` / `catalog_v2.json` | 可发布目录产品（嵌入定义 + 编译摘要 + 内容哈希） |
| `catalog_v1.definitions.json` / `_v2` | 源定义（修订版对前 40 条消息重命名并改变缩放） |
| `frames_v1.jsonl` | 100000 条总线事件（含非法 hex / 错误长度 / 未知消息） |
| `observations_v1.db` | 实际数据仓（绑定 v1 与 v2 两个产品的观测） |
| `build_report.json` | 规模、计时、拒绝样本、样例聚合、修订对照、重开重建核对 |
| `verification_report.json` | 独立核对结果 |

## 规模与性能

200 消息 × 50 信号（10000 个信号）、100000 帧、约 400 万条活动观测。

* 目录构建（定义解析 + 重叠证明 + 掩码编译）：约 1 秒（全量 200 条）。
* 编码/解码走预编译掩码 + 整数提取；同 width 重复 raw 模式命中值缓存，避免逐帧构造 `Fraction`。
* 观测仓批量插入走单事务 + `executemany`，400 万行需要十余秒；查询按 `(product, signal, 时间)` 主键范围扫描。

以上数字以 `build_report.json` 为准（每次运行机器不同，数字会浮动）。

## 测试与核对覆盖

* 跨字节两种位序的位位置与单比特权重（逐位参考模型对照，含 1..12 / 17..32 宽度）。
* 有符号最小值（负的最小值、-1、最大值）在跨字节位置下的补码解释与精确值。
* 不同互斥分支复用相同位；分支集合有交集时重叠仍被拒绝；selector 重叠被拒绝；跨位序重叠被拒绝。
* 有理数缩放（`1/1000`、`-5/7`、十进制文本 `0.1`）与 `raw*factor+offset` 的精确性。
* 非法 hex、奇数 nibble、错误长度、未知消息整帧拒绝并保留位置。
* 半开窗口边界；不活动信号不进入分母；批大小 1/7/64/5000 结果逐帧一致。
* 目录修订后同一原件可解释为不同信号；旧产品结果仍可复查；重开后从原始 hex 重建抽样结果一致。
