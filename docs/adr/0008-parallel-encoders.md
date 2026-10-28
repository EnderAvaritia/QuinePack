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
- 小载荷（`7z-store` < 8 MiB、CRC64 流 < 32 MiB）和 `QUINEPACK_NO_MP=1` 保持进程内路径；工作进程启动失败时自动退回进程内实现。

## Considered Options

- `multiprocessing.Pool`：代码最短，但 Windows `spawn` 会重跑调用方 `__main__`，对库调用方是fork 炸弹式陷阱。
- 把 CRC64 和 range coder 下沉到 Rust/C 扩展：更快，但需要新 ADR、构建链和平台轮子，违背 ADR-0004 的零第三方运行时依赖。
- 只做单核微优化（`cast("Q")`、slice-by-16 表）：实测上限约 1.5×，无法覆盖 4.3 GB 的 CRC64 扫描。
- 改变布局让 `need_ds` 一次收敛：会改变产物体积与字节，破坏与既有基线的一致性。

## Consequences

- 64 MiB 随机载荷：`7z-store` 19.4 → 5.7 s（峰值 152 → 153 MiB），`txz` 45.0 → 7.1 s（峰值 279 → 344 MiB）。
- 256 MiB 随机载荷：`7z-store` 105 → 23.3 s（峰值 546 → 548 MiB），`txz` 171 → 23.2 s（峰值 1051 → 1310 MiB）。
- 1 GiB 随机载荷：`7z-store` 411 → 93 s（峰值 2120 → 2118 MiB），`txz` 693 → 88 s（峰值 4142 → 5171 MiB，来自共享内存分片副本），仍低于 ADR-0007 的 ≤ 8 GB 门槛。
- 产物逐字节不变：64 MiB 三种格式的 SHA-256 与改造前完全一致；1 GiB `7z-store` / `txz` 分别通过 `7z t` 与 `xz -t`。
- 每次构建多出约 0.6 s 的工作进程启动开销，由上述阈值挡在小载荷之外。
- `txz` 的 CRC64 在无法创建共享内存或工作进程的环境里退回单核，正确性不受影响。