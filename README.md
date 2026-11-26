# 开源缺口修复（Open-Source Gap Fixes）

归档体系在开源共建中发现并修复的上游缺口参考实现。每个条目对应一份已发布的 RFC/提案，提供「提案 + 现成实现」双弹：上游一旦接受方向，落地成本已降到最低。

## 条目索引

| 条目 | 上游仓库 | 关联提案 | 内容 |
| --- | --- | --- | --- |
| `memos-span-contract/` | MemTensor/MemOS | RFC [#2656](https://github.com/MemTensor/MemOS/issues/2656)（缺口清单 #2527） | span 级更新契约：未动区校验 + off/warn/enforce 三模式门控；含对 main @ 3a16e5f 的 git patch |
| `promptfoo-strict-eval/` | promptfoo/promptfoo | [#4321 评论](https://github.com/promptfoo/promptfoo/issues/4321)（假绿评测门禁） | opt-in `--strict` 门禁：未知顶层键告警（含 "Did you mean defaultTest.assert?" 提示）+ 零断言失败；JS 实现 |
| `mem0-journal-first/` | mem0ai/mem0 | [#7982](https://github.com/mem0ai/mem0/issues/7982)（跨库原子性） | journal-first 变更路径：PENDING→APPLIED 记账、启动恢复扫描、按 scope 写锁收 TOCTOU 家族 |
| `openviking-delivery-receipt/` | volcengine/OpenViking | [#8232](https://github.com/volcengine/OpenViking/issues/8232)（统一投递结算契约） | per-delivery receipt 契约：dequeue 返回 (message_id, delivery_id)、ack/retry 条件结算、serial 槽仅在当前投递结算时释放；含 RFC 验收场景测试 |
| `mempalace-wal-recovery/` | MemPalace/mempalace | [#3050](https://github.com/MemPalace/mempalace/issues/3050)（WAL 恢复契约） | wal_id 关联配对（无 ID 退回顺序配对）+ 意图条目 fsync + dry-run 默认的幂等回放 |

## 红蓝对抗加固（2026-11-26）

全部五个条目已通过一轮红蓝对抗验证：

- **红队**：16 项攻击向量（原缺陷 PoC 复现 + 新增向量：毒行注入、双重应用、幂等键互撞、坏 JSONL、崩溃卡死、陈旧 ACK、时钟回拨），对原始副本命中 11 项。
- **蓝队**：修复 4 项内核级缺陷并补修时钟回拨（RT-J5）后，16/16 全部防御成立。
- **回归**：五套原有测试套件全绿，零回归。
- 本仓库当前内容即加固后的修复版；上游缺陷在各自 main 分支仍在（行号证据见各提案议题的跟进评论）。

## 通用约定

- 所有实现**自包含、仅标准库**（promptfoo 条目为标准 JavaScript），可直接独立运行。
- 每个条目带回归测试，覆盖对应提案的验收场景（崩溃注入、交错写入、迟到 ACK 注入等）。
- 实现映射上游真实挂载点（模块/函数名在对应提案稿中逐行核验），供上游直接参照或移植。

## 运行测试

```bash
# Python 条目（memos-span-contract / mem0-journal-first / openviking-delivery-receipt / mempalace-wal-recovery）
python3 -m unittest discover -s <条目目录>/tests -v

# promptfoo 条目（Node.js 内置测试器）
node --test promptfoo-strict-eval/test_strict_eval.cjs
```

## 关联

- 全部提案均已在对应仓库对外发布（账号 tanbopiao），进入对等回馈观察期。
- 镜像：本仓库与魔挂数据集 [zongyuanroot/open-source-gap-fixes](https://www.modelscope.cn/datasets/zongyuanroot/open-source-gap-fixes) 内容一致，互为镜像。
- 缺口发现方法：源码逐行核验 + 社区议题家族归因，详见各条目提案正文。
