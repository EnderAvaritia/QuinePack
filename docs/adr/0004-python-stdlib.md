# 使用 Python 3.14 与标准库实现

## Status

accepted

## Context

参考实现（Mayx 的 7z / TXZ quine）本身就是 Python；标准库 `lzma` 能直接产出 raw LZMA2 流，`tarfile`、`struct`、`mmap`、`binascii` 足以完成容器、CRC 定点与分块处理。1 GiB 载荷上限要求性能与内存可控，但首版更需要正确性与可调试性；Rust 全量重写会把位级 7z/LZMA 代码的移植成本放大数倍。

## Decision

QuinePack 使用 Python 3.14 实现。运行时核心只依赖标准库，不引入第三方运行时依赖；测试可以使用 pytest 等开发依赖。1 GiB 场景通过 mmap、增量 CRC 与分块 token 生成来控制内存与耗时。

## Considered Options

- Python + Rust 核心：性能更好，但两套语言、构建链与调试成本显著上升。
- Rust 全量重写：性能最好，但首版周期最长。
- Go 或其他语言：现成参考代码最少，LZMA2 位级控制风险最高。

## Consequences

- 必须避免一次性构造全量 token 列表；quine 区指令按块生成、直接写入目标文件。
- CRC 定点必须利用线性性质做增量计算，不能对整文件反复重扫。
- CLI 启动与纯 Python 循环会比原生实现慢，需在 1 GiB 验收载荷上给出耗时基线。
- 若后续性能不达标，可以新增 ADR 将热点下沉到 Rust 扩展，但不得改变产物格式。