# QuinePack

把本地文件或目录树转换为「自含压缩包」：解压后会得到原始载荷，以及归档自身的一份逐字节副本。

英文名 **Quine Archive** 指这种“解压结果包含压缩包自身副本”的归档。

> **English summary** — QuinePack builds self-containing (“quine”) archives from local files or directory trees. Extracting one yields the original payload plus a byte-for-byte copy of the archive itself. Three formats are available: compressed `7z`, compatibility `7z-store`, and `txz`. Runtime needs only Python 3.11+ and no third-party libraries; output is deterministic and can be verified with the official extractors.

## 工作原理

一个自含压缩包解压后包含三类内容：

- **载荷（Payload）**：用户提供的原始文件或目录树，原样铺在解压根目录。
- **自含副本（Self Copy）**：与原产物逐字节相同、文件名也相同的文件。
- **种子（Seed）**：`.quinepack/<产物名>.seed`，解压时先输出，供后续压缩指令回引归档自身的头部与压缩流。它是实现自含的脚手架，不属于载荷，解压后可以直接删除。

```text
site/
├── index.html
├── assets/...
├── site.7z                     # 自含副本，与产物逐字节相同
└── .quinepack/
    └── site.7z.seed            # 种子 / 引导副本
```

## 快速开始

### 环境要求

- Python 3.11 或更高版本。
- 运行时无第三方依赖。
- 生成 `7z` / `7z-store` 不需要 7-Zip；校验它们需要官方 7-Zip（`7z`、`7zr`、标准 Windows 安装目录，或 `--7zr PATH`）。
- `txz` 的生成与校验都不需要 7-Zip。
- 单次载荷上限 1 GiB（ADR-0003）。

### 安装

```bash
pip install -e .
```

安装后可直接使用 `quinepack` 命令，也可以继续用 `python -m quinepack`。

### 最小示例

```bash
# 生成压缩版，默认输出到输入旁边
quinepack pack site/ --format 7z

# 生成并立即校验
quinepack verify site.7z --against site/
```

## 产物格式

| `--format` | 产物名 | 压缩方式 | 目标解压器 | 体积预期 |
|---|---|---|---|---|
| `7z` | `site.7z` | 双层 LZMA2 | 官方 7-Zip（`7z` / `7zr`） | 接近内容压缩后体积 |
| `7z-store` | `site.store.7z` | 单层 LZMA2 store | 官方 7-Zip；单 coder 期望可被 libarchive / Win11 资源管理器打开 | ≈ 原始载荷 |
| `txz` | `site.tar.xz` | store-only 单 LZMA2 流 | `xz` / `tar` / BusyBox / Python `lzma` | ≈ 原始载荷 |

`--format all` 等价于同时请求三种产物。`7z` 是压缩版；`7z-store` 与 `txz` 是兼容版，不承诺体积收益。

产物命名规则：

| 输入 | `7z` | `7z-store` | `txz` |
|---|---|---|---|
| 目录 `site/` | `site.7z` | `site.store.7z` | `site.tar.xz` |
| 单文件 `app.exe` | `app.exe.7z` | `app.exe.store.7z` | `app.exe.tar.xz` |

单文件载荷保留原扩展名，避免 `app.exe` 与 `app.zip` 撞名。默认输出到输入的同级目录。

## 用法

### 生成归档

一次生成三种产物并校验：

```bash
quinepack pack site/ \
  --format all \
  --outdir dist/ \
  --7zr "/path/to/7za" \
  --verify
```

只生成 txz 并校验（不需要 7-Zip）：

```bash
quinepack pack site/ \
  --format txz \
  --outdir dist/ \
  --verify
```

自定义产物文件名（只能配合单一格式）：

```bash
quinepack pack site/ \
  --format txz \
  -o dist/custom-name.tar.xz
```

### `pack` 参数

- `input`：载荷文件或目录。
- `--format`：`7z`、`7z-store`、`txz`，或逗号分隔组合；`all` 表示三种全部生成，不能与其他值组合。
- `-o, --output`：指定单个输出文件的完整路径，只能配合单一格式，不能与 `--outdir` 同用；父目录会自动创建。
- `--outdir`：指定输出目录，文件名仍按输入名推导。
- `--preset`：7z 压缩等级 0–9，默认 6；只对 `7z` 生效，与 `7z-store` / `txz` 同用时给出警告。
- `--exclude`：按相对路径 glob 排除内容，可重复使用；命中目录时整棵剪枝。
- `--force`：覆盖已存在的输出。
- `--verify`：写入后立即校验；7z 变体需要外部 7-Zip。
- `--7zr PATH`：显式指定 7-Zip 可执行文件；未指定时依次在 `PATH` 中查找 `7zr`、`7z`、`7z.exe`、`7zr.exe`，Windows 上再回退到标准安装目录。
- `--quiet` / `--verbose`：减少或增加输出，两者互斥。
- 非交互环境（非 TTY）必须显式传 `--format`。

### 交互式菜单

未传 `--format` 且 stdin/stdout 都是 TTY 时，会进入菜单：

```text
QuinePack: site (12.3 MiB)
Select output format(s):
  1) 7z        compressed, requires 7-Zip        [default]
  2) 7z-store  compatibility, not compressed
  3) tar.xz    Linux/BusyBox, not compressed
  a) all

>
```

直接回车等于 `1`；`1,3` 表示多选；`a` 表示全部。非法输入重新提示，Ctrl-C 退出码 130。

### 校验归档

```bash
quinepack verify dist/site.7z --against site/ --7zr "/path/to/7za"
quinepack verify dist/site.tar.xz --against site/
```

`verify` 始终检查归档可解、自含副本存在且 SHA-256 等于归档文件本身；`--against` 额外把解压出的载荷与原始载荷逐文件比对。

### `verify` 参数

- `archive`：要校验的归档文件。
- `--against PATH`：同时把解压结果与原始载荷目录逐文件比对。
- `--exclude GLOB`：比对时按相对路径排除内容，可重复使用。
- `--7zr PATH`：校验 7z / 7z-store 时需要的 7-Zip 可执行文件；txz 不需要。
- `--quiet` / `--verbose`：减少或增加输出，两者互斥。

### 如何选格式

- 需要体积收益：用 `7z`。
- 条目数很多、又需要单 coder 的 7z 兼容性：用 `7z-store`（large-footer 路径已验证 2000 / 5000 个小文件）。
- 不想依赖 7-Zip，或面向 Linux / BusyBox：用 `txz`。

查看版本：

```bash
quinepack version
```

## 输出布局

`--format all` 会生成：

```text
dist/
├── site.7z
├── site.store.7z
└── site.tar.xz
```

解压任一产物后，目录中同时包含载荷与自含副本（见「工作原理」）。`7z-store` 与 `txz` 的目录结构相同，只是自含副本和种子文件名分别对应 `site.store.7z`、`site.tar.xz`。

产物名同时决定自含副本与种子名：自含副本与产物同名，种子为 `.quinepack/<产物名>.seed`。使用 `-o` 自定义文件名时建议保留对应后缀（`.7z`、`.store.7z`、`.tar.xz`），否则 `verify` 与外部工具可能无法仅凭名称识别格式。一次生成多种格式时不能分别指定文件名；需要多次运行 `pack`，每次用 `-o`。

`tar.xz` 在成员名无法放入 ustar 字段或包含非 ASCII 字符时，会在该成员前写入 PAX 扩展头，因此长载荷路径、Unicode 文件名、长产物名与种子名均可经 GNU `tar`、BusyBox `tar` 与 Python `tarfile` 往返。

## 行为细节

- **确定性**：相同载荷字节、mtime 与参数下产物逐字节一致；不写生成时间，不使用随机数，压缩参数固定（ADR-0005）。
- **扫描**：目录按相对路径的 POSIX 形式稳定排序；默认包含隐藏文件（含 `.git`、`.env`）；跟随符号链接并检测环路；保留空目录与 mtime。
- **上限**：扫描阶段统计总字节数，超过 1 GiB 直接拒绝（退出码 4）。
- **布局保护**：载荷根目录已存在 `.quinepack/`、载荷中已有与产物同名的文件、或输出路径落在载荷目录内部时，生成前直接报错。
- **大小写冲突**：大小写不敏感的文件系统上出现仅大小写不同的路径时给出警告，不自动改名。
- **覆盖与原子写入**：目标已存在且是 TTY 时逐文件询问；非 TTY 下报错并以退出码 5 结束，除非传 `--force`。产物先写 `<output>.partial`，成功后原子替换，失败或中断时清理 partial。
- **进度**：阶段信息输出到 stderr（扫描、压缩、布局、CRC 定点、写入、校验），`--quiet` 只保留错误。
- **大载荷**：扫描内容、压缩流与 tar 前缀会落到当前目录的 `.quinepack-spool-*` 临时文件并通过 `mmap` 访问，构建结束或失败后清理；单格式打包会在内容不再需要时主动释放映射。

## 性能

Windows、Python 3.12、不可压缩随机载荷实测的峰值工作集与耗时：

| 载荷 | `7z` | `7z-store` | `txz` |
|---|---:|---:|---:|
| 64 MiB | 665 MiB / 33 s | 153 MiB / 3 s | 344 MiB / 7 s |
| 256 MiB | 698 MiB / 158 s | 548 MiB / 12 s | 1310 MiB / 23 s |
| 1 GiB | 2116 MiB / 661 s | 2119 MiB / 47 s | 5171 MiB / 90 s |

每行由 `tools/bench_memory.py` 测得；1 GiB 随机载荷满足 ADR-0007 的 ≤ 8 GB 目标。

`7z-store` 与 `txz` 的耗时来自 ADR-0008 的子进程并行编码，再一次由 ADR-0009 的解析 CRC 列向量削掉整份归档的重扫；`7z` 压缩版的剩余时间有 87% 是 `lzma.LZMACompressor` 的单线程 C 编码（64 MiB 载荷 33.4 s 中 29.1 s），标准库没有多线程 LZMA2 编码器。`txz` 的峰值工作集随并行 CRC64 上升（64 MiB 279 → 344 MiB，256 MiB 1051 → 1310 MiB，1 GiB 4142 → 5171 MiB），来自共享内存分片副本。

改造前的基线：64 MiB `7z` 44.3 s / `7z-store` 19.4 s / `txz` 45.0 s，256 MiB 224 s / 105 s / 171 s，1 GiB 923 s / 411 s / 693 s。

> 旧的 `7z-store` 144.8 s / `txz` 688.2 s 属于修复前产物不可校验的测量，不能作为当前实现的性能承诺。

### 并行范围

- `7z-store` 的 match chunk 编码：载荷 ≥ 8 MiB 时按计划分片交给工作进程。
- `7z` 双层布局的 LZMA 块编码：`_build_streams` / `_build_mseg` 先规划再批量编码，载荷 ≥ 4 MiB 时并行。
- `txz` 的 CRC64：待哈希流 ≥ 32 MiB 时按共享内存分片并行，父进程用 GF(2) 多项式合并。
- 多格式：`--format all`（或任意多格式）在载荷 ≥ 8 MiB 时为每个格式起一个子进程，按估计峰值内存（`7z` / `7z-store` 2.2×、`txz` 5.1× 载荷）和核数调度，默认预算 8192 MiB。
- 这些分片都只做纯函数切分并按提交顺序回收，产物逐字节不变。

环境变量：

- `QUINEPACK_NO_MP=1` 关闭全部工作进程，退回进程内实现（受限环境或调试）。
- `QUINEPACK_WORKERS=n` 限制每棵进程树的工作进程数（多格式扇出会自动用它分配核数）。
- `QUINEPACK_MEMORY_BUDGET_MIB=n` 改多格式扇出的内存预算，默认 8192。

### 1 GiB 验收（当前提交）

2026-10-28 在当前 `7z-store` / `txz` 布局上重建并验收 1 GiB 载荷：

| 格式 | 产物大小 | 官方校验 | 自含副本 SHA-256 |
|---|---:|---|---|
| `7z-store` | 1,077,172,763 B | `7z t` → `Everything is Ok` | `75e7c9cf8a5edfc94bc6dcaf4f74cfaa0d395aad289ada2d5580cd8c8fdaca60` |
| `txz` | 1,077,176,176 B | `xz -t` 通过 | `71a03875b6b8f970f040bd98917276a52e5f5053a61366e34c67f7b724286beb` |

两种自含副本均与产物逐字节一致。1 GiB 外部 `tar -xJf` 在 Windows 样本上出现 `tar`/`xz` 管道悬挂，因此 `txz` 的自含副本改用 Python `tarfile` 读取校验；归档本身已通过 `xz -t`。

2026-10-29 的 ADR-0008 / ADR-0009 复测（载荷名 `random-1024.bin`）：

| 格式 | 产物大小 | 耗时 | 官方校验 |
|---|---:|---:|---|
| `7z` | 1,078,228,942 B | 661 s | `7z t` → `Everything is Ok` |
| `7z-store` | 1,077,172,843 B | 47 s | `7z t` → `Everything is Ok` |
| `txz` | 1,077,176,176 B | 90 s | `xz -t` 通过 |

三个产物的自含副本都用 `python -m quinepack verify` 校验通过。同一载荷下 64 MiB 三种产物的 SHA-256 与并行改造前完全一致，说明布局字节未变；1 GiB 的 `--format all` 扇出构建与单格式构建也逐字节相同（墙钟 686 s，串行合计 797 s）。

### 多文件条目验收

`7z-store` 的 large-footer 路径（header 超过 64 KiB）已用 1 字节小文件验收：

| 条目数 | 产物大小 | 官方校验 | 自含副本 |
|---:|---:|---|---|
| 2,000 | 159,599 B | `7z t` 通过 | `7z x` 后 SHA-256 与产物一致 |
| 5,000 | 397,747 B | `7z t` 通过 | `7z x` 后 SHA-256 与产物一致 |

## 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 成功 |
| 1 | 未预期的内部错误 |
| 2 | 命令行参数错误 |
| 3 | 归档校验失败 |
| 4 | 载荷缺失、不支持或超过大小限制 |
| 5 | 输出已存在且当前环境不能交互确认 |
| 6 | 校验需要 7-Zip，但未找到可执行文件 |
| 130 | 用户中断（Ctrl-C） |

## 开发

```bash
python -m pytest -q
python -m compileall -q quinepack tests
python tools/bench_memory.py --size 256 --format 7z
```

`tools/bench_memory.py` 还支持 `--format 7z-store|txz`、`--verify`、`--7zr PATH` 与 `--workdir DIR`（默认 `_tmp/bench`，已被 git 忽略）。

当前测试覆盖：

- 7z / 7z-store / txz 的生成与校验。
- 空文件、空目录、Unicode 文件名、ustar prefix 与 PAX 长名称。
- 自含副本 SHA-256、CRC64 fixpoint、确定性重复构建。
- 路径穿越、符号链接、硬链接、设备/FIFO 等不安全 tar 成员。
- `7z-store` large-footer：2000 / 5000 个小文件构建、官方 `7z t`、解压自含副本 SHA-256 均通过。
- `--format all`、`-o` 自定义文件名、非 TTY 保护与 `.partial` 原子写入。

测试套件会从 `PATH` 或标准 7-Zip 安装目录查找 `7z` / `7zr` 运行官方验收用例；找不到时跳过并给出提示。仅当 `xz` 与 `tar` 都缺失时，才跳过对应验收用例。

## 已知限制

- 仍不是完整流式实现：归档 `F` 仍整份驻留；压缩版 seed 在单格式 `release_payload` 路径上已改为虚拟种子。临时文件位于当前工作目录，构建失败时也会尽力清理。
- 需要 PAX 扩展头、但名称中含换行等 PAX 不支持的字符时，返回载荷错误（退出码 4），不会截断或静默改名。
- `7z-store` 的 large-footer 路径会在 header 超过单个 64 KiB store chunk 时，把 footer 镜像放进 seed 并用 match 输出；代价是 seed 与归档约为 `2 × header + 载荷`，极大条目数仍受内存与 LZMA2 字典距离约束。
- 1 GiB 随机载荷 `7z` 实测约 661 s、峰值约 2.1 GB，其中 87% 是单线程 liblzma 编码；100 MiB 级常规载荷的回归测试尚未系统覆盖。
- 大文件布局回归已覆盖 42 MiB `txz`、48 MiB `7z-store` 与 1 GiB 两格式（官方 `xz -t` / `7z t`，自含副本 SHA-256 一致）；生产使用前请按目标尺寸做性能验收。
- Windows 上纯 Python CRC64 仍慢于 C 实现；ADR-0008 后它按共享内存分片并行，但 `QUINEPACK_NO_MP=1` 或小于 32 MiB 的流仍走单核。

尚未实现：

- 压缩版 tar.xz（ADR-0002 限定为 store-only）。
- 超过 1 GiB 的载荷（ADR-0003）。
- ZIP 变体（ADR-0006）。
- `path` 之外的 PAX 记录（mtime 纳秒、超大文件大小、xattr 均不携带）。

## 文档

- [产品规格](docs/product-spec.md)：产物矩阵、CLI 行为、验收标准。
- [ADR-0001](docs/adr/0001-dual-format-output.md)：同时产出 7z 与 tar.xz。
- [ADR-0002](docs/adr/0002-txz-is-compatibility-only.md)：tar.xz 定位为兼容版。
- [ADR-0003](docs/adr/0003-payload-cap-1gib.md)：V1 载荷上限 1 GiB。
- [ADR-0004](docs/adr/0004-python-stdlib.md)：Python 3.11+ 与标准库实现。
- [ADR-0005](docs/adr/0005-deterministic-output.md)：产物逐字节可复现。
- [ADR-0006](docs/adr/0006-no-zip.md)：ZIP 不纳入产物格式。
- [ADR-0007](docs/adr/0007-1gib-prototype-baseline.md)：1 GiB 原型基线与内存门槛。
- [ADR-0008](docs/adr/0008-parallel-encoders.md)：热点编码子进程并行，产物字节不变。
- [ADR-0009](docs/adr/0009-analytic-crc32-columns.md)：CRC 定点改用解析列向量，不再逐位重扫归档。
- [CONTEXT.md](CONTEXT.md)：术语表。

## 许可证

MIT。