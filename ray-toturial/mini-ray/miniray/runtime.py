"""运行时全局状态 —— ``miniray.init()`` / ``miniray.shutdown()``。

一个进程里只有一个「Ray 运行时」。driver 进程 ``init()`` 的时候会在**同一个进程内**
拉起三个东西:

.. code-block:: text

   driver 进程(你的 python 脚本)
   ┌──────────────────────────────────────────────────────────────┐
   │  GCS 服务线程        集群元数据(节点/actor/函数/对象目录)  │
   │  Raylet 服务线程     调度 + 对象存储 + worker 池 + 血缘      │
   │  Driver CoreWorker   你的脚本用来提交任务/取值的客户端       │
   └──────────────────────────────────────────────────────────────┘
              ▲  TCP(127.0.0.1:随机端口)      ▲
              │                               │
        worker 进程 1 … N ──────────────────┘  (spawn 出来的子进程)

真实 Ray 里 GCS 与 raylet 都是**独立进程**(C++),driver 通过 gRPC 连它们。
mini-ray 把它们压进 driver 进程的线程里 —— 这是为了「零依赖、可调试」做的
最大一处简化,代价是:driver 进程挂掉 = 整个集群挂掉(真实 Ray 里 driver
挂掉后集群还在,只是 job 结束)。

.. warning::
   在 worker 进程里 ``init()`` 会报错 —— worker 已经在集群里了。
   真要在 worker 里拿运行时,用 :func:`get_core_worker`。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Dict, Optional

from .core_worker import CoreWorker
from .errors import MiniRayError
from .gcs import GcsService
from .ids import DriverID, JobID, NodeID, WorkerID
from .raylet import Raylet, RayletConfig
from .rpc import RpcServer

__all__ = [
    "init",
    "shutdown",
    "is_initialized",
    "get_core_worker",
    "get_raylet",
    "get_session_dir",
    "set_core_worker",
    "get_runtime_context",
    "RuntimeContext",
    "get_gpu_ids",
]

logger = logging.getLogger("miniray")


class _GlobalState:
    """进程内的运行时单例。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.core_worker: Optional[CoreWorker] = None
        self.raylet: Optional[Raylet] = None
        self.raylet_server: Optional[RpcServer] = None
        self.gcs_service: Optional[GcsService] = None
        self.gcs_server: Optional[RpcServer] = None
        self.session_dir: Optional[str] = None
        self.job_id: Optional[str] = None
        self.namespace: str = "default"
        self.is_driver: bool = False
        self.start_time: float = 0.0
        self.options: Dict[str, Any] = {}


_GLOBAL = _GlobalState()


class RuntimeContext:
    """``miniray.get_runtime_context()`` 的返回值(对齐 Ray 的同名 API)。

    在 task / actor 里拿到的 ID 用于日志、指标、调试 —— 分布式系统里
    「我现在在哪个 worker 上跑」是排查问题的第一手信息。
    """

    def __init__(self, worker: CoreWorker, *, actor_id: Optional[str] = None) -> None:
        self._worker = worker
        self._actor_id = actor_id

    def get_worker_id(self) -> str:
        return self._worker.worker_id

    def get_node_id(self) -> str:
        return self._worker.node_id

    def get_job_id(self) -> str:
        return self._worker.job_id

    def get_actor_id(self) -> Optional[str]:
        if self._actor_id:
            return self._actor_id
        return os.environ.get("MINIRAY_ACTOR_ID")

    def get_namespace(self) -> str:
        return self._worker.namespace

    def __repr__(self) -> str:  # pragma: no cover - 调试
        return (
            f"RuntimeContext(worker={self.get_worker_id()[:8]}, "
            f"node={self.get_node_id()[:8]}, job={self.get_job_id()[:8]})"
        )


def get_runtime_context() -> RuntimeContext:
    return RuntimeContext(get_core_worker())


def get_gpu_ids():
    """当前 worker 被分配到的 GPU 序号(Ray 的同名 API)。

    调度器分配 GPU 的方式是设置 ``CUDA_VISIBLE_DEVICES``,这个函数把它读回来。
    """
    value = os.environ.get("CUDA_VISIBLE_DEVICES", "")
    ids = []
    for part in value.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


# ---------------------------------------------------------------------------
# init / shutdown
# ---------------------------------------------------------------------------


def init(
    address: Optional[str] = None,
    *,
    num_cpus: Optional[float] = None,
    num_gpus: Optional[float] = None,
    memory: Optional[float] = None,
    resources: Optional[Dict[str, float]] = None,
    object_store_memory: Optional[int] = None,
    local_mode: bool = False,
    ignore_reinit_error: bool = False,
    logging_level: Any = logging.INFO,
    namespace: Optional[str] = None,
    runtime_env: Optional[Dict[str, Any]] = None,
    include_dashboard: Optional[bool] = None,
    num_nodes: int = 1,
    max_workers_per_node: int = 0,
    worker_idle_timeout_ms: int = 10000,
    enable_object_spilling: bool = True,
    object_spilling_directory: Optional[str] = None,
    temp_dir: Optional[str] = None,
    **kwargs: Any,
) -> "RuntimeContext":
    """启动(或连接)一个 mini-ray 集群。

    常用参数与 Ray 一致(``num_cpus`` / ``num_gpus`` / ``resources`` /
    ``object_store_memory`` / ``local_mode`` / ``namespace`` / ``runtime_env``);

    下面两个是 **mini-ray 特有的模拟参数**(真实 Ray 里节点来自 ``ray start``):

    :param num_nodes: 逻辑节点数。用来观察调度策略、对象本地性、放置组行为。
    :param max_workers_per_node: 每节点 worker 进程上限(0 = 自动)。

    :param worker_idle_timeout_ms: 空闲 worker 多久被回收(Ray 默认 10 秒)。

    :param address: 只支持 ``None``(本地起集群)/ ``"local"`` / ``"auto"``。
        真实 Ray 里 ``address="auto"`` 表示连到已有的集群 —— mini-ray 没有
        跨进程的集群发现,所以明确拒绝其它地址。

    :param include_dashboard: 未实现(会打印一条提示)。Ray 的 dashboard 是
        独立的 web 服务,mini-ray 用 :mod:`miniray.state` 与
        :func:`miniray.timeline` 提供可观测性。
    """
    global _GLOBAL
    with _GLOBAL.lock:
        if address not in (None, "local", "auto", ""):
            raise MiniRayError(
                f"mini-ray 不支持连接已有集群(address={address!r})。"
                "它是「driver 进程内自带 raylet」的实现;"
                "请用 address=None 本地起一个,或者用真实 Ray 做多机部署。"
            )
        if _GLOBAL.core_worker is not None:
            if not ignore_reinit_error:
                raise MiniRayError(
                    "miniray 已经初始化过了。要重新初始化,请先 miniray.shutdown(),"
                    "或者传 ignore_reinit_error=True。"
                )
            shutdown()
        if kwargs:
            logger.warning(
                "miniray.init() 收到未识别的参数(已忽略): %s", ", ".join(sorted(kwargs))
            )
        if include_dashboard:
            logger.warning("mini-ray 没有 dashboard,include_dashboard 被忽略(用 miniray.state 代替)")

        session_dir = tempfile.mkdtemp(prefix="miniray-session-", dir=temp_dir)
        job_id = JobID.from_random().hex()
        namespace = namespace or "default"

        # ---- 1) GCS(元数据)
        gcs_service = GcsService(session_dir=session_dir, job_id=job_id, namespace=namespace)
        gcs_server = RpcServer(gcs_service, name="gcs").start()

        # ---- 2) Raylet(调度 + 对象存储 + worker 池)
        config = RayletConfig(
            job_id=job_id,
            namespace=namespace,
            num_cpus=float(num_cpus) if num_cpus is not None else RayletConfig.detect_cpus(),
            num_gpus=float(num_gpus or 0),
            memory=float(memory or 0.0),
            resources=dict(resources or {}),
            num_nodes=int(num_nodes),
            object_store_memory=int(object_store_memory or _default_store_memory()),
            enable_spilling=enable_object_spilling,
            spill_dir=object_spilling_directory or os.path.join(session_dir, "spill"),
            max_workers_per_node=int(max_workers_per_node),
            worker_idle_timeout_ms=int(worker_idle_timeout_ms or 0),
            local_mode=bool(local_mode),
            session_dir=session_dir,
        )
        raylet = Raylet(config, gcs_server.address)
        raylet_server = RpcServer(raylet, name="raylet").start()
        raylet.address = raylet_server.address
        raylet.start()

        # ---- 3) driver 自己的 CoreWorker
        driver_id = DriverID.from_random().hex()
        core_worker = CoreWorker(
            raylet_address=raylet_server.address,
            gcs_address=gcs_server.address,
            worker_id=driver_id,
            node_id=raylet.node_ids[0],
            job_id=job_id,
            is_driver=True,
            local_mode=bool(local_mode),
            namespace=namespace,
        )
        core_worker._raylet.register_worker(
            worker_id=driver_id,
            node_id=raylet.node_ids[0],
            pid=os.getpid(),
            worker_type="driver",
        )

        _GLOBAL.core_worker = core_worker
        _GLOBAL.raylet = raylet
        _GLOBAL.raylet_server = raylet_server
        _GLOBAL.gcs_service = gcs_service
        _GLOBAL.gcs_server = gcs_server
        _GLOBAL.session_dir = session_dir
        _GLOBAL.job_id = job_id
        _GLOBAL.namespace = namespace
        _GLOBAL.is_driver = True
        _GLOBAL.start_time = time.time()
        _GLOBAL.options = {
            "num_cpus": config.num_cpus,
            "num_gpus": config.num_gpus,
            "num_nodes": config.num_nodes,
            "local_mode": config.local_mode,
            "resources": config.resources,
            "runtime_env": runtime_env or {},
        }
        _configure_logging(logging_level)

    logger.info(
        "mini-ray 已启动: job=%s 节点数=%d CPU=%s GPU=%s raylet=%s%s",
        job_id[:8],
        config.num_nodes,
        config.num_cpus,
        config.num_gpus,
        raylet_server.address,
        "(local_mode)" if config.local_mode else "",
    )
    return RuntimeContext(core_worker)


def _default_store_memory() -> int:
    """默认对象存储容量:取 512MB 与可用内存的 30% 中较小的那个。

    Ray 的默认是「机器内存的 30%」,上限 200GB(``RAY_DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES``)。
    """
    from .object_store import DEFAULT_OBJECT_STORE_BYTES

    try:
        if hasattr(os, "sysconf") and "SC_AVPHYS_PAGES" in os.sysconf_names:
            available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            return int(min(DEFAULT_OBJECT_STORE_BYTES, available * 0.3))
    except Exception:  # pragma: no cover - Windows 上没有 sysconf
        pass
    return DEFAULT_OBJECT_STORE_BYTES


def _configure_logging(level: Any) -> None:
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def shutdown() -> None:
    """关闭集群:停 worker、释放共享内存、关服务。

    顺序很重要(**先 worker 后服务**):worker 会向 raylet 汇报引用计数/退回
    共享内存,如果 raylet 先走了,这些收尾全部报错。
    """
    global _GLOBAL
    with _GLOBAL.lock:
        worker = _GLOBAL.core_worker
        raylet = _GLOBAL.raylet
        raylet_server = _GLOBAL.raylet_server
        gcs_server = _GLOBAL.gcs_server
        session_dir = _GLOBAL.session_dir
        if worker is None and raylet is None:
            return

    if worker is not None:
        try:
            worker.flush_refs()
        except Exception:  # pragma: no cover
            pass
    if raylet is not None:
        try:
            raylet.stop()
        except Exception:  # pragma: no cover
            pass
    if raylet_server is not None:
        raylet_server.stop()
    if gcs_server is not None:
        gcs_server.stop()
    if session_dir and os.path.isdir(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)

    with _GLOBAL.lock:
        _GLOBAL.core_worker = None
        _GLOBAL.raylet = None
        _GLOBAL.raylet_server = None
        _GLOBAL.gcs_service = None
        _GLOBAL.gcs_server = None
        _GLOBAL.session_dir = None
        _GLOBAL.is_driver = False


def is_initialized() -> bool:
    """当前进程是否已经在 Ray 运行时里(driver 或 worker 都算)。"""
    return _GLOBAL.core_worker is not None


def get_core_worker(create: bool = True) -> Optional[CoreWorker]:
    worker = _GLOBAL.core_worker
    if worker is None and create:
        raise MiniRayError(
            "miniray 还没有初始化。请先调用 miniray.init();"
            "如果你在 worker/task 里看到这个错误,说明该对象的 owner 已经退出了。"
        )
    return worker


def set_core_worker(worker: Optional[CoreWorker], *, is_driver: bool = False) -> None:
    """由 worker 进程在启动时登记自己的 CoreWorker。"""
    with _GLOBAL.lock:
        _GLOBAL.core_worker = worker
        _GLOBAL.is_driver = is_driver
        if worker is not None and not _GLOBAL.job_id:
            _GLOBAL.job_id = worker.job_id


def get_raylet() -> Optional[Raylet]:
    return _GLOBAL.raylet


def get_session_dir() -> Optional[str]:
    return _GLOBAL.session_dir


def is_driver() -> bool:
    return _GLOBAL.is_driver
