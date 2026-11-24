# 图片归档中的方位元数据生命周期分析

**分析对象：** Pillow 仓库实际代码（`src/PIL/`），版本 `13.1.0.dev0`（`src/PIL/_version.py`），分支 `task/120`，基线提交 `af804a5`。
**分析方法：** 逐行阅读 `ImageOps.exif_transpose`、`Image.getexif`、各格式插件的打开/保存路径；随后在本机编译该仓库（`/tmp/pillowvenv`，Python 3.12，启用 libjpeg/zlib；**libtiff、WebP、AVIF 未编译进该构建**，相关结论只依据源码）并用真实代码做了对照实验。下文每条结论都标注了代码位置或实验依据。
**适用场景：** “先按方位信息转正，再保存为后续可重复处理的素材”的归档流水线。

---

## 0. 结论摘要

1. **`ImageOps.exif_transpose()` 把“像素转正 + 删 EXIF 方位 + 删 XMP 方位 + 更新 `size`”放在同一次调用里**（`src/PIL/ImageOps.py:847-905`）。但四者**并非无条件同时成功**：XMP 的删除依赖正则匹配（`ImageOps.py:891-893`），写法不规范时会失败（§5.1 模式 B）；无转正需求（方位 1/非法值）时整段删除逻辑都不执行。只有在“方位值可映射 + XMP 写法可匹配”时，对结果的“再次转正”才是无操作（幂等），这一路径被官方测试覆盖（`Tests/test_imageops.py:544-546`）。
2. **方位来源有明确优先级：EXIF 优先，XMP 仅在 EXIF 中完全没有 `Orientation` 标签时作为补充**（`src/PIL/Image.py:1701-1710`）。EXIF 里出现 `Orientation=1` 也会**阻断** XMP 的取值——这是最容易造成下游误判的一条规则。
3. **不同格式对“打开时 size 是否已按方位交换”的处理不一致**：TIFF 在打开时（`_open`/`_setup`）就把 `size` 交换成显示方向，并在 `load()` 时自动原地转正；JPEG/PNG 在打开时 `size` 始终是存储方向。这直接决定“重复转正”和“重新读取”后的可观察结果。
4. **元数据不会自动继承到新文件**：`save()` 只写显式传入的 `exif=`/`xmp=`/`tiffinfo=`（`src/PIL/Image.py:2749, 2793-2796`、各插件 `encoderinfo.get(...)`）。因此“素材能否重复处理”取决于归档时是否**显式写入转正后的元数据**。
5. **已实测出五类会导致二次旋转、方向反转或尺寸错乱的坑**（详见 §5）。其中 TIFF + XMP-only 方位（§5.4）、TIFF 用 `tiffinfo=im.tag_v2` 归档（§5.3）、以及 TIFF 的 EXIF=1 + XMP=6（§5.6）会产出**像素与元数据/尺寸不一致、或两次读取结果不同的归档文件**；JPEG 的 EXIF=1 + XMP=6 归档方式不同则下游行为相反（§5.1）。这些是“下游避免二次旋转”的主要威胁。
6. §6 单独列出**无法仅从当前代码证实的保存后行为**（AVIF `irot/imir` 与 EXIF 的合成、libtiff 写路径的 EXIF IFD 重写、XMP 正则漏匹配后的语义等）作为证据缺口。

---

## 1. 转正前后：像素、尺寸、EXIF 与 XMP 的关系

### 1.1 映射表与像素语义

`ImageOps.exif_transpose` 把 8 个方位值映射到 `Image.Transpose` 枚举（`src/PIL/ImageOps.py:862-870`）：

| EXIF Orientation | Transpose 操作 | 尺寸是否交换 |
|---|---|---|
| 1 | （无操作） | 否 |
| 2 | `FLIP_LEFT_RIGHT` | 否 |
| 3 | `ROTATE_180` | 否 |
| 4 | `FLIP_TOP_BOTTOM` | 否 |
| 5 | `TRANSPOSE` | **是** |
| 6 | `ROTATE_270` | **是** |
| 7 | `TRANSVERSE` | **是** |
| 8 | `ROTATE_90` | **是** |

实验（3×2 图，仅 (0,0) 为白）确认 `exif_transpose` 的结果与直接调用同名 `transpose()` 逐字节相同。注意 6 对应 `ROTATE_270`、8 对应 `ROTATE_90`，与“顺时针 90°/270°”的直觉相反——6 表示“存储的上边缘位于显示方向的右侧”，需逆时针转 90° 才正。仓库测试 `Tests/test_file_tiff.py:665-682` 断言了同样的映射（orientation 5/6/7/8 ↔ TRANSPOSE/ROTATE_270/TRANSVERSE/ROTATE_90 且 size 为 `(64, 128)`）。

`method is None` 的情形（方位缺省、=1、或非法值如 0/9）**不转正、也不删除该标签**（`src/PIL/ImageOps.py:871,880`）。实测 `Orientation=1/0/9` 时返回图像的 EXIF 中仍保留原值。

### 1.2 尺寸（`Image.size`）在转正前后

- **返回新图路径**（默认 `in_place=False`）：`transpose()` 通过 `_new()` 用新核心图重建对象，`_size` 取自核心图尺寸（`src/PIL/Image.py:673-688, 3207-3219`），因此 `size` 自动是转正后的尺寸。
- **原地路径**（`in_place=True`）：`ImageOps.py:872-874` 显式 `image.im = image.im.transpose(method)` 后 `image._size = image.im.size`；这是全仓库唯一直接改写 `_size` 的地方，说明作者意识到该字段不会自动跟随。
- **`Exif` 内的像素尺寸标签不会随之更新**：`PixelXDimension(0xA002)`/`PixelYDimension(0xA003)` 保持原值。实测 4×2、orientation 6 → 输出 `size=(2,4)`，但 `PixelXDimension=4, PixelYDimension=2`。归档若依赖这两个标签校验尺寸，会与真实像素不符。

打开阶段的 `size` 语义按格式不同（见 §2.3），这是“转正前”观察到的 `size` 到底代表什么的关键。

### 1.3 EXIF `Orientation` 的清除

只有 `method is not None` 时才进入删除分支（`ImageOps.py:880-881`）。删除后按“元数据实际存放的位置”回写（`ImageOps.py:882-888`）：

1. 有 `info["exif"]` → 用 `exif.tobytes()` 覆盖；
2. 否则若有 `info["Raw profile type exif"]`（ImageMagick 风格十六进制文本）→ 重新生成该文本；
3. 两条都不满足（例如方位只存在于内存中的 `Exif` 对象、且不在 `info` 里）→ **只删除对象上的标签，不产生任何 `info` 项**。

实测：仅用 `im.getexif()[0x0112] = 6` 设置方位、`info` 中没有 `exif` 键时，转正后返回图 `getexif()` 为 `{}`，且 `info` 无 `exif`。这意味着若下游从别的副本（或同一张图的另一份原始字节）重新 `getexif()`，仍可能读到旧值（见 §5.5 的 `Raw profile` 复活问题）。

子 IFD（Exif IFD、GPS IFD）与 IFD1 缩略图**不在删除范围内**：实测 `FNumber`、GPS IFD、以及 IFD1 缩略图数据在 `tobytes()`/转正后保持原样（`Tests/test_file_jpeg.py:392-401` 也有 GPS IFD 保留断言）。**但缩略图本身不会被旋转**：实测用一张 `Orientation=6` 的主图 + 原缩略图，输出 `info["exif"]` 中缩略图字节完全不变（仍是 `160×120` 的原始朝向）。主图已转正而缩略图未转正，是“归档素材内部自相矛盾”的一个真实来源。

### 1.4 XMP `tiff:Orientation` 的读取（补充来源）

`Image.getexif()` 在 EXIF 完全不含 `Orientation` 时，用**正则**（非 XML 解析）从 `info` 提取 XMP 方位（`src/PIL/Image.py:1701-1710`）：

- 先看 `info["XML:com.adobe.xmp"]`（PNG iTXt 的原始关键字，`src/PIL/PngImagePlugin.py:703-704`），再用 `info["xmp"]`（JPEG APP1、WebP `XMP ` chunk、TIFF tag 700、AVIF 的 `xmp`）；
- 两个键都存在时**按此顺序取第一个能匹配的**：实测 `XML:com.adobe.xmp=8` + `xmp=6` → 取 8。

识别范围非常窄——只有两种固定写法：
- `tiff:Orientation="<digit>"`（**双引号、固定 `tiff:` 前缀、单字符数字**）
- `<tiff:Orientation><digit></tiff:Orientation>`

实测这些写法**读不到**：单引号 `tiff:Orientation='6'`、带空格 `tiff:Orientation = "6"`、其它命名空间前缀 `ns:Orientation="6"`、多位数 `"10"`（只读取首位 `1`）、`"08"`（读成 `0`）。读不到时该图被视为无方位信息（按 1 处理）。

### 1.5 转正时对 XMP 的清除（与读取的对称缺陷）

`ImageOps.py:889-900` 在删除 EXIF 标签的同一次调用中，遍历 `("XML:com.adobe.xmp", "xmp")` 两个键，用与读取端**同样的两条正则**做 `re.sub(..., "")`。由此产生一组不对称行为：

- 能被读取的写法 → 能被删除，一致；
- 读不到的写法（单引号、多位数等）→ 也不会被删除，**旧方位残留在 XMP 里**。

实测：单引号 XMP `Orientation='6'` 的 JPEG，EXIF 转正成功，但输出 `info["xmp"]` 仍含 `Orientation='6'`（§5.1）。

另外，删除是“文本删除”而非“属性删除”：实测 `tiff:Orientation="8" tiff:Other="1"` 被删成 ` tiff:Other="1"`，保留双空格且整体 XML 上下文被破坏；多位数 `"10"` 因只匹配单字符而完全保留。XMP 正则删除对 `XML:com.adobe.xmp` 与 `xmp` 两个键**各自独立执行**，实测两个键同时存在时都会被处理。

### 1.6 多来源冲突的结论与优先依据

**代码依据（`src/PIL/Image.py:1698-1711`）：**

```python
exif_info = self.info.get("exif")          # 1) info["exif"]
... # 否则 "Raw profile type exif"，再否则 TIFF tag_v2
if exif_info is not None:
    self._exif.load(exif_info)             # 2) 逐字节解析 EXIF
if ExifTags.Base.Orientation not in self._exif:   # 3) 仅当 EXIF 没有该标签
    xmp_tags = self.info.get("XML:com.adobe.xmp")  # 4) XMP 作为补充
    if not xmp_tags and (xmp_tags := self.info.get("xmp")): ...
    if match: self._exif[ExifTags.Base.Orientation] = int(match[2])
```

即**优先级：EXIF（含 Raw profile、TIFF tag_v2）> XMP**，且 XMP 只是“补缺”，**不会覆盖 EXIF**。实测矩阵：

| EXIF | XMP | `getexif()[Orientation]` | 结论 |
|---|---|---|---|
| 6 | 8 | **6** | EXIF 胜 |
| 1 | 6 | **1** | **EXIF=1 也阻断 XMP**（不表示“无方位”，而是“显式正向”） |
| 无 274 标签，但有其它 EXIF 标签 | 6 | **6** | XMP 补缺 |
| 9（非法） | 6 | **9** | EXIF 胜；9 不映射，故不转正、也不删标签 |
| 无 EXIF | 6 | 6 | 只用 XMP |
| — | `XML:com.adobe.xmp`=8 且 `xmp`=6 | **8** | 两个 XMP 键中 `XML:...` 优先 |

**对归档的直接含义：** 若一张图 EXIF 写着 `Orientation=1`（或某个非法值）而 XMP 写着 6，Pillow 会按 1 处理——**不旋转**。此时如果归档流水线“信任原图 XMP 已经转正过”，就会漏转；反之，若转发给会尊重 XMP 的第三方工具，则可能被转。这是“多来源不一致时结论必须有明确依据”的场景，**依据应是 Pillow 的实际取值，而不是任何单一来源**。

---

## 2. 原地处理 vs 返回新图：对原素材的影响

### 2.1 `in_place=False`（默认）：不改原对象，返回新对象

`ImageOps.py:875-877, 901-904`：

- 先 `image.load()`（`ImageOps.py:859`），再 `image.transpose(method)` 生成新图；
- 对**副本**执行“删 EXIF 方位 + 重写 `info["exif"]` / `Raw profile` / XMP”；
- 无转正需求时返回 `image.copy()`（`ImageOps.py:903-904`）。

实测（JPEG orientation 6）：调用后原图 `size` 与 `getexif()[Orientation]` 不变，`info["exif"]` 对象**同一性不变**（未被改写）；返回图 `Orientation` 已删除、其它 EXIF 标签（`Make`、`UserComment`）保留。

⚠️ **TIFF 例外：** 若原对象是 `TiffImageFile` 且尚未加载，`exif_transpose` 内部的 `image.load()` 会触发 `TiffImageFile.load_end()`（`TiffImagePlugin.py:1407-1422`），后者**就地**转正像素并删除 `tag_v2[274]`。因此对 TIFF 调用默认参数时，**传入对象本身已被改变**，只是返回值是一份额外的拷贝。实测：调用前 `size=(32,64)`、`274=6`；调用后原对象 `size=(32,64)`、`274=None`、核心图已是转正后的像素。这违反“不改原素材”的直觉，但对归档是“幸运”的——因为像素已经对了。

### 2.2 `in_place=True`：直接改写传入对象

`ImageOps.py:872-874` 修改 `image.im` 与 `image._size`，并就地删除 EXIF 方位、重写 `info`。返回 `None`。实测：JPEG 原对象 `size` 由 `(2,1)` 变 `(1,2)`，`Orientation` 删除，`info["exif"]` 被替换为重新序列化的新字节（实测 32 字节 → 20 字节）。

注意 `in_place=True` 在**不需要转正**的情况下（方位 1/0/9/缺省）**什么都不做**，且返回 `None`（`ImageOps.py:903-905` 的 `elif not in_place` 分支），所以调用方无法从返回值区分“转了”与“没转”。仓库测试 `Tests/test_imageops.py:609-618` 覆盖了基本行为但未覆盖“无方位时的返回值”。

### 2.3 打开阶段就已转正的情形（TIFF）

TIFF 有两处独立的方位处理：

1. **`_setup()` 交换 `size`**（`TiffImagePlugin.py:1555-1559`）：`Orientation ∈ {5,6,7,8}` 时 `self._size = (ysize, xsize)`，而 `_tile_size` 保持存储方向。因此 **TIFF 打开后 `size` 是显示方向**，与 JPEG/PNG（打开后 `size` 是存储方向）相反。实测：同一 64×32 源图加 `Orientation=6`，JPEG/PNG 打开 `size=(64,32)`，TIFF 打开 `size=(32,64)`。
2. **`load_end()` 原地转正**（`TiffImagePlugin.py:1420-1422`）：像素转正 + 删 `tag_v2[Orientation]`。因为 `load()` 期间的 `_size` 被临时换回 `_tile_size`（`TiffImagePlugin.py:1394-1404`），转正时 `_size` 会再次被设为转正后尺寸，加载完成后两者一致。

因此“对 TIFF 调 `exif_transpose` 究竟做了什么”取决于是否已加载：
- 未加载：会先加载（自动转正、删标签），`method` 算出 `None`，返回 `copy()`——**没有任何额外的转正动作**，这正是 TIFF 不会二次旋转的原因；
- 已加载：同样 `method is None`，返回 `copy()`。

### 2.4 重复转正：内存中的可观察结果

对**同一个对象**或**它返回的图**连续调用 `exif_transpose`：

- 只要第一次调用成功删除了方位，第二次的 `method` 为 `None`，返回 `copy()`——像素不再变化。实测 JPEG/PNG/TIFF 均如此；仓库亦有断言（`Tests/test_imageops.py:544-546`）。
- 对“原图”重复调用（JPEG/PNG）会得到两个**独立的转正副本**，内容相同（受 JPEG 重编码影响则需按相似度比较）。
- 对 TIFF 原图重复调用不会再次转正（`tag_v2` 已删）。

### 2.5 重新读取后的可观察结果

从磁盘重开一张**已转正且方位已删**的文件：`getexif()[Orientation]` 不存在，`size` 为像素尺寸，再次 `exif_transpose` 为无操作。这是理想的“可重复处理”状态。

但“重新读取”能否成立完全取决于**保存时写入了什么**（§3）。以下几种情况会破坏它：

- 归档用**原始 EXIF 字节**（含旧方位）→ 重开后方位复活 → 二次旋转（§5.2 实测量化）。
- 归档用 `tiffinfo=im.tag_v2`（转正后 `tag_v2` 的 256/257 仍是旧像素尺寸）→ 重开后标签尺寸与像素不符，图被拉成错误几何（§5.3）。
- 归档时方位来源被“换掉”：归档丢弃 EXIF、只保留未被清除的 XMP 方位（例如原 EXIF=1 + XMP=6 的情形）→ 重开后 EXIF 无方位、XMP 有方位 → 下游按 XMP 转正，方向与归档前相反（§5.1 模式 A）。
- 重新读取 TIFF 时，若 `tag_v2` 仍残留 tag 700 的旧 XMP（未删除）——而 `info["xmp"]` 已被清空——**同一文件两次 `load()` 可能给出不同结果**（§5.4）。

---

## 3. 保存路径：元数据不会自动继承

`Image.save()` 把 `params` 存为 `_default_encoderinfo`（`src/PIL/Image.py:2749, 2793-2796`）并合并 `encoderinfo`，各插件只读取 **`encoderinfo` 中显式给出**的键：

| 格式 | EXIF 写入条件 | XMP 写入条件 |
|---|---|---|
| JPEG | `info.get("exif", b"")`（`JpegImagePlugin.py:806`） | `info.get("xmp")`（`JpegImagePlugin.py:765`） |
| PNG | `encoderinfo.get("exif")`（`PngImagePlugin.py:1560`） | **无 `xmp=` 参数**；只能经 `pnginfo=` 的 iTXt 块写入（实测 `save(xmp=...)` 被忽略） |
| TIFF | `encoderinfo["exif"]` 或 `tiffinfo`（`TiffImagePlugin.py:1849`）；另会在 `tiffinfo` 缺失时从 `im.tag_v2` 拷贝 `XMP(700)` 等（`TiffImagePlugin.py:1899-1916`） | 同类路径，tag 700 |
| WebP | `encoderinfo.get("exif")`（`WebPImagePlugin.py:225,314`） | `encoderinfo.get("xmp")`（同处） |
| AVIF | `info.get("exif")`（`AvifImagePlugin.py:190`） | `info.get("xmp")`（`AvifImagePlugin.py:202`） |

关键实测：一张带 `exif`（`Orientation=6`）与 `xmp` 的 JPEG，**不传参数**另存后，新文件 `info` 中既无 `exif` 也无 `xmp`（`[8]` 实验）。TiffImagePlugin 是唯一会从 `im.tag_v2` 隐式继承少量标签（含 tag 700）的路径（`TiffImagePlugin.py:1899-1916`），这既是“TIFF 默认能保住 XMP”的原因，也是 §5.4 二次旋转的来源。

因此归档的正确写法是**显式写入转正后图上的元数据**：

```python
corrected = ImageOps.exif_transpose(im)
corrected.save(dst, exif=corrected.info.get("exif", b""))   # JPEG/PNG/WebP/AVIF
# PNG 的 XMP 需经 pnginfo 重新写入 info["XML:com.adobe.xmp"]
# TIFF 需注意不要传 tiffinfo=im.tag_v2（见 §5.3）
```

---

## 4. 归档流水线的推荐做法（基于代码与实测）

1. **只调用一次** `ImageOps.exif_transpose()`，之后**以返回图为准**，不要再用原图（TIFF 的原图已被就地转正，JPEG 的原图未转正，二者语义不同）。
2. **归档前显式检查四项一致性**：`getexif().get(0x0112)` 不存在、`info` 中所有 XMP 键都不含 `tiff:Orientation`、`size` 与像素核心尺寸一致、`info["exif"]` 中 `PixelXDimension/PixelYDimension` 与 `size` 一致。
3. **不要把原始 EXIF/XMP 传给 `save()`**；传转正图上的 `info["exif"]`（已被重写）与已剥离方位的 XMP。
4. **TIFF 归档不要传 `tiffinfo=dict(im.tag_v2)`**：转正后 `tag_v2` 的 256/257 仍是旧尺寸。要么不传，要么显式用核心图尺寸覆盖。
5. **下游二次处理前先判断“是否已转正”**：可靠信号是 `Orientation` 缺失 + 无 XMP 方位；不可靠信号是“文件里没有 `exif` 键”（可能是 §3 的继承缺失，也可能是转正成功）。

---

## 5. 已实测的二次旋转/几何错乱场景

以下均在编译后的仓库代码上复现（脚本见 `/tmp/probe/verify.py`、`verify2.py`）。

### 5.1 XMP 方位残留 → 归档后方位来源被替换
XMP 的处理有**两种不同的失败模式**，对应不同后果，必须分开看：

**模式 A（Pillow 自身会二次旋转）：EXIF 里有可读方位、但转正未发生，XMP 也没被清除。**
- 触发条件：`EXIF Orientation=1`（或非法值）+ XMP `tiff:Orientation="6"`（标准双引号写法）。
- 过程：`getexif()` 因 EXIF 已有 274（值为 1）而**不读 XMP**（`Image.py:1702`）；`exif_transpose` 的 `method is None`，于是**既不转正、也不进入删除分支**（`ImageOps.py:871,880`）——XMP 中的 6 原样保留。
- 归档若**丢掉 EXIF**（只写 XMP）：重开的文件 EXIF 无 274 → 此时 `getexif()` 从 XMP 读到 6（`Image.py:1702-1710`）→ `exif_transpose` 转正。实测 `C_drop`：归档前 `size=(64,32)`，重开后 `getexif()[274]=6`、下游转正得 `(32,64)`——**像素方向被改变**。
- 归档若**保留 EXIF**：重开仍读到 1，下游无操作（`C_keep`）。可见**同一张转正结果，归档方式不同会导致下游行为相反**。

**模式 B（Pillow 自身不旋转，但违反“无方位”约定）：写法不被识别，读不到也删不掉。**
- 触发条件：EXIF `Orientation=6` + XMP 用单引号 `tiff:Orientation='6'`、带空格、非 `tiff:` 前缀或多位数等写法。
- 过程：EXIF 转正成功并删除 EXIF 274，但 XMP 正则（`ImageOps.py:891-893`）只认双引号与元素式，**旧方位残留在 XMP**。
- 重开：`getexif()[274]` 为 `None`（EXIF 已删），`info["xmp"]` 仍有 `Orientation='6'`。**Pillow 自己也读不到单引号写法**（`Image.py:1704` 的正则同样只认双引号），故 `ImageOps.exif_transpose` 对它是无操作（实测 `B_arch` 下游 `size` 不变）。
- 风险不在 Pillow，而在**第三方按标准 XML/XMP 解析器读取**（它们能识别单引号属性）时会把该文件当作“方位=6”再转一次。归档素材因此**不是“无方位”的干净状态**，违反 §7 的判定前提。

两种模式的共同结论：**只看“EXIF 里没有 274”不足以断定“已转正”**；必须同时确认所有 XMP 键都不含 `tiff:Orientation` 的**任意写法**。

### 5.2 归档沿用原始 EXIF 字节 → 方位复活
- 源：`orientation_rectangle.jpg`（2×1，`Orientation=8`）。
- 转正后 `(1,2)`；把**原始** `im.info["exif"]` 作为 `exif=` 写出。
- 重开：`Orientation=8`，尺寸 `(1,2)`；下游 `exif_transpose` 输出 `(2,1)`——**几何被再次旋转**。而不传 `exif` 的归档文件重开后 `Orientation=None`、下游无操作。两者可直接对比（`[9]` 实验）。
- 对非方图，这会直接改变宽高比，是“二次旋转”最直观的破坏。

### 5.3 TIFF 用 `tiffinfo=im.tag_v2` 归档 → 标签尺寸与像素不符
- 源：TIFF `Orientation=6`，加载后 `size=(32,64)`（已转正），但 `tag_v2[256]=64, tag_v2[257]=32` 仍是存储方向。
- 归档：`save(..., tiffinfo=dict(im.tag_v2))`。因为 `ifd[IMAGEWIDTH]=im.size[0]` 在此仅作为默认值（`TiffImagePlugin.py:1841`），传入的 `tiffinfo` 会覆盖它。
- 重开：标签声明 `64×32`，而像素缓冲是 `32×64`；读取时 `size` 报 `(64,32)`，核心图 `(32,64)`，像素内容被错误解读（`[11]` 实验；`v_g_bad` 标签 `64x32` vs `v_g_ok` `32x64`）。

### 5.4 TIFF 的 XMP-only 方位 → 尺寸/像素不一致 + 归档后双转
- 源：TIFF 只有 tag 700 的 XMP `tiff:Orientation="6"`，无 EXIF 274。
- 打开：`getexif()[274]=6`（来自 XMP），但 `_setup` 用 `tag_v2.get(274)` 算尺寸（`TiffImagePlugin.py:1555`），**没有看到 XMP**，故 `size` 不交换。
- 加载：`exif_transpose(in_place=True)` 转正像素并删除 EXIF 274（本就来自 XMP），随后按正则把 `Orientation` 文本从 `info["xmp"]` 中删掉；但 **`tag_v2[700]` 的原始 XMP 字节无人修改**。
- 结果：加载后 `size=(64,32)`（旧值）、核心图 `(32,64)`（已转正）——**对象自相矛盾**。
- 归档：默认保存会从 `im.tag_v2` 继承 tag 700（`TiffImagePlugin.py:1899-1916`），但尺寸按 `im.size` 写成 `64×32`。
- 重开：`getexif()[274]` 又从 tag 700 读到 6，`load()` 再转一次 → **像素被二次旋转**。实测：首读像素与正确结果差异 `0.0`，归档后重读的像素与正确结果差异 `63.52`（`verify2.py` A 段）。
- 同一问题在**多帧 TIFF `seek()`** 时也会出现：`_seek()` 每次都从 `tag_v2` 重建 `info["xmp"]`（`TiffImagePlugin.py:1349-1355`），已在内存中清空的 XMP 方位会被重新装回（实测 `seek(0)` 后 `getexif()[274]` 从 `None` 回到 6）。

### 5.5 `Raw profile type exif` 与 `info["exif"]` 并存 → 方位复活
- `exif_transpose` 的删除采用 `if "exif" ... elif "Raw profile ..."`（`ImageOps.py:882-888`），**只改一个**。
- 实测：两者都含 `Orientation=6` 时，转正后主 `exif` 已删，但 `Raw profile` 仍是旧值；一旦下游删掉 `info["exif"]`（或经由只带 Raw profile 的路径重建 Exif），`getexif()[274]` 又变回 6（`[14]` 实验）。

### 5.6 TIFF 的 EXIF=1 + XMP=6 → 两次读取不一致
- 首次读取：EXIF 274=1 阻断 XMP，不转正；像素=存储方向（`read1 vs raw 0.0`）。
- `load_end` 删除了 `tag_v2[274]`（值为 1），但 tag 700 的 XMP 仍在。
- 归档后重读：EXIF 无 274 → XMP 补缺得 6 → 自动转正；像素=显示方向（`read2 vs rot270 0.0`）。
- 两次读取的像素方向不同（`read1 vs read2` 尺寸 `(64,32)` vs `(32,64)`）。**“同一条流水线两次归档结果不一致”**。

### 5.7 无方位时 `in_place=True` 无可观察反馈
方位 1/0/9/缺省时 `exif_transpose(in_place=True)` 返回 `None` 且不改动任何东西；调用方无法据此判断“是否需要转正”。若归档逻辑以“返回值/尺寸变化”作为“已处理”标记，会误判。

---

## 6. 证据缺口（无法仅从当前代码证实）

以下结论**不能**由本仓库源码或本次实验直接证实，下游不应把它们当作既定事实：

1. **AVIF 的 `irot/imir` 与 EXIF 合成后的最终像素方向。** `_avif.c` 中 `AvifDecoder.get_info()` 通过 `irot_imir_to_exif_orientation()` 把容器变换折算成 EXIF 方位（`src/_avif.c:50-86, 761`），保存时 `exif_orientation_to_irot_imir()` 反向写回（`src/_avif.c:95-135, 404-406`）。但**解码出的像素是否已经应用了 `irot/imir`** 取决于 libavif 的默认行为（是否设置 `ignoreTransform`），代码中未见显式设置（`_avif.c:650-679` 只改了 `strictFlags` 与线程数），本次构建也未启用 AVIF。若 libavif 已经应用了变换，则 `getexif()[274]` 与像素可能“双重表达”同一方位，`exif_transpose` 的结果需要单独验证。
2. **libtiff 写路径对 EXIF IFD 的重写。** `TiffImagePlugin.py:1849-1862` 把 `encoderinfo["exif"]` 的键合并进 IFD，但 libtiff 后端（`TiffImagePlugin.py:2005-2095`）会对 `IMAGEWIDTH/IMAGELENGTH` 做特殊处理（`TiffImagePlugin.py:1864`，以及 13.1.0 发行说明所述“总是取自图像”）。本机构建无 libtiff，未能验证 libtiff 路径下 EXIF 子 IFD 与 `Orientation` 的最终落盘形态。
3. **WebP 保存路径是否会在 EXIF chunk 中保留/重写 `Orientation`。** `WebPImagePlugin.py:225` 直接透传 `exif`（若是 `Image.Exif` 则 `tobytes()`），C 层 `WebPMuxSetChunk("EXIF", ...)`（`src/_webp.c:376-380, 721-724`）原样写入；但 WebP 是否允许“EXIF 已删、像素已转”的文件被其他解码器再次按 EXIF 旋转，取决于外部解码器实现。本机构建无 WebP。
4. **XMP 正则漏匹配后的语义。** 代码只说明“匹配不到就不读/不删”（`Image.py:1704-1710`、`ImageOps.py:891-900`），但**不保证**这些文件在第三方工具中被视为“无方位”。真实相机/编辑软件写出的 XMP 是否总有双引号与 `tiff:` 前缀，需要样本统计，不在本仓库可证实范围。
5. **`save()` 后 `info` 的实际落盘与第三方读取的一致性。** 本仓库只能证实“Pillow 自己重开时读到什么”。其他软件是否会因为 `PixelXDimension` 未更新（§1.2）、缩略图未旋转（§1.3）、或 XMP 被正则破坏（§1.5）而做不同处理，属于外部行为。
6. **`Exif.tobytes()` 对 `_ifds`（子 IFD）的完整保留。** 实测 GPS IFD 与 IFD1 在常见样本上保留，但 `tobytes()` 只对 `Exif`/`GPSInfo` 两组的 `_ifds` 做显式处理（`src/PIL/Image.py:4300-4318`），对 Interop、MakerNote 等嵌套 IFD 的序列化完整性未全面验证。

---

## 7. 下游避免二次旋转的判定清单

下游拿到的归档文件，按以下顺序判断“是否还需要转正”：

1. `im.getexif().get(0x0112)` 存在且 ∈ {2..8} → **需要**转正（且注意 TIFF 可能已在 `load()` 中转过，见下一条）。
2. 若是 `TiffImageFile`：在 `load()` 之前判断；`load()` 会自动转正并删标签，**不要**在 `load()` 之后再手动 `rotate`。
3. `getexif()[0x0112]` 不存在时，**不要只看 EXIF**：检查 `info["xmp"]` 与 `info["XML:com.adobe.xmp"]` 是否仍含 `tiff:Orientation`（所有写法），有则视为“未完成转正”并单独处理（§5.1）。
4. 校验几何（**先 `load()`**，因为 `im.im` 在未加载时不可访问）：`im.size == im.im.size`；TIFF 的 XMP-only 场景下二者会不等（§5.4），据此可发现自相矛盾的归档文件。另核对 `PixelXDimension/PixelYDimension` 与 `size`（§1.2）。
5. 只有当“EXIF 无方位 + 所有 XMP 键无方位 + `size` 与像素尺寸自洽”三条同时成立时，才把文件当作“已转正、可重复处理”的素材，下游**不得再次旋转**。任一条不成立，都应先查明原因再处理，而不是套用默认转正逻辑。

---

## 附：本次分析的关键代码位置

| 主题 | 位置 |
|---|---|
| 转正主逻辑/映射/原地分支 | `src/PIL/ImageOps.py:847-905` |
| EXIF 方位读取优先级 + XMP 补缺正则 | `src/PIL/Image.py:1671-1712` |
| `_reload_exif` | `src/PIL/Image.py:1714-1718` |
| `Exif.tobytes`（子 IFD 写出） | `src/PIL/Image.py:4297-4319` |
| `save()` 的 encoderinfo 组装 | `src/PIL/Image.py:2748-2752, 2793-2796` |
| TIFF 打开时交换 size | `src/PIL/TiffImagePlugin.py:1553-1559` |
| TIFF `load` 的尺寸临时切换 | `src/PIL/TiffImagePlugin.py:1390-1405` |
| TIFF `load_end` 自动转正 | `src/PIL/TiffImagePlugin.py:1407-1422` |
| TIFF `seek` 重建 `info["xmp"]` | `src/PIL/TiffImagePlugin.py:1349-1356` |
| TIFF 保存尺寸/tiffinfo 覆盖 | `src/PIL/TiffImagePlugin.py:1841-1842, 1864, 1899-1916` |
| JPEG EXIF/XMP 读取 | `src/PIL/JpegImagePlugin.py:111-119` |
| JPEG EXIF/XMP 写入 | `src/PIL/JpegImagePlugin.py:765-772, 806-809` |
| PNG XMP 读入 | `src/PIL/PngImagePlugin.py:703-704` |
| PNG eXIf 写出 | `src/PIL/PngImagePlugin.py:1560-1565` |
| WebP exif/xmp | `src/PIL/WebPImagePlugin.py:93-94, 225-228, 314-319` |
| AVIF 方位↔irot/imir | `src/_avif.c:50-135, 404-406, 761`; `src/PIL/AvifImagePlugin.py:100-111, 189-202` |
| 官方幂等性测试 | `Tests/test_imageops.py:518-618` |
| TIFF 方位映射测试 | `Tests/test_file_tiff.py:665-682` |
