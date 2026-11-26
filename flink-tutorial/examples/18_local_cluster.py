"""MiniFlink 18：LocalCluster 的 slot 分配与作业提交。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import StreamExecutionEnvironment
from miniflink.deployment import LocalCluster

env = StreamExecutionEnvironment(parallelism=2)
env.from_collection(range(6), parallelism=2).map(lambda value: value + 1).print()

cluster = LocalCluster(task_managers=2, slots_per_task_manager=2)
job = cluster.submit(env, job_name="cluster-demo")
print("job status:", job.status.value)
print("slots:", cluster.available_slots, "/", cluster.total_slots)
