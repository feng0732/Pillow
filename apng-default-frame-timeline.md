# APNG 默认图、帧索引与播放时间线分析报告

- 分析对象：本仓库 Pillow（提交 `af804a5`，版本 `13.1.0.dev0`）的 APNG 读取、跳帧与保存路径
- 主要源码：`src/PIL/PngImagePlugin.py`（APNG 支持内嵌在 PNG 插件中，没有独立的 `ApngImagePlugin.py`）
- 测试基线：`python3 selftest.py` 通过；`python3 -m pytest Tests/test_file_apng.py` 56 项全部通过
- 经验性复算：本报告所有"实测"结论均可用 `.analysis/probe*.py`（venv 中 `python -I` 运行）复现；引用的仓库夹具在 `Tests/images/apng/`

---

## 1. 数据模型：文件物理帧、acTL 逻辑帧与 Pillow 公开帧

APNG 文件在 PNG 流中由三类私有扩展块承载动画：

| 块 | 含义 | Pillow 处理位置 |
|---|---|---|
| `acTL` | 动画控制：`num_frames`（逻辑动画帧数）、`num_plays`（循环次数，0=无限） | `PngStream.chunk_acTL`，`PngImagePlugin.py:725-744` |
| `fcTL` | 单帧控制：序号、帧区域 bbox、`delay_num/delay_den`、dispose、blend | `PngStream.chunk_fcTL`，`PngImagePlugin.py:746-774` |
| `fdAT` | 帧图像数据（比 IDAT 多 4 字节 sequence number） | `PngStream.chunk_fdAT`，`PngImagePlugin.py:776-790` |

而默认图（default image）就是 APNG 规范里那段普通的 `IDAT` 图像数据：不支持 APNG 的解码器只看到它。Pillow 在 `chunk_IDAT` 中用一个关键副作用标记它——**仅当此前已解析到 `acTL` 时**，把 `im_info["default_image"] = True`（`PngImagePlugin.py:503-504`）：

```python
if self.im_n_frames is not None:
    self.im_info["default_image"] = True
```

由此产生三层编号，必须区分：

1. **文件物理布局**：块的先后顺序（IDAT/fdAT）。
2. **APNG 逻辑动画帧**：`acTL.num_frames` 个，每个逻辑帧对应一个 `fcTL`，按 `sequence_number` 严格连续编号（从 0 开始）。默认图**不是**逻辑动画帧，没有 `fcTL`。
3. **Pillow 公开帧**：`Image.n_frames` 与 `seek(i)` 使用的 0 基索引。

### 1.1 默认图是否存在如何改变公开帧数与编号

`_open()` 收尾处的计算（`PngImagePlugin.py:854-875`）：

```python
self.n_frames = self.png.im_n_frames or 1          # 先取 acTL.num_frames
self.default_image = self.info.get("default_image", False)
...
if self.png.im_n_frames is not None:
    ...
    if self.default_image:
        # IDAT chunk contains default image and not first animation frame
        self.n_frames += 1                          # 有默认图时再 +1
    self._seek(0)
self.is_animated = self.n_frames > 1
```

| 文件情形 | acTL 逻辑帧数 N | 默认图 | Pillow `n_frames` | 公开帧 0 是什么 | 逻辑动画帧 k 的公开索引 |
|---|---:|:---:|---:|---|---:|
| 普通静态 PNG | 无 acTL | — | 1 | 唯一图像 | — |
| 无默认图的 APNG（首帧 fcTL 在 IDAT 前） | N | 否 | N | 逻辑动画帧 0 | k |
| 带默认图的 APNG（IDAT 在前，无 fcTL） | N | 是 | **N+1** | 默认图（非动画帧） | **k+1** |
| acTL 非法/重复/位于 IDAT 之后 | 被忽略 | — | 1 | 静态回退图像 | — |

实测对照（`probe.py`）：

- `single_frame.png`：`acTL.num_frames=1`、无默认图 → `n_frames=1`、`is_animated=False`、mimetype 仍是 `image/apng`；公开帧 0 即可读到 `fcTL` 的 `duration=1000.0`。
- `single_frame_default.png`：`acTL.num_frames=1`、有默认图 → `n_frames=2`、`is_animated=True`；公开帧 0 是红色默认图且 `info` 中**没有** `duration/disposal/blend/bbox`，`seek(1)` 才是绿色逻辑动画帧 0（`duration=1000.0`）。
- `delay.png`：`acTL.num_frames=4` + 默认图 → 公开 5 帧；对应关系实测为：

  | 公开索引 `seek(i)` | APNG 逻辑帧 | `info["duration"]` |
  |---:|---:|---:|
  | 0 | 默认图（无 fcTL） | `None`（键不存在） |
  | 1 | 0 | 500.0 ms |
  | 2 | 1 | 1000.0 ms |
  | 3 | 2 | 500.0 ms |
  | 4 | 3 | 1000.0 ms |

这与官方文档 `docs/handbook/image-file-formats.rst:1019-1034` 的描述一致：带默认图时 `n_frames = frame_count + 1`，必须 `seek(1)` 才能取到第一个真正的动画帧。

### 1.2 默认图标记本身依赖文件布局

`default_image` 不是一个独立块，而是"`acTL` 出现在第一个 `IDAT` 之前"这一布局事实的推论：

- `chunk_actl_after_idat.png` 实测 `is_animated=False`、mimetype 退化为 `image/png`：解析在首个 IDAT 处即 `EOFError` 停摆（`chunk_IDAT`，`PngImagePlugin.py:498-509`），之后的 acTL 永远不会被处理。
- 出现第二个 acTL（`chunk_multi_actl.png`）或 `num_frames` 为 0 / 超过 `0x80000000` 时，`im_n_frames` 被置 `None` 并告警 "Invalid APNG, will use default PNG image if possible"（`PngImagePlugin.py:733-740`），随后按静态 PNG 处理。
- 因此"有没有默认图"在 Pillow 里等价于"IDAT 被解析时 `im_n_frames is not None` 是否成立"，这是一个**布局依赖**判断，不是对文件语义的独立校验。

---

## 2. 每帧时长：读取换算与再保存对应

### 2.1 读取路径：fcTL → `info["duration"]`（毫秒浮点）

`chunk_fcTL`（`PngImagePlugin.py:768-771`）：

```python
delay_num, delay_den = i16(s, 20), i16(s, 22)
if delay_den == 0:
    delay_den = 100
self.im_info["duration"] = float(delay_num) / float(delay_den) * 1000
```

要点：

1. 单位换算固定为 `duration_ms = delay_num / delay_den × 1000`，类型是 `float`。
2. `delay_den == 0` 按 APNG 规范视为 100（实测 `delay_zero_denom.png` 的 `50/0` 读回 500.0 ms）。
3. **`delay_num == 0` 时 Pillow 原样给出 `0.0`，不做规范替换**。APNG 规范规定 `delay_num=0` 应按 100 Hz 即 100 ms 处理；实测 `delay_zero_numer.png` 公开帧 1、2 的 duration 均为 `0.0`。这是有意的源码行为而非疏漏（代码没有任何零值分支），消费者若按规范语义计算总时长需自行替换。
4. `info` 是同一个可变字典，每次 `seek` 被原地覆写（实测保存旧引用后其 `duration` 会随 seek 变化：500.0 → 1000.0）。带默认图的文件在刚 `open` 时 `info` 里根本没有 `duration` 键，要 `seek(1)` 后才出现。
5. `disposal`、`blend`、`bbox` 与 duration 同处一个 fcTL，生命周期完全相同：默认图帧上全部缺失。

### 2.2 保存路径：毫秒 → 有理数 → fcTL 分数

`_write_multiple_frames` 写每个动画帧时（`PngImagePlugin.py:1320-1340`）：

```python
frame_duration = encoderinfo.get("duration", 0)          # 缺省 0 ms
delay = Fraction(frame_duration / 1000).limit_denominator(65535)
if delay.numerator > 65535:
    raise ValueError("cannot write duration")
# o16(delay.numerator), o16(delay.denominator) 写入 fcTL
```

- 输入单位是毫秒；先除以 1000 换成秒，再用 `Fraction.limit_denominator(65535)` 找分母不超过 16 bit 的最接近有理数，分子超 65535 直接报错。
- 实测边界：`65535000 ms` 可写（=65535/1），`65535001 ms` 抛 `ValueError: cannot write duration`。
- 有理逼近带来往返误差，但对常见值精确：`0.5 → 0.5`、`1000/3 → 333.3333333333333`（分数 1/3）、`33.3 → 33.300000000000004`；过小的值会被近似吞掉，实测 `0.001 ms` 读回 `0.0`。
- `duration` 可以是标量或列表/元组。**列表只按动画帧编号索引，不含默认图条目**；文档 `image-file-formats.rst:1110-1116` 明确要求列表长度等于"实际动画帧数"，带默认图时不应为默认图提供条目。源码在 `PngImagePlugin.py:1257-1258` 用动画帧计数器 `frame_count` 下标取值，列表过短实测抛 `IndexError: list index out of range`，过长则静默忽略多余项。
- 不带 `duration` 时写出 `delay=0/1`，读回 `0.0`。

### 2.3 保存侧的两个隐式帧变换（都会改变索引与时长分布）

1. **相邻全等帧合并**（`PngImagePlugin.py:1267-1284`）：非首帧若与上一帧像素完全相同（`ImageChops.subtract_modulo` 的 bbox 为空）且带 duration，则把两帧时长累加到前一帧并跳过本帧；前提是累加后的时长仍能编码进 16 bit 分子，否则保留两帧。实测三帧相同 `[100,100,100]` + 一个不同帧 `[250]` 写出后只剩 2 帧，duration 为 `[300, 250]`，总时长 550 ms 不变但**帧索引整体收缩**。
2. **单动画帧且无默认图时退化为静态 PNG**（`PngImagePlugin.py:1289-1290`）：`len(im_frames)==1 and not default_image` 时直接返回单图，不写 acTL/fcTL。实测 `save_all=True, duration=100` 的单帧产物块序列只有 `IHDR/IDAT/IEND`，`n_frames=1`、mimetype=`image/png`、duration 丢失；而加 `default_image=True` 后保留 acTL，仍是 2 帧 APNG。
3. 增量 bbox：保存时对每帧做差集裁剪，fcTL 写子区域坐标（实测改动 4×1 像素的帧写出 `bbox=(2,3,6,4)`），读取端再按 dispose/blend 合成回全画布。**像素时间线等价，文件内帧区域布局不等价**。

---

## 3. 跳帧语义：任意跳转是怎样实现的

`PngImageFile.seek`（`PngImagePlugin.py:909-922`）与 `_seek`（`924-1019`）：

- 公开的 `seek(frame)` 支持任意目标索引，但内部 `_seek` 只接受"下一帧"（`frame != self.__frame + 1` 即抛 `ValueError: cannot seek to frame N`，`PngImagePlugin.py:946-948`）。
- 因此向**后**跳转时，`seek` 先调用 `_seek(0, rewind=True)` 回到 `_open` 时保存的 `save_rewind()` 快照（`PngImagePlugin.py:433-444, 868-870, 930-938`），再从帧 0 逐帧正向解码到目标。任意跳转的代价是 O(n) 次重新解码，而不是 O(1) 定位。
- 越界由 `ImageFile._seek_check`（`ImageFile.py:501-514`）拦截，抛 `EOFError: attempt to seek outside sequence`；直接调用私有 `_seek` 越过文件末尾则可能表现为 `OSError: Truncated File Read`（实测）。
- 跳转过程中 dispose（NONE/BACKGROUND/PREVIOUS）与 blend（SOURCE/OVER）在 `_seek` 和 `load_end`（`PngImagePlugin.py:1068-1141`）里逐帧重演，返回的始终是**合成后的全逻辑画布**（文档 `image-file-formats.rst:1042-1048` 承诺"what a web browser would render"）。
- 序号合法性是硬校验：fcTL/fdAT 的 sequence number 必须从 0 严格递增，任何 gap/repeat/reorder 都抛 `SyntaxError: APNG contains frame sequence errors`（`PngImagePlugin.py:755-759, 786-788`），不做修复重排（文档 `:1050-1051`）。七个 `sequence_*.png` 夹具覆盖该路径。
- 副作用提示：读取 `im.text` 会内部 seek 到最后一帧再恢复（`PngImagePlugin.py:877-891`），实测 `tell()` 最终恢复为 0，但期间发生过完整解码。

---

## 4. 保证矩阵：起始画面、播放顺序、总时长

图例：✅ 有源码路径直接保证；⚠️ 依赖文件布局/元数据正确性，仅有限验证；❌ 无保证或存在已知偏差。

| 性质 | 仅读取 + 任意 seek | 读取元数据后（不 load 全部帧） | 保存往返（round trip）后 |
|---|---|---|---|
| **起始画面 = `seek(0)` 的内容** | ✅ 无默认图时即逻辑帧 0；有默认图时即 IDAT 默认图。rewind 快照保证回退后像素可复现（`test_rewind`、`test_apng_basic`） | ✅ `open` 仅解析到首个 IDAT/fdAT，默认图无需解码后续块即可得到 | ⚠️ 仅当显式传 `default_image=True` 时保留独立默认图；否则默认图被提升为逻辑帧 0（见 §5 示例 B）。注意 `frame.copy()` 会携带 `info["default_image"]`，使保存端在未显式传参时**隐式继承**该标记（`PngImagePlugin.py:1376-1378`） |
| **播放顺序 = fcTL 序号顺序** | ✅ 严格递增校验 + 单向 `_seek` 状态机，乱序文件直接 SyntaxError，不存在"读到但顺序错"的状态 | ⚠️ `open` 阶段只校验到首个数据块；深层序号错误要到 `seek/load` 才暴露（`sequence_*` 夹具均在 seek+load 时抛错） | ✅ 写出端 `seq_num` 从 0 连续分配（`PngImagePlugin.py:1310, 1331, 1352-1358`），自身产物必然有序；但**全等帧合并会删除中间帧**，顺序不变而索引改变 |
| **公开帧数** | ⚠️ 直接信任 `acTL.num_frames`（+默认图 1），`open` 时不实际清点 fcTL。实测 `syntax_num_frames_low.png`（声明 1、实有 2 个 fcTL）公开 `n_frames=2`，多出的物理帧永远不可达；`syntax_num_frames_high.png`（声明 3、实有 2）公开 4 帧，迭代到末尾才 `OSError: Truncated File Read` | 同左：帧数在元数据阶段即定，错误延迟到迭代时爆发 | ⚠️ 受合并/单帧退化影响可能减少；`acTL` 写出值 = 合并后的 `len(im_frames)`（`PngImagePlugin.py:1293-1298`），自身产物自洽 |
| **每帧 duration 与 fcTL 一一对应** | ✅ 每个 fcTL 在对应 `_seek` 中解析进 `info`，默认图帧无此键；`delay_den=0→100` 有规范处理 | ❌ 带默认图时 `open` 后读不到任何 duration；无默认图时只能拿到帧 0 的值，其余必须逐 seek | ⚠️ 数值经 `Fraction.limit_denominator(65535)` 有理化（亚毫秒级可能变 0）；全等帧时长被累加；默认图从不接收 duration（设计如此） |
| **一轮播放总时长** | ❌ 无公开 API。只能自行 `seek` 全部动画帧求和；默认图不贡献时长；`delay_num=0` 按 Pillow 读数为 0 而非规范的 100 ms，与浏览器口径可能不同 | ❌ 不遍历就无法求和，且 acTL 不携带总时长 | ⚠️ 在不触发合并/退化、且所有时长可精确有理化时总量守恒（实测 550 ms、3000 ms 用例守恒）；合并改变分布但保持算术和；零值语义偏差会原样传播 |
| **循环次数 `loop`** | ✅ 取自 `acTL.num_plays`，0=无限（`num_plays*.png` 实测 0/1） | ✅ acTL 在头部，`open` 即可读 | ✅ 保存默认 0，或取 `im.info["loop"]`/显式参数（`PngImagePlugin.py:1239`）；测试 `test_apng_save_duration_loop` 覆盖往返 |
| **dispose/blend 语义** | ✅ 逐帧合成结果与规范 dispose/blend 一致（大量夹具像素断言） | ❌ 仅当前帧 info 可见，且默认图帧缺失 | ❌ **不自动继承**：保存缺省恒为 `OP_NONE`/`OP_SOURCE`（`PngImagePlugin.py:1325-1326`）。实测源文件帧 1 的 `disposal=1(background), blend=1(over)`，朴素重存后全部变 `0/0`。因写出的是已合成的全画布帧，视觉通常仍等价，但文件语义已变 |
| **首帧就是动画首帧（无默认图布局）** | ⚠️ 依赖"fcTL 位于 IDAT 之前"的物理布局；布局错误时整个动画被静默降级为静态 PNG | 同左 | ✅ 无默认图保存时刻意把首帧放进 IDAT（`PngImagePlugin.py:1344-1350`），符合规范的向后兼容布局 |

---

## 5. 可复算的错位风险示例

### 示例 A：duration 列表按公开帧索引对齐 → 整体前移一帧（off-by-one）

对 `delay.png`（默认图 + 4 个动画帧，公开 5 帧）做朴素往返。读取端按公开索引收集到：

```
公开索引 i : 0      1      2       3      4
duration   : None  500.0  1000.0  500.0  1000.0
身份        : 默认图  动画0   动画1    动画2   动画3
```

若用 `[f.info.get("duration", 0) for f in Iterator(im)]` 得到 `[0, 500, 1000, 500, 1000]` 并原样传给 `save(..., default_image=True, duration=该列表)`，保存端只按 **4 个动画帧**取下标 0..3（`PngImagePlugin.py:1257-1258`），实测产物读回：

```
公开索引 0: None      （默认图）
公开索引 1: 0.0       ← 本应 500，吃下了公开索引 0 的占位 0
公开索引 2: 500.0     ← 本应 1000
公开索引 3: 1000.0    ← 本应 500
公开索引 4: 500.0     ← 本应 1000；原最后一帧的 1000 ms 被静默丢弃
```

时间线由 `500+1000+500+1000 = 3000 ms` 缩成 `0+500+1000+500 = 2000 ms`，且每一帧都错配。仓库自己的正确写法见 `Tests/test_file_apng.py:573-583`：收集时刻意 `if i != 0 or not default_image` 跳过默认图槽位，使列表长度回到动画帧数 4。

复算命令：`.venv/bin/python -I .analysis/probe4.py`（"naive round trip" 一节）。

### 示例 B：默认图标记的隐式继承 → 帧身份在不知情时被保留或被改写

`Image.copy()` 复制 `info` 字典，所以从带默认图文件取出的 `frames[0].info["default_image"] is True`。保存端参数解析为（`PngImagePlugin.py:1376-1378`）：

```python
default_image = im.encoderinfo.get("default_image", im.info.get("default_image"))
```

- 不传该参数：隐式为 True，产物仍是 `IHDR, acTL, IDAT, fcTL, fdAT, IEND` 布局，公开帧 0 继续是无 fcTL 的默认图（实测）。
- 显式 `default_image=False`：同一批像素被重排成 `IHDR, acTL, fcTL, IDAT, fcTL, fdAT, IEND`——原默认图**提升为逻辑动画帧 0**，公开帧数从 2 变为 2 但身份全变：新帧 0 现在带 fcTL，且因没有给它配 duration 而得到 `0.0 ms`（实测）。任何把"公开帧 0 = 静态预览"写死的下游逻辑都会在此错位。

### 示例 C：全等帧合并使保存后的索引失效

输入 3 个相同红帧 + 1 个绿帧，duration `[500, 100, 150, ...]` 风格的用例（仓库测试 `test_apng_save_duration_loop` 同款）：保存后 `n_frames` 由 3 收缩为 2，原公开索引 1 的帧消失，其 100 ms 并入索引 0（读回 600 ms）。若外部系统缓存了"第 2 帧事件"之类的索引型注解，往返后注解将指向完全不同的画面。合并条件还依赖时长能否编码（`PngImagePlugin.py:1277-1284`），同样像素在不同 duration 下一个合并、一个不合并，行为不稳定，无法只凭像素预测。

### 示例 D：acTL 计数与实际 fcTL 不符 → 公开帧数本身就是谎言

| 夹具 | acTL 声明 | 实测 fcTL 数 | Pillow `n_frames` | 实际行为 |
|---|---:|---:|---:|---|
| `syntax_num_frames_low.png` | 1 | 2 | 2（含默认图 +1） | 第二个 fcTL/fdAT 物理存在但 `seek(2)` 被 `_seek_check` 拒绝，永远不可达 |
| `syntax_num_frames_high.png` | 3 | 2 | 4 | 迭代到尽头抛 `OSError: Truncated File Read`，`n_frames` 承诺的帧拿不到 |
| `syntax_num_frames_zero_default.png` | 0 | 0 | 1 | 告警后降级静态 PNG，默认图像素仍可正常 `load()` |

结论：**`n_frames` 是元数据信任值，不是清点结果**。任何把 `n_frames` 当作可解码帧数的契约都需要防御性处理。

---

## 6. 证据缺口（源码没有保证、需要有限验证或外部补齐的部分）

1. **没有总时长 API，也没有规范零延迟补偿。** 源码不暴露单轮总时长；`delay_num=0` 读回 `0.0` 而 APNG 规范要求按 100 ms 处理，Pillow 与浏览器的时间线口径在此分叉。报告中的"总时长"均为按 Pillow 读数手工求和，规范口径需消费者自行把零值替换为 100 ms。
2. **`n_frames` 与实际 fcTL 数不一致时不做预检。** 高报在迭代末尾才以 `OSError`（而非友好的 SyntaxError）爆发；低报会静默隐藏尾部帧。源码没有"打开时清点全部 fcTL"的路径，因为 `_open` 按设计在首个数据块即停。
3. **dispose/blend 不参与保存往返。** 读取端忠实呈现合成结果，但重存时这两个参数没有从 `frame.info` 自动回填的通道，缺省值 NONE/SOURCE 会覆盖源文件语义。视觉等价性建立在"Pillow 保存的是全画布合成帧"这一实现细节上，对再被其他工具按增量帧处理的场景无保证。
4. **duration 有理化的精度边界只有运行时错误、没有静态契约。** `limit_denominator(65535)` 的近似误差（如 `0.001 ms → 0.0`、`33.3 → 33.3000…4`）无文档化容差承诺；分子超限抛错但近似不抛错，静默偏差只能靠往返比对发现。
5. **默认图判定完全绑定块顺序。** acTL 位于 IDAT 之后、重复 acTL 等情况静默降级为静态 PNG（仅重复/非法计数时有 UserWarning），调用方若不检查 `is_animated`/mimetype，会把动画文件当静态图处理而无异常。
6. **任意 seek 的正确性依赖 rewind 快照的完整性。** `save_rewind()` 只保存 `im_info` 拷贝、tile 与 `_seq_num`（`PngImagePlugin.py:400-444`）；回退语义由 `test_rewind` 等测试守护，但源码层面没有对"快照后流被外部修改"的防护，文件句柄被替换/截断时行为未定义（`test_seek_after_close` 只证明 close 后抛 ValueError）。
7. **`im.info` 原地可变且跨 seek 复用。** 没有每帧不可变快照，持有旧引用观察到的值会随下一次 seek 改变；多线程并发 seek 同一 Image 对象无安全性保证（源码无锁）。
8. **本报告的经验结论基于仓库自带 54 个 APNG 夹具与合成文件**，未覆盖第三方编码器（如 ffmpeg、各类浏览器导出器）的全部合法块排列；尤其是私有块穿插、fdAT 极端分片等组合，只验证了 `split_fdat*.png` 两个代表路径。

---

## 7. 复算入口

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[tests]"
.venv/bin/python selftest.py
.venv/bin/python -m pytest Tests/test_file_apng.py -q
.venv/bin/python -I .analysis/probe.py    # 帧数/编号/seek/往返基础事实
.venv/bin/python -I .analysis/probe2.py   # 块布局、计数不符、bbox、单帧退化
.venv/bin/python -I .analysis/probe3.py   # 异常夹具结构、info 别名、duration 边界
.venv/bin/python -I .analysis/probe4.py   # off-by-one 错位与时间线求和
```

关键源码索引（本提交行号）：

- 默认图标记：`PngImagePlugin.py:503-504`；帧数 +1：`:854-875`
- fcTL 时长换算：`:768-771`；fdAT/序号校验：`:746-790`
- seek/rewind 状态机：`:909-1019`；合成（blend/dispose）：`:1068-1141`
- 保存参数解析：`:1376-1378`；帧合并：`:1267-1287`；acTL/fcTL/fdAT 写出：`:1293-1358`
- 文档承诺：`docs/handbook/image-file-formats.rst:998-1116`
