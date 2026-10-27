# QuinePack

把本地文件或目录树转换为「自含压缩包」：解压后会得到原始载荷，以及归档自身的一份逐字节副本。

英文名 **Quine Archive** 指这种“解压结果包含压缩包自身副本”的归档。

## 状态

阶段 2 已实现：

- `7z`：压缩版，双层 LZMA2 coder chain。
- `7z-store`：兼容版，单层 LZMA2 store。
- `txz`：兼容版，store-only tar.xz，可用 `xz` / `tar` 读取，并用 Python 标准库 `lzma` / `tarfile` 校验。
- `pack`、`verify`、`version` 三个命令。
- `--format all` 可一次生成三种产物。
- 相同载荷字节与 mtime 下，产物确定。

尚未实现：

- 面向 1 GiB 载荷的流式 / mmap 写入；当前仍把载荷与归档保存在内存中，见 `docs/adr/0007-1gib-prototype-baseline.md`。
- tar PAX 长路径；当前受 ustar 的 100 字节 name / 155 字节 prefix 限制。
- ZIP 变体；ZIP 已明确不支持，见 `docs/adr/0006-no-zip.md`。

## 环境要求

- Python 3.11 或更高版本。
- 运行时无第三方依赖。
- 仅校验 `7z` / `7z-store` 时需要官方 7-Zip（`7z`、`7zr`，或 `--7zr PATH`）。
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

校验已有归档：

```bash
quinepack verify dist/site.7z --against site/ --7zr "/path/to/7za"
quinepack verify dist/site.tar.xz --against site/
```

常用参数：

- `--format`：`7z`、`7z-store`、`txz`，或逗号分隔组合；`all` 表示三种全部生成。
- `-o, --output`：指定单个输出文件，只能配合单一格式。
- `--outdir`：指定输出目录。
- `--preset`：7z 压缩等级 0–9，默认 6。
- `--exclude`：按相对路径 glob 排除内容，可重复使用。
- 非交互环境（非 TTY）必须显式传 `--format`。
- `--force`：覆盖已存在的输出。
- `--verify`：写入后立即校验；7z 变体需要外部 7-Zip。
- `--quiet` / `--verbose`：减少或增加输出。

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
```

当前测试覆盖：

- 7z / 7z-store / txz 的生成与校验。
- 空文件、空目录、Unicode 文件名、ustar prefix。
- 自含副本 SHA-256、CRC64 fixpoint、确定性重复构建。
- 路径穿越、符号链接、硬链接、设备/FIFO 等不安全 tar 成员。
- `--format all`、`-o` 自定义文件名、非 TTY 保护与 `.partial` 原子写入。

## 已知限制

- 阶段 2 仍为全内存实现，1 GiB 载荷的峰值内存尚未达到 ADR-0007 目标。
- tar 名称超过 ustar 限制时返回载荷错误（退出码 4），不会截断。
- `7z-store` 在约 1500 个以上小文件时可能失败：其 gadget 会包含完整 7z header，超过单个 64 KiB store chunk 上限；`7z` 压缩版不受此限制。
- 1 GiB 随机载荷的原型基线已测（约 596 s、峰值 16.6 GB），但生产实现仍是全内存；100 MiB 级常规载荷的回归测试尚未系统覆盖。
- Windows 上纯 Python CRC64 仍会带来明显的二次遍历开销。

## 许可证

MIT。