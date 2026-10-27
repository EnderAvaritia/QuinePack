# QuinePack V1 产品规格

状态：待确认。本文只描述产品行为与验收标准，不规定具体代码结构。

## 1. 目标

输入一个本地文件或目录树，产出至少一种「自含压缩包」：解压后得到载荷的全部内容，以及该产物自身的一份逐字节副本。压缩型 7z 必须显著压缩载荷；兼容型产物允许不压缩。

## 2. 非目标

- URL 抓取、网页离线化（只预留输入适配层，不在 V1）。
- 超过 1 GiB 的单次载荷。
- 压缩型 tar.xz。
- Windows 10 资源管理器原生打开 7z。
- GUI、加密、增量备份、去重、云上传。
- ZIP 产物（store-only ZIP 的数据区无法从文件起点开始，DEFLATE ZIP quine 又受 32 KiB 窗口限制；见 ADR-0006）。

## 3. 产物矩阵

| `--format` | 产物名 | 压缩方式 | 目标解压器 | 体积预期 |
|---|---|---|---|---|
| `7z` | `site.7z` | 双层 LZMA2 | 官方 7-Zip（`7z` / `7zr`） | 接近内容压缩后体积 |
| `7z-store` | `site.store.7z` | 单层 store | 官方 7-Zip；单 coder 期望可被 libarchive / Win11 资源管理器打开 | ≈ 原始载荷 |
| `txz` | `site.tar.xz` | store-only 单 LZMA2 流 | `xz` / `tar` / BusyBox | ≈ 原始载荷 |

`--format all` 等价于同时请求三种产物。

## 4. 解压布局

载荷内容直接铺在解压根目录，自含副本与原产物同名，种子数据放在 `.quinepack/` 下：

```
site/
├── index.html
├── assets/...
├── site.7z                        ← 自含副本
└── .quinepack/
    └── site.7z.seed               ← 种子数据
```

- 种子文件名包含产物名，避免把多个格式解压到同一目录时互相覆盖。
- 载荷根目录出现 `.quinepack/`，或出现与产物同名的文件时，生成前直接报错。
- 产物路径落在载荷目录内部时直接报错，避免自我包含。
- 单文件载荷直接以原文件名出现在根目录。

## 5. 命名

| 输入 | `7z` | `7z-store` | `txz` |
|---|---|---|---|
| 目录 `site/` | `site.7z` | `site.store.7z` | `site.tar.xz` |
| 单文件 `app.exe` | `app.exe.7z` | `app.exe.store.7z` | `app.exe.tar.xz` |

- 单文件保留原扩展名，避免 `app.exe` 与 `app.zip` 撞名。
- 自含副本 entry 使用与产物完全相同的文件名。
- 默认输出到输入的同级目录。

## 6. CLI

```
quinepack pack <input> [--format LIST] [-o FILE | --outdir DIR]
                      [--preset N] [--exclude GLOB]... [--force]
                      [--verify] [--7zr PATH] [--quiet | --verbose]

quinepack verify <archive> [--against <payload>] [--7zr PATH]
                           [--quiet | --verbose]

quinepack version
```

### 6.1 格式选择

- `--format` 接受逗号分隔的 `7z`、`7z-store`、`txz`，或单独的 `all`。
- 未传 `--format` 且 stdin/stdout 是 TTY：进入交互式菜单。
- 未传 `--format` 且不是 TTY：直接报错，提示必须显式指定。
- `all` 不能与其他值组合。

### 6.2 交互式菜单

```
QuinePack: site (12.3 MiB)
Select output format(s):
  1) 7z        compressed, requires 7-Zip        [default]
  2) 7z-store  compatibility, not compressed
  3) tar.xz    Linux/BusyBox, not compressed
  a) all

> 
```

- 直接回车等于 `1`。
- 支持 `1,3` 这类多选，`a` 表示全部。
- 非法输入重新提示；Ctrl-C 退出码 130。
- 载荷超过 1 GiB 时在菜单前报错。

### 6.3 输出路径

- 默认输出目录是输入的同级目录。
- `-o FILE` 只在“恰好一个格式”时允许，指定完整文件路径。
- `--outdir DIR` 用于任意格式组合，文件名按第 5 节推导。
- `-o` 与 `--outdir` 同时出现时直接报错。

### 6.4 覆盖策略

- 目标已存在且是 TTY：逐文件询问是否覆盖；回答 `n` 跳过该格式。
- 目标已存在且不是 TTY：报错并以退出码 5 结束，除非传 `--force`。
- `--force` 跳过所有覆盖询问。
- 所有产物先写 `<output>.partial`，成功后 `os.replace` 原子替换；失败或中断时清理 partial，已存在的旧产物保持不变。

### 6.5 其它参数

- `--preset N`：0–9，默认 6，只影响压缩型 7z；与其他格式同用时给出警告。
- `--exclude GLOB`：可重复，相对于载荷根目录的 glob；命中目录时整棵剪枝。
- `--verify`：生成后立刻用原载荷校验产物。
- `--7zr PATH`：显式指定 7-Zip 可执行文件；未指定时依次在 PATH 中查找 `7zr`、`7z`、`7z.exe`、`7zr.exe`。
- `--quiet` 抑制进度，仅保留错误；`--verbose` 输出阶段细节；两者同时使用报错。

### 6.6 verify 子命令

- 始终检查：归档可解、自含副本存在且 SHA-256 等于归档文件本身。
- `--against <payload>`：额外逐文件比对解压出的载荷。
- 7z / 7z-store 需要外部 7-Zip；txz 使用标准库。
- 找不到 7-Zip 且需要校验 7z 时，退出码 6。

### 6.7 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行时失败 |
| 2 | 参数错误 |
| 3 | 校验失败 |
| 4 | 载荷超限或不受支持 |
| 5 | 产物已存在，非交互环境未加 `--force` |
| 6 | 缺少外部 7-Zip |
| 130 | 用户中断 |

## 7. 载荷扫描

- 输入必须是已存在的本地文件或目录。
- 生成前统计总字节数，超过 1 GiB 直接拒绝。
- 目录按相对路径的 POSIX 形式（`/` 分隔）稳定排序。
- 默认包含隐藏文件（含 `.git`、`.env`）。
- 符号链接默认跟随目标内容；检测环路并报错。
- 保留空目录与 mtime。
- tar.xz 保留 POSIX 权限位；7z 保留 Windows 属性（只读/隐藏/系统/归档），不承诺 POSIX 执行位。
- 不保留硬链接、稀疏文件、xattr、ACL；硬链接按独立文件复制。
- 读取期间若文件大小或 mtime 变化，报错并清理产物。
- 大小写不敏感的路径冲突给出警告，不自动改名。
- 文件名按原样保留；tar.xz 使用 PAX 以支持长路径与 UTF-8，7z 使用 UTF-16。

## 8. 确定性与性能

- 同一份载荷（路径、内容、mtime）与同一组参数，产物必须逐字节一致（ADR-0005）。
- 不写入生成时间，不使用随机数，压缩参数固定。
- 压缩型 7z 默认 preset 6，内容压缩字典上限 64 MiB。
- 1 GiB 场景使用 mmap、增量 CRC、分块 token 生成；内存预算为产物体积的 2–3 倍。
- 进度按阶段输出到 stderr：扫描、压缩、布局、CRC 定点、写入、校验。
- 1 GiB 的耗时与峰值内存基线在原型阶段测量后写入验收文档。

## 9. 验证与验收

### 9.1 三层验收

1. **外部解压**：官方 7-Zip 解压 7z / 7z-store；`xz` + `tar` 解压 txz。
2. **逐字节一致**：载荷每个文件与原始文件 SHA-256 相同；自含副本与产物文件 SHA-256 相同。
3. **体积门槛**：
   - `size(quine.7z) <= size(plain.7z) + max(2% * size(plain.7z), 256 KiB)`
   - `size(quine.store.7z) <= size(plain.store.7z) + max(5% * size(plain.store.7z), 512 KiB)`
   - `size(quine.tar.xz) <= size(plain.tar) + max(5% * size(plain.tar), 512 KiB)`

其中 `plain.7z` 用官方 7-Zip 对同一载荷生成；`plain.store.7z` 用 `-mx=0`；`plain.tar` 用未压缩 tar。

### 9.2 测试矩阵

- 单元：路径扫描、命名、CRC 线性代数、LZMA2 chunk 编码、7z/tar 头部。
- 集成：文本、二进制、空目录、Unicode 名称、长路径、单文件、空文件、`--exclude`。
- 确定性：同一输入跑两次，产物 SHA-256 相同。
- 规模：100 MiB 常规测试；1 GiB 慢速测试，记录耗时与峰值内存。
- 兼容：官方 7-Zip（用户自备）、`xz 5.6.4` + GNU `tar 1.35`；BusyBox 与 Win11 资源管理器为人工/后续验证项。

### 9.3 外部工具缺失

- 测试套件检测不到 7-Zip 时，官方 7z 验收用例标记为 skipped，并打印明确提示。
- V1 发布验收必须在有官方 7-Zip 的环境中跑完全部用例，不能用 skipped 代替。

## 10. 打包与分发

- `pyproject.toml`，包名 `quinepack`，版本 `0.1.0`。
- `requires-python = ">=3.11"`；开发与验收使用 3.14。
- 运行时零第三方依赖；测试依赖 pytest。
- 提供 `quinepack` 控制台命令与 `python -m quinepack`。
- 许可证 MIT。
- 不内置 7-Zip；用户自备并通过 PATH 或 `--7zr` 指定。
- README 以中文为主，附英文摘要；CLI 帮助与错误信息使用英文。

## 11. 计划目录结构

```
quinepack/
  __init__.py
  __main__.py
  cli.py
  errors.py
  naming.py
  payload.py
  scan.py
  verify.py
  crc.py
  lzma2.py
  layout.py
  formats/
    __init__.py
    base.py
    sevenzip.py
    sevenzip_store.py
    tarxz.py
tests/
docs/adr/
CONTEXT.md
README.md
pyproject.toml
```

## 12. 技术约束

- 7z 允许任意 coder 图，双层 LZMA2 合法；官方 7-Zip 可解，libarchive 会拒绝，因此 Win11 资源管理器的 7z 预览/解压不可用（Windows 10 资源管理器根本不支持 7z）。
- `.xz` 只允许一条过滤器链，LZMA2 必须是最后一个过滤器，双层 LZMA2 非法；V1 的 tar.xz 因此是 store-only。
- LZMA2 单个 LZMA chunk 解压上限 2 MiB、压缩上限 64 KiB；字典大小固定，match 不能超出字典窗口；`0xC0` 只重置状态与属性，不重置字典。
- 7z 与 xz 都带校验值，自含副本会把产物自身的字节纳入校验，需要 CRC 定点；7z 的 CRC32 定点用 GF(2) 线性代数求解。
- ZIP 的 local file header 没有数据偏移字段，entry 数据区必然从本地头之后开始；该区域若为文件后缀则不可能等于整个文件，若越过 EOF 则无法读满。因此 store-only ZIP 无法自含，DEFLATE ZIP quine 又受 32 KiB 窗口限制。

## 13. 开放项

- 1 GiB 原型的时间与内存基线。
- CI 平台与工作流。
- 项目目录从 `selfconbine` 改名为 `QuinePack`（当前会话占用目录，需会话结束后操作）。
- Win11 资源管理器对 7z-store 的实际行为、BusyBox 对 txz 的实际行为需要人工验证。