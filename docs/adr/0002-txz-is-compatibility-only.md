# tar.xz 产物定位为兼容版，不承诺压缩

## Status

accepted

## Context

核查 Mayx 文章的 TXZ 实现后确认：载荷内容以 LZMA2 uncompressed chunk 原样输出，不做压缩。实测 181,200 字节可压缩文本产出 186,300 字节，而同内容普通 xz 仅 288 字节；仓库中提交的 7z 单层脚本同样为 store-only，真正压缩的只有未提交的双层 LZMA2 7z 内联代码。

xz 只允许一条过滤器链，没有 7z 的第二层 coder 来消除 seed 中“压缩内容副本”的重复。压缩型 TXZ 至少要把压缩后的内容再存一份作为自引用来源，体积约 `2×|C|`；只有压缩率高于 2 倍时才小于原始载荷，对图片、可执行文件等已压缩载荷反而更大。

## Decision

V1 同时生成 `.7z` 与 `.tar.xz`。`.7z` 采用双层 LZMA2，必须真正压缩；`.tar.xz` 采用 store-only 自含方案，只保证自含与可解，不承诺小于载荷。CLI 与文档必须明确标注两者的能力差异。

## Considered Options

- 双格式都 store-only：实现统一，但违背“压缩”的项目目标。
- 只做 7z：放弃 Linux / BusyBox 的原生解压能力。
- 压缩型 TXZ：需要原创的 xz 不动点设计，体积约 `2×|C|`，对已压缩载荷可能大于原始载荷；列为后续研究项。

## Consequences

- 验收标准按格式区分：7z 检查压缩收益；tar.xz 检查可解性与逐字节自含。
- “QuinePack 能压缩任意载荷”只对 7z 成立；tar.xz 是兼容产物。
- 若未来实现压缩型 TXZ，需要新增 ADR 取代本条的相关部分。