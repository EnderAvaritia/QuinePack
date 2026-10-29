# 热点编码改用子进程并行，产物字节不变

## Status

accepted

## Context

1 GiB 随机载荷的分阶段剖析显示，耗时集中在两处纯 Python 循环，而不是 I/O：

- `txz`：Windows 上 `ctypes.util.find_library("lzma")` 返回空，python.org 的 `_lzma.pyd` 是静态链接、没有可加载的 `liblzma.dll`，CRC64 只能走纯 Python 的 slice-by-8，约 6 MiB/s；一次构建要对 `tar_prefix + archive + zeros` 扫两遍（1 GiB 载荷约 4.3 GB），占 693 s 总耗时的约 80%。
- `7z-store`：布局需要为每个 LZMA2 块手工编码 match token。每次 `layout()` 都会把整份载荷的块重新编码，而 `_StoreBuilder.build()` 为了收敛 `need_ds` 会跑 3 次，前两次的结果全部丢弃；64 MiB 载荷的布局耗时 15.75 s，占总耗时 82%。

两者都是「每个块的输出只由块的输入决定」的纯函数，天然可以分片。直接使用 `multiprocessing` 不可行：Windows 只有 `spawn`，子进程会重新导入调用方的 `__main__`，把没有 `if __name__ == "__main__"` 保护的脚本整份重跑（实测会重复构建并抢写 `.partial` 文件）。

## Decision

新增 `quinepack/parallel.py`：用 `python -c` 启动的常驻工作进程池，工作进程只导入 QuinePack，因此不会重跑调用方代码；任务按顺序提交、按顺序回收。

- `7z-store`：`_StoreBuilder.layout()` 先把每个 match 块拆成 `(distance, total, output_pos)` 计划，再批量编码，最后顺序累加 `file_pos`。每个块都用全新的 range coder，输出只取决于 `(distance, tokens, output_pos & 3)`，所以分片结果与原循环逐字节相同。
- `txz`：`_crc64_pure_update` 改为 `memoryview.cast("Q")` 的按字循环；`_crc64_parts` 在流 ≥ 32 MiB 时把数据放进 `shared_memory`，按 4 MiB 分段并行计算原始状态，父进程用 GF(2) 多项式 `_crc64_combine` 按顺序拼接。
- `7z` 双层布局：`_build_streams()` 先规划整条 chunk 序列（store / lzma / term），再批量编码其中的 LZMA 块；`_build_mseg()` 按原来「每满 2 MiB 冲刷」的边界切分 token 组，同样批量编码。两处都只依赖 `(distance, tokens, output_pos)`，因此顺序累加 `file_pos` 后与原流式编码逐字节相同。该路径的布局定点会重编码 3–4 轮，批量化把每轮从串行变成一轮并发。
- 多格式：`--format all`（或任意多格式）在载荷 ≥ 8 MiB 时为每个格式起一个 `python -m quinepack pack` 子进程，各自扫描、构建、原子写入。父进程按「估计峰值内存」和核数调度：`7z` / `7z-store` 按 2.2× 载荷、`txz` 按 5.1× 载荷估算（1 GiB 实测值），默认预算 8192 MiB（`QUINEPACK_MEMORY_BUDGET_MIB` 可改），放不下的格式排队，先启动最重的一个；子进程通过 `QUINEPACK_WORKERS` 分到 `核数 / 并发数` 个工作进程。
- 小载荷（`7z-store` < 8 MiB、`7z` 双层 < 4 MiB、CRC64 流 < 32 MiB、多格式 < 8 MiB）和 `QUINEPACK_NO_MP=1` 保持进程内/串行路径；工作进程启动失败时自动退回进程内实现。

## Considered Options

- `multiprocessing.Pool`：代码最短，但 Windows `spawn` 会重跑调用方 `__main__`，对库调用方是fork 炸弹式陷阱。
- 把 CRC64 和 range coder 下沉到 Rust/C 扩展：更快，但需要新 ADR、构建链和平台轮子，违背 ADR-0004 的零第三方运行时依赖。
- 只做单核微优化（`cast("Q")`、slice-by-16 表）：实测上限约 1.5×，无法覆盖 4.3 GB 的 CRC64 扫描。
- 改变布局让 `need_ds` 一次收敛：会改变产物体积与字节，破坏与既有基线的一致性。

## Consequences

- 64 MiB 随机载荷：`7z-store` 19.4 → 2.7 s（峰值 153 MiB），`txz` 45.0 → 7.1 s（峰值 279 → 344 MiB），`7z` 44.3 → 33.4 s（峰值 665 MiB）。
- 256 MiB 随机载荷：`7z-store` 105 → 11.6 s（峰值 548 MiB），`txz` 171 → 23.2 s（峰值 1051 → 1310 MiB），`7z` 224 → 157.7 s（峰值 698 MiB）。
- 1 GiB 随机载荷：`7z-store` 411 → 46.9 s（峰值 2119 MiB），`txz` 693 → 88 s（峰值 4142 → 5171 MiB，来自共享内存分片副本），`7z` 923 → 660.9 s（峰值 2116 MiB），均低于 ADR-0007 的 ≤ 8 GB 门槛。
- 1 GiB `--format all`：串行三格式合计 797 s，扇出后墙钟 685.9 s（`7z` 与 `txz` 并行、`7z-store` 排队），产物与单格式构建逐字节相同。
- 产物逐字节不变：64 MiB 三种格式的 SHA-256 与改造前完全一致；1 GiB 三种产物通过 `7z t` / `xz -t` 与自含副本校验。
- `7z` 压缩版剩下的 87% 是 `lzma.LZMACompressor` 的单线程 C 编码（64 MiB 载荷 33.4 s 中 29.1 s），标准库没有多线程 LZMA2 编码器，再优化 Python 侧收益已可忽略。
- 每个格式的 CRC 定点见 ADR-0009：96/128 次全归档扫描降为 1 次。
- 每次构建多出约 0.6 s 的工作进程启动开销，由上述阈值挡在小载荷之外。
- `txz` 的 CRC64 在无法创建共享内存或工作进程的环境里退回单核，正确性不受影响。
- 多格式扇出会重新扫描一遍输入（每个子进程各自扫描），换来的是三份产物互不阻塞；扫描成本相对构建可忽略。
