# -*- coding: utf-8 -*-
r"""12 —— Keeper + 复制:3 个进程内副本,一个写入,三个收敛,死一个再选一个。

复制表的关键不是一个"复制协议",而是**两件事分开**:

.. code-block:: text

   ① Keeper(协调服务)—— 存"谁活着、谁是 leader、复制日志到哪了"这类**小状态**。
      副本通过临时节点(ephemeral)登记自己:会话一断,节点自动消失。

   ② MergeTree part 本身 —— 存在**各副本各自的**存储里。
      副本之间传的不是"SQL",而是"哪个 part 该拉过来"这样的**日志条目**。

本示例用 ``minich.replication.LocalCluster`` 在**一个进程里**起 3 个副本
(``replica_a`` / ``replica_b`` / ``replica_c``)和一台 Keeper,然后把下面这条链
走完,每一步都打印当时的集群状态:

.. code-block:: text

   建集群(replica_a 自动成为 leader)
     → 在 replica_a 上插入一个 part
         → process_all():b、c 从 Keeper 的日志里拉到这个 part,三副本一致
             → 杀掉 leader(replica_a 的会话断 → 临时节点消失)
                 → Keeper 立刻把下一个副本选为新 leader(自动,不用手动选)
                     → 新 leader 做一轮合并决策,全体应用

注意一个设计要点:**leader 选举是"临时节点消失"的副作用,不是一次投票**。
真实 ClickHouse 的 Keeper 用 ZAB/Raft 保证一致性;这里为了教学简化成单机,
所以**无法**演示"脑裂"和"多数派丢失"。

.. warning:: ``cluster.stop()`` 必须调用 —— 否则 Keeper 的清扫线程会一直挂着,
   读者的终端会在脚本结束后卡住。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.replication import LocalCluster  # noqa: E402


def dump_state(cluster: LocalCluster, title: str) -> None:
    print(f"\n--- {title} ---")
    print(f"leader = {cluster.leader()!r}")
    state = cluster.state()
    for name in sorted(state):
        s = state[name]
        print(f"  {name:<10} parts={s.get('parts')!s:<3} rows={s.get('rows')!s:<3} "
              f"log_pointer={s.get('log_pointer')!s:<3} "
              f"is_leader={s.get('is_leader')} is_readonly={s.get('is_readonly')}")


def main() -> None:
    print("=" * 78)
    print("12 · Keeper + 复制 —— 写入、收敛、leader 死亡与重选")
    print("=" * 78)

    # LocalCluster() 默认就是 replica_a/b/c,shard 01,table t;
    # 它自己带一台 KeeperServer,不用我们手动接 ctx.keeper。
    cluster = LocalCluster()
    try:
        dump_state(cluster, "① 建成集群", )
        print("  说明:replica_a 先登记了 leader 临时节点,所以它一上来就是 leader。")

        print("\n>>> cluster.insert('replica_a', partition_id='all', block_id='block1')")
        result = cluster.insert("replica_a", partition_id="all", block_id="block1")
        print(f"    插入结果: {result}")
        dump_state(cluster, "② 只在 a 上写入之后")
        print("  此时 b、c 还没拉到这个 part(log_pointer 停在 0)。")

        print("\n>>> cluster.process_all()   # 让每个副本去消费 Keeper 上的复制日志")
        cluster.process_all()
        cluster.assert_converged()          # 三副本的 part 签名完全一致
        dump_state(cluster, "③ process_all 之后:三副本收敛")
        print("  assert_converged() 通过 —— 三个副本的 part 集合与内容一致。")

        old_leader = cluster.leader()
        print(f"\n>>> cluster.kill_replica({old_leader!r})   # 会话过期,临时节点消失")
        new_leader = cluster.kill_replica(old_leader)
        print(f"    返回的新 leader = {new_leader!r}")
        dump_state(cluster, "④ 杀掉原 leader 之后:自动选出新 leader")
        print(f"  没有手动 'elect' —— 原 leader 的临时节点一消失,Keeper 就把")
        print(f"  下一个序号最小的副本({new_leader})当成 leader。")

        print("\n>>> cluster.process_all(); cluster.assert_converged()"
              "   # 存活的副本仍一致(死掉的副本被跳过)")
        cluster.process_all()
        cluster.assert_converged(only_active=True)
        print("  用 only_active=True:只对'活着'的副本要求一致(死副本冻结在最后状态)。")

        print("\n>>> cluster.insert(new_leader, partition_id='all', block_id='block2')")
        print(f">>> cluster.process_all()")
        cluster.insert(new_leader, partition_id="all", block_id="block2")
        cluster.process_all()
        dump_state(cluster, "⑤ 新 leader 再写一个 part 并让全员跟上")
        print("  现在每个分区有 2 个 part —— 于是有了合并候选。")

        print("\n>>> cluster.merge_round()   # 让新 leader 做一轮合并决策,全体应用")
        entry = cluster.merge_round()
        print(f"    合并产生的日志条目: {entry}")
        cluster.process_all()
        dump_state(cluster, "⑥ 合并之后:两个 part 合成一个")

        print("\n--- system.replicas 风格的状态表(每个副本一行)---")
        for name, row in sorted(cluster.status_rows().items()):
            print(f"  {name:<10} {row}")

        print("""
小结:

  · Keeper 只管**小状态**(谁是 leader、日志消费到哪);数据(part)各副本自己存。
  · "复制" = 每个副本按日志顺序把该拉的 part 拉过来 —— 是**拉**模型,不是推。
  · leader 在本示例里只用来协调**合并**:合并产物也是一条日志,大家各自应用。
  · 本实现的 Keeper 是单机教学版,**没有** Raft / 多数派,复现不了脑裂;
    真实 ClickHouse 的 Keeper 是 3/5 节点的 Raft 集群。

  ⚠ 真实 clickhouse 里,SQL 建 ReplicatedMergeTree 表会去连 Keeper;本仓库里
    那条路会退化成"本地表 + 一句 warning"(见 engines.py),所以本示例直接用
    LocalCluster 显式驱动 —— 这样每一步的状态都看得见。
""")
    finally:
        cluster.stop()           # 不停会留下 Keeper 清扫线程,终端不回收


if __name__ == "__main__":
    main()
