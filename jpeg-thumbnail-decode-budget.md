# JPEG 缩略图解码预算分析：解码器预缩放与缩略尺寸决策

分析对象：本仓库 Pillow（`src/PIL/` Python 层 + `src/libImaging/JpegDecode.c`、`src/decode.c` C 层），
实测环境为可编辑安装的本仓库代码（libjpeg-turbo，`PIL.features.check('libjpeg_turbo') == True`）。
本文区分两类结论：

- **可由源码保证的像素上界**：由确定的整数运算与分配点推出，不依赖运行环境；
- **需要实测的资源开销**：libjpeg 内部工作内存、RSS、重采样临时缓冲等，随实现/平台变化。

---

## 1. 解码链路总览（实际舍入与状态变化）

一次 `Image.open(...).thumbnail((tw, th))` 的完整路径：

| 步骤 | 代码位置 | 发生的事 |
|---|---|---|
| 打开识别 | `Image.py:3783` `_open_core` | 仅解析标记头得到原图 `(W, H)`；**按原图尺寸**执行一次 `_decompression_bomb_check(im.size)`；不分配像素缓冲 |
| 尺寸决策 | `Image.py:2951-2977` `thumbnail` | 目标框先 `math.floor`，再按原图宽高比算出 `final_size` |
| 预缩放请求 | `Image.py:2980-2985` | 调 `draft(None, (int(tw*g), int(th*g)))`，`g = reducing_gap`（默认 2.0） |
| 选 DCT 档位 | `JpegImagePlugin.py:439-469` `draft` | 整数除法选 `scale ∈ {1,2,4,8}`，**向上取整**改写 `self._size`，置 `decoderconfig=(scale, 0)` |
| 分配像素缓冲 | `ImageFile.py:478-481` `load_prepare` → `Image.core.new(mode, self.size)` | 按 draft 改写后的尺寸分配，**此处不再做 bomb 检查** |
| 解码器构造 | `ImageFile.py:418-419` → `Image.py:504-526` → `decode.c:876-923` | `decoderconfig` 透传为 C 侧 `context->scale` |
| libjpeg 配置 | `JpegDecode.c:216-219` | `scale_num=1; scale_denom=scale` |
| 输出尺寸校验 | `JpegDecode.c:239-245` | libjpeg 实际输出宽不得超过缓冲宽，否则 `IMAGING_CODEC_BROKEN` |
| 逐行解码 | `JpegDecode.c:250-266` | 每行 shuffle 进预分配缓冲 |
| 最终重采样 | `Image.py:2483-2503` `resize` | 用 draft 返回的浮点 `box` 映射坐标，输出 `final_size` |

关键事实：**像素缓冲只有一次分配**（`load_prepare`），其尺寸由 `draft` 写入的 `self._size` 决定；
libjpeg 本身不持有全尺寸输出图像，它逐行产出到该缓冲。

### 1.1 一个容易误判的事实：`draft` 快速 DCT 标志当前是死路径

`JpegImagePlugin.py:466` 恒写 `self.decoderconfig = (scale, 0)`，第二个参数固定为 `0`。
它在 `decode.c:920` 进入 `jpeg_decoder_state_context->draft`，而 `JpegDecode.c:220-223` 的

```c
if (context->draft) {
    context->cinfo.do_fancy_upsampling = FALSE;
    context->cinfo.dct_method = JDCT_FASTEST;
}
```

因此经 Pillow 正常 `draft()` 路径时 `context->draft == 0`，该分支不会触发；
预缩放只通过 `scale_num/scale_denom` 生效。全代码库中 JPEG 的 `decoderconfig` 仅在此处赋值，
没有任何地方写入第二参数 `1`。

---

## 2. 进入完整像素处理前的解码尺寸边界

设原图 `(W, H)`，缩略服务目标框 `(tw, th)`（`thumbnail` 的入参），`g = reducing_gap`。

### 2.1 第一步：draft 请求尺寸（向零截断）

`Image.py:2982`：

```
rw = int(tw * g)      # int() 对正数即向零截断
rh = int(th * g)
```

### 2.2 第二步：选择 2 的幂档位（整除 + 钳位）

`JpegImagePlugin.py:459-464`：

```
scale_raw = min(W // rw, H // rh)          # 整除（floor）
scale = max{ s ∈ {8,4,2,1} : scale_raw >= s }   # 从大到小第一个满足者
```

即：

| 条件（两维同时满足） | scale |
|---|---|
| `W >= 8·rw` 且 `H >= 8·rh` | 8 |
| 否则 `W >= 4·rw` 且 `H >= 4·rh` | 4 |
| 否则 `W >= 2·rw` 且 `H >= 2·rh` | 2 |
| 否则 | 1（不缩放，全尺寸解码） |

档位由**较短边的约束方向**决定（`min`），任一维不满足就降档。
当 `rw` 或 `rh` 为 0 时，`W // rw` 抛 `ZeroDivisionError`（实测确认）——
服务侧必须在入口拒绝含 0 维的目标框，Pillow 自身不做防御。

### 2.3 第三步：实际解码缓冲尺寸（向上取整）——像素上界的来源

`JpegImagePlugin.py:463`：

```
Wd = ceil(W / scale) = (W + scale - 1) // scale
Hd = ceil(H / scale) = (H + scale - 1) // scale
self._size = (Wd, Hd)
```

**这是源码可保证的解码像素上界**：

```
P_decode = Wd · Hd = ceil(W/scale) · ceil(H/scale)
```

为什么必须用 ceil 而不是 floor：libjpeg 以 DCT 块（8×8）为单位工作，缩放后的输出维度按
MCU 网格向上取整。C 侧 `JpegDecode.c:239-241` 会校验
`cinfo.output_width <= state->xsize` 且 `output_components·output_width <= state->bytes`，
Pillow 用 ceil 预留缓冲正是为了容纳 libjpeg 的网格取整。实测（libjpeg-turbo）：

| 原图 | scale | Pillow 缓冲（ceil） | 若按 floor | 实测 load 后尺寸 |
|---|---|---|---|---|
| 435×361 | 8 | 55×46 = 2,530 | 54×45 = 2,430 | 55×46 |
| 257×257 | 8 | 33×33 = 1,089 | 32×32 = 1,024 | 33×33 |
| 4000×3000 | 8 | 500×375 = 187,500 | 500×375（整除，无差异） | 500×375 |

ceil 相对 floor 的最坏单维偏差是 `scale - 1` 个像素；两维同时不对齐时，
像素数相对 `W·H/scale²` 的偏差量级约为 `(scale-1)(W+H)/scale²`。

### 2.4 档位选择的边界不等式（服务侧可直接用于预判）

把 2.2 的条件与 `rw = int(tw·g)` 结合，可在**只读取文件头、未分配任何像素内存**时预判档位：

- `scale = 8` 成立 ⇔ `W // int(tw·g) >= 8` 且 `H // int(th·g) >= 8`
- 一般地，选定档位 `s` 后，解码缓冲严格满足：

```
Wd <= W/s + 1,  Hd <= H/s + 1
P_decode <= (W/s + 1)(H/s + 1)
```

而 `scale = 1` 意味着预缩放完全失效，`P_decode = W·H`（全尺寸），这是预算分析必须显式处理的退化情形：
当目标框相对原图不够小（任一维缩小比例不足 2 倍）时，JPEG 解码器不省任何像素。

### 2.5 不同模式的字节上界（源码保证）

`load_prepare` 调 `Image.core.new(mode, (Wd, Hd))`，单像素字节数由 C 模式表固定
（`libImaging/Storage.c`）：

| 模式 | pixelsize | 缓冲字节上界（源码保证） |
|---|---|---|
| `RGB` | **4**（3 通道 + 1 填充，`Storage.c:135-142`；`tobytes()` 才打包成 3 字节） | `4·Wd·Hd` |
| `L` | 1 | `Wd·Hd` |
| `YCbCr` | 4 | `4·Wd·Hd` |
| `CMYK` | 4 | `4·Wd·Hd` |
| `RGBA`/`RGBX` | 4 | `4·Wd·Hd` |

注意不能用 `tobytes()` 的 3 字节/像素做容量规划——那是拷贝打包后的结果，
常驻核心图像仍是 4 字节/像素（实测 `tobytes()` 长度为 `3·Wd·Hd`，但核心对象 `bands=3`、内部按 32 位存储）。

此外读取侧还有固定的流式块开销：`decodermaxblock = MAXBLOCK = 65536`（`ImageFile.py:53,157`），
与图像尺寸无关，是常数项。

---

## 3. 最终缩略尺寸的成立条件

### 3.1 宽高比保持（`preserve_aspect_ratio`，`Image.py:2953-2972`）

入参先经 `provided_size = (floor(tw), floor(th))`。记原图宽高比 `a = W/H`：

- 若 `floor(tw)/floor(th) >= a`：目标框"偏宽"，高度顶格 `y = floor(th)`，
  宽度 `x = round_aspect(y·a)`，在 `floor` 与 `ceil` 中选使 `|a - x/y|` 最小者；
- 否则宽度顶格 `x = floor(tw)`，高度同理按 `x/a` 选取。

`round_aspect` 的结果同时不超过 `y·a` 的 ceil，因此**恒有 `x <= floor(tw)`、`y <= floor(th)`**，
即最终尺寸必然落在目标框内。实测：435×361 输入 `thumbnail((100,100))` → `(100, 83)`，
宽高比 1.205 对原图 1.2048。

### 3.2 提前返回条件

`Image.py:2958`：若 `floor(tw) >= W` 且 `floor(th) >= H`，`preserve_aspect_ratio` 返回 `None`，
`thumbnail` 直接 return——**不调 draft、不触发解码、不分配像素缓冲**。
实测确认目标框大于原图时图像尺寸保持不变。这是服务侧最便宜的短路路径。

### 3.3 draft box 与最终 resize 的坐标闭合

`draft` 返回 `box = (0, 0, W/scale, H/scale)`（浮点，`JpegImagePlugin.py:468`），
把"原图坐标"映射到缩小后缓冲。`thumbnail` 把它透传给 `resize(..., box=box, reducing_gap=g)`
（`Image.py:2991-2993`），重采样在缩小后的缓冲上按该 box 取样，输出 `final_size`。
因此最终画面比例由两级共同保证：

1. DCT 预缩放是**各向同性**的（两维同一 `scale`），不改变宽高比；
2. `resize` 用浮点 box 做亚像素映射，ceil 取整多出的边缘像素只影响 box 右端的浮点坐标，不拉伸画面。

### 3.4 第二次整数降采样是否发生（影响峰值的附加条件）

`resize` 内（`Image.py:2485-2501`）在 `resample != NEAREST` 时计算：

```
factor_x = int( box_width  / final_w / g ) or 1
factor_y = int( box_height / final_h / g ) or 1
```

仅当 `factor_x > 1 or factor_y > 1` 才再跑一次 `reduce()` 分配中间图像。
常规缩略图（draft 已把缓冲压到约 `g` 倍目标尺寸）下两个 factor 都是 1，实测
435×361→32×27、gap=2 时 `factor = (1,1)`，无第二次分配。
但**极端小目标**下仍可能触发：实测 4000×3000 调 `thumbnail((4,4))`，
draft 仍只能给 scale=8 的 500×375 缓冲（这是 libjpeg 8 倍下限），
随后 resize 阶段 `factor_x = 6`，会再分配一张约 84×63 的 `reduce` 中间图。
结论：最终输出虽只有 4×3，但解码缓冲预算由 scale=8 的下限决定，**不会随目标变小而继续下降**。

---

## 4. 延迟加载状态与解码配置变化对内存上限可靠性的影响

### 4.1 三个关键状态变量

| 状态 | 初始值 | 变化点 | 后果 |
|---|---|---|---|
| `self._im` | `None`（延迟加载） | 首次 `load()` 后非 `None` | 之后 `Image.load()`（`Image.py:1009-1011`）直接返回，不再走解码分配 |
| `self.tile` | `[_Tile("jpeg", ...)]` | `load()` 结束时清空为 `[]`（`ImageFile.py:463`） | `draft` 首检 `len(self.tile) != 1` 失败，返回 `None` |
| `self.decoderconfig` | `()`（falsy） | 首次有效 `draft` 置 `(scale, 0)`（truthy） | 第二次 `draft` 在 `JpegImagePlugin.py:446-447` 直接返回 `None`，**保护重复调用** |

### 4.2 同一内存上限在什么条件下仍然可靠

**可靠的前提：在首次 `load()` 之前完成且仅完成一次有效 `draft`。** 此时：

- 缓冲只在 `load_prepare` 分配一次，尺寸就是 `(ceil(W/s), ceil(H/s))`；
- 重复 `draft` 被 `decoderconfig` 短路（实测第二次返回 `None`，配置与尺寸不变）；
- 截断文件不改变预算：`LOAD_TRUNCATED_IMAGES=True` 时缓冲仍按完整 draft 尺寸分配，
  只是解码提前结束（实测 1/3 截断的 4000×3000 文件仍分配 500×375）；
- 模式降级（RGB→L/YCbCr，`JpegImagePlugin.py:454-456`）只可能降低 pixelsize，
  不放大像素数。

### 4.3 上限失效的状态变化（服务侧必须规避）

1. **先 `load()` 后 `draft()`**：`tile == []`，`draft` 返回 `None`，
   缓冲已按**原图全尺寸** `W·H` 分配。实测确认 load 后调 draft 无任何效果，尺寸保持 435×361。
   任何隐式触发像素访问的操作（`crop`、`filter`、`getdata`、`tobytes`、pickle 的
   `__getstate__` 等都会调 `load()`）都会锁死全尺寸预算。
2. **非 JPEG/MPO 格式**：基类 `Image.draft` 是空实现（`Image.py:1478` 只有 `pass`，返回 `None`）；
   实测 PNG 调 `draft` 返回 `None` 且 tile 仍是 `zip` 解码器——预缩放预算对这些格式不成立，
   只能按全尺寸规划（MPO 继承 `JpegImageFile`，同 JPEG 结论）。
3. **多 tile 或已被插件改写 tile 的 JPEG**：`len(tile) != 1` 时 `draft` 返回 `None`。
4. **目标框含 0 维**：`ZeroDivisionError`（见 2.2），请求应在入口拒绝。
5. **bomb 检查的位置错位**：`_decompression_bomb_check` 只在 `open` 时按**原图尺寸**执行
   （`Image.py:3783`），draft 缩小与 `load_prepare` 分配路径上都没有二次检查（实测：
   把 `MAX_IMAGE_PIXELS` 设为 10 万，open 时对 157k 像素原图报警，draft 到 55×46 后
   load 无任何告警）。因此 `Image.MAX_IMAGE_PIXELS` 不能替代服务侧基于 `(Wd, Hd)` 的预算判断；
   反过来，原图超过 `2·MAX_IMAGE_PIXELS` 时请求在 open 阶段就被
   `Image.DecompressionBombError` 拒绝，根本到不了 draft。

### 4.4 服务侧接受请求的判据（建议）

按时间顺序，每一步都只依赖文件头或纯整数运算，可在分配像素内存前完成：

1. **格式判据**：`isinstance(im, JpegImageFile)`（含 MPO）。不满足则走全尺寸预算分支。
2. **输入合法性**：`tw > 0 and th > 0`，否则拒绝（避免 `ZeroDivisionError`）。
3. **短路判据**：`floor(tw) >= W and floor(th) >= H` → 无需解码像素，直接接受。
4. **bomb 前置判据**：`W·H > 2·MAX_IMAGE_PIXELS` 会在 open 处直接抛
   `Image.DecompressionBombError`；服务应把它作为显式拒绝项，而不是依赖异常冒泡。
   注意引用命名常量，不要硬编码阈值（运行时可被改写）。
5. **档位与缓冲预判**：按 2.2/2.3 用整数运算算出 `scale` 与 `(Wd, Hd)`，
   以 `B = pixelsize(mode) · ceil(W/scale) · ceil(H/scale)` 作为**源码保证的解码缓冲上界**，
   对照服务自身内存预算决定接受/降级/拒绝。
6. **状态纪律**：接受后必须在任何像素访问之前调用 `thumbnail`/`draft`，
   且不要对同一实例二次 `draft`；需要全分辨率副本时先 `copy()`。
7. **极端小目标**：预算按 scale=8 的下限 `(ceil(W/8), ceil(H/8))` 计，而非目标尺寸；
   若启用非 NEAREST 重采样，再为 `resize` 的 `reduce` 中间缓冲留余量（见 4.5）。

### 4.5 源码保证 vs 必须实测的部分

**源码可保证（确定性整数/分配语义）：**

- 解码缓冲维度 `(ceil(W/s), ceil(H/s))` 与像素数 `P_decode`；
- 各模式 pixelsize（RGB=4、L=1、CMYK=4 等）与缓冲字节上界；
- scale 档位只可能是 {1,2,4,8}，且两维同比例；
- 缓冲仅分配一次、重复 draft 无效、load 后 draft 无效、bomb 检查仅在 open 时发生；
- 最终缩略尺寸 `x <= floor(tw)`、`y <= floor(th)` 且宽高比不被 DCT 缩放改变。

**必须实测（无源码级上界，随 libjpeg 版本/图像内容/平台变化）：**

- libjpeg 内部工作内存（Huffman/量化表、系数块、悬挂式输入缓冲、逐行 scanline buffer）；
- 进程 RSS 峰值与分配器保留行为。实测参考（4000×3000 RGB，单独进程，`ru_maxrss` 增量）：

  | 路径 | 解码尺寸 | 解码缓冲理论值 | 实测 RSS 峰值增量 |
  |---|---|---|---|
  | 全尺寸 load | 4000×3000 | 48.0 MB（4 B/px） | ≈ 49.5 MB（另含全 resize 流程时）/ ≈ 84.6 MB（含 `tobytes` 打包副本的一次测量） |
  | scale=2 | 2000×1500 | 12.0 MB | ≈ 14.2 MB |
  | scale=4 | 1000×750 | 3.0 MB | ≈ 5.4 MB |
  | scale=8 | 500×375 | 0.75 MB | ≈ 3.3 MB |

  RSS 增量在最小档已不随像素数线性下降（3.3 MB 中大部分是解释器/libjpeg 固定占用），
  说明固定开销主导小图预算，不能按 `4·P_decode` 线性外推进程内存。
- 重采样临时缓冲：常规 gap=2 下第二次 `reduce` 不触发（factor=1），实测 LANCZOS 重采样
  仅增加约 0.25 MB；但极端小目标会触发额外 `reduce` 分配（3.4 节），其尺寸需按
  `factor_x/factor_y` 实测核算。
- 不同 libjpeg 实现的网格取整细节：`JpegDecode.c:239-245` 的校验是防御性的，
  本仓库实测的 libjpeg-turbo 输出与 Pillow ceil 缓冲一致；更换 IJG libjpeg 时应重新实测该不变量。
- 并发请求数：单请求上界可由源码推出，服务总内存 = 并发数 ×（单请求缓冲上界 + 实测固定开销），
  并发度本身只能靠压测确定。

---

## 5. 结论摘要

1. **解码像素边界**：`draft` 以 `min(W//rw, H//rh)` 选 {1,2,4,8} 档，缓冲按
   `(ceil(W/s), ceil(H/s))` 分配——这是进入完整像素处理前唯一的、源码可证的像素上界；
   RGB 模式按 4 字节/像素计，预算式为 `4·ceil(W/s)·ceil(H/s)`。
2. **缩略尺寸成立条件**：DCT 各向同性缩放 + `round_aspect` 的 floor/ceil 选择，
   保证最终尺寸恒在 `(floor(tw), floor(th))` 内且保持宽高比；目标框不小于原图时零像素处理直接返回。
3. **内存上限可靠性**：仅在"load 前一次有效 draft"的状态纪律下可靠；load 之后、
   非 JPEG 格式、多 tile、二次 draft 都会使预缩放失效或被忽略。`MAX_IMAGE_PIXELS`
   只在 open 时按原图执法，不能替代解码预算判断。
4. **服务判据**：格式 → 非零校验 → 短路 → bomb 前置 → 纯整数预判 `(Wd,Hd)` 与字节上界 →
   按 scale=8 下限兜底极小目标 → 严格在首次像素访问前发起 draft。
5. **可证与实测的分界**：像素数、pixelsize、状态机行为可由源码保证；libjpeg 工作内存、
   RSS 峰值、重采样临时缓冲与并发容量必须在目标部署环境实测，实测显示最小档的固定开销
   （约 3 MB 级）主导预算，不能按像素数线性估算。
