# QuinePack

把本地文件或目录树转换为「自含压缩包」：解压后会得到原始载荷，以及归档自身的一份逐字节副本。

英文名 **Quine Archive** 指这种“解压结果包含压缩包自身副本”的归档。

## 状态

阶段 3 与阶段 4 的部分能力已实现：

- `7z`：压缩版，双层 LZMA2 coder chain。
- `7z-store`：兼容版，单层 LZMA2 store。
- `txz`：兼容版，store-only tar.xz，可用 `xz` / `tar` 读取，并用 Python 标准库 `lzma` / `tarfile` 校验。
- `txz` 对超出 ustar 的 100 字节 name / 155 字节 prefix 限制的名称，以及包含非 ASCII 字符的名称，写入 PAX 扩展头；长载荷路径、Unicode 文件名、长产物名与种子名均可往返。
- `pack`、`verify`、`version` 三个命令。
- `--format all` 可一次生成三种产物。
- 相同载荷字节与 mtime 下，产物确定；打包阶段进度输出到 stderr，并对大小写不敏感的路径冲突给出警告。
- 大型载荷的扫描内容、压缩流与 tar 前缀会落到当前目录的 `.quinepack-spool-*` 临时文件并通过 `mmap` 访问；构建结束或失败后清理。
- 单格式打包会在内容不再需要时主动释放映射，避免载荷与中间流长期同时驻留。
- 验收测试会调用本机官方 7-Zip 与 `xz` / `tar`（存在时）。

Windows、Python 3.12、不可压缩随机载荷实测的峰值工作集：

| 载荷 | `7z` | `7z-store` | `txz` |
|---|---:|---:|---:|
| 64 MiB | 664 MiB / 44 s | 149 MiB / 9 s | 277 MiB / 43 s |
| 256 MiB | 697 MiB / 224 s | 538 MiB / 35 s | 1050 MiB / 167 s |
| 1 GiB | 2107 MiB / 923 s | 2094 MiB / 145 s | 4141 MiB / 688 s |

每行由 `tools/bench_memory.py` 测得；1 GiB 随机载荷满足 ADR-0007 的 ≤ 8 GB 目标。

> `7z-store` / `txz` 的 64 MiB 以上数字为修复前测量；当时产物无法通过官方校验。修复后已验证 48 MiB `7z-store`、42 MiB `txz` 以及 1 GiB 两格式的正确性；表中 64 MiB 以上 `7z-store` / `txz` 的耗时与峰值仍为修复前基线，尚未重测。

### 1 GiB 验收（当前提交）

2026-10-28 在当前 `7z-store` / `txz` 布局上重建并验收 1 GiB 载荷：

| 格式 | 产物大小 | 官方校验 | 自含副本 SHA-256 |
|---|---:|---|---|
| `7z-store` | 1,077,172,763 B | `7z t` → `Everything is Ok` | `75e7c9cf8a5edfc94bc6dcaf4f74cfaa0d395aad289ada2d5580cd8c8fdaca60` |
| `txz` | 1,077,176,176 B | `xz -t` 通过 | `71a03875b6b8f970f040bd98917276a52e5f5053a61366e34c67f7b724286beb` |

两种自含副本均与产物逐字节一致。1 GiB 外部 `tar -xJf` 在 Windows 样本上出现 `tar`/`xz` 管道悬挂，因此 `txz` 的自含副本改用 Python `tarfile` 读取校验；归档本身已通过 `xz -t`。

尚未完成：

- 归档 `F` 仍整份驻留；压缩版 seed 在 `release_payload` 路径上已改为虚拟种子，不再分配整份 seed。
- 压缩版 tar.xz（ADR-0002 限定为 store-only）。
- 1 GiB `7z-store` / `txz` 的耗时与峰值复测（正确性已通过本页验收）。
- 超过 1 GiB 的载荷（ADR-0003）。
- ZIP 变体（ADR-0006）。
- `path` 之外的 PAX 记录（mtime 纳秒、超大文件大小、xattr 均不携带）。

## 环境要求

- Python 3.11 或更高版本。
- 运行时无第三方依赖。
- 仅校验 `7z` / `7z-store` 时需要官方 7-Zip（`7z`、`7zr`、标准 Windows 安装目录，或 `--7zr PATH`）。
- `txz` 的生成与校验不需要 7-Zip。

## 安装

```bash
pip install -e .
```

安装后可直接使用 `quinepack` 命令，也可以继续用 `python -m quinepack`。

## 用法

生成压缩版，默认输出到输入旁边：

```bash
quinepack pack site/ --format 7z
```

一次生成三种产物并校验：

```bash
quinepack pack site/ \
  --format all \
  --outdir dist/ \
  --7zr "/path/to/7za" \
  --verify
```

只生成 txz 并校验，不需要 7-Zip：

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

校验已有归档：

```bash
quinepack verify dist/site.7z --against site/ --7zr "/path/to/7za"
quinepack verify dist/site.tar.xz --against site/
```

查看版本：

```bash
quinepack version
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
- `--7zr PATH`：显式指定 7-Zip 可执行文件；未指定时从 `PATH` 或标准 Windows 安装目录查找。
- `--quiet` / `--verbose`：减少或增加输出，两者互斥。
- 非交互环境（非 TTY）必须显式传 `--format`。

### `verify` 参数

- `archive`：要校验的归档文件。
- `--against PATH`：同时把解压结果与原始载荷目录逐文件比对。
- `--exclude GLOB`：比对时按相对路径排除内容，可重复使用。
- `--7zr PATH`：校验 7z / 7z-store 时需要的 7-Zip 可执行文件；txz 不需要。
- `--quiet` / `--verbose`：减少或增加输出，两者互斥。

格式选择建议：

- 需要体积收益：用 `7z`。
- 文件条目数很多（实测 1660 个小文件可建，1680 个失败，具体取决于名称与元数据）：用 `7z` 或 `txz`，避免 `7z-store`。
- 不想依赖 7-Zip：用 `txz`。

## 输出布局

`--format all` 会生成：

```text
dist/
├── site.7z
├── site.store.7z
└── site.tar.xz
```

解压任一产物后，目录中同时包含载荷与自含副本：

```text
site/
├── index.html
├── assets/...
├── site.7z                     # 自含副本，文件名与产物相同
└── .quinepack/
    └── site.7z.seed            # 种子 / 引导副本
```

`7z-store` 与 `txz` 的目录结构相同，只是自含副本和种子文件名分别对应 `site.store.7z`、`site.tar.xz`。

产物名同时决定自含副本与种子名：自含副本与产物同名，种子为 `.quinepack/<产物名>.seed`。使用 `-o` 自定义文件名时建议保留对应后缀（`.7z`、`.store.7z`、`.tar.xz`），否则 `verify` 与外部工具可能无法仅凭名称识别格式。一次生成多种格式时不能分别指定文件名；需要多次运行 `pack`，每次用 `-o`。

`tar.xz` 在成员名无法放入 ustar 字段或包含非 ASCII 字符时，会在该成员前写入 PAX 扩展头。长载荷路径、Unicode 文件名与长产物名因此可以经 GNU `tar`、BusyBox `tar` 与 Python `tarfile` 往返。

**种子（Seed）** 是重建自含副本所需的脚手架数据，不属于载荷；解压后可以删除。不要把它当作随机种子、密钥或第二份载荷。

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

## 开发

```bash
python -m pytest -q
python -m compileall -q quinepack tests
python tools/bench_memory.py --size 256 --format 7z
```

当前测试覆盖：

- 7z / 7z-store / txz 的生成与校验。
- 空文件、空目录、Unicode 文件名、ustar prefix 与 PAX 长名称。
- 自含副本 SHA-256、CRC64 fixpoint、确定性重复构建。
- 路径穿越、符号链接、硬链接、设备/FIFO 等不安全 tar 成员。
- `7z-store` footer 边界：1660 个小文件可建，2000 个给出明确错误。
- `--format all`、`-o` 自定义文件名、非 TTY 保护与 `.partial` 原子写入。

测试套件会从 `PATH` 或标准 7-Zip 安装目录查找 `7z` / `7zr` 运行官方验收用例；找不到时跳过并给出提示。仅当 `xz` 与 `tar` 都缺失时，才跳过对应验收用例。

阶段 4 已把扫描内容、压缩流与 tar 前缀移出 Python 堆；小载荷仍走纯内存快路径。峰值内存仍由压缩器、种子与输出归档共同决定，见上表与 `docs/adr/0007-1gib-prototype-baseline.md`。

## 已知限制

- 仍不是完整流式实现：归档 `F` 仍整份驻留；压缩版 seed 在单格式 `release_payload` 路径上已改为虚拟种子。临时文件位于当前工作目录，构建失败时也会尽力清理。
- 需要 PAX 扩展头、但名称中含换行等 PAX 不支持的字符时，返回载荷错误（退出码 4），不会截断或静默改名。
- `7z-store` 的尾部 gadget 必须装进单个 64 KiB LZMA2 store chunk；header 随文件条目数增长，因此条目很多时会失败。固定测试数据（`f00000.txt`、1 字节文件）实测 1660 个可建，1680 个失败；报错为 `7z-store cannot encode ... above the 65536-byte LZMA2 store chunk limit`，并提示改用 `--format 7z` 或 `--format txz`。该限制由文件条目数 / header 大小触发，不是载荷总字节数上限；`7z` 压缩版在 2000 个小文件下已验证正常。要真正突破该上限，需要实现 7z `kEncodedHeader`（压缩 header）或放弃单 coder 的 `7z-store` 兼容性，当前未实现。
- 1 GiB 随机载荷 `7z` 实测约 923 s、峰值约 2.1 GB；100 MiB 级常规载荷的回归测试尚未系统覆盖。
- 大文件布局回归已覆盖 42 MiB `txz`、48 MiB `7z-store` 与 1 GiB 两格式（官方 `xz -t` / `7z t`，自含副本 SHA-256 一致）；1 GiB 的性能/峰值仍未在当前实现上重测，生产使用前请按目标尺寸做性能验收。
- Windows 上纯 Python CRC64 仍会带来明显的二次遍历开销。

## 许可证

MIT。