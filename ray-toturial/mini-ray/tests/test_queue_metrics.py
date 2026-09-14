"""``ray.util.queue.Queue`` 与 ``ray.util.metrics`` 的测试。

这两个模块是 mini-ray 里**唯一的两个"通信/监控"原语**,它们的价值不在
"能不能跑",而在**边界语义**:

* 队列:满了会怎样、空了会怎样、超时抛什么、多个消费者会不会拿到同一条;
* 指标:重复注册会不会静默、标签没声明会怎样、打点能不能跨线程。

所以下面每个测试都在验一个**契约**,而不是"跑通了"。
"""

from __future__ import annotations

import queue as queue_lib
import threading
import time

import pytest

import miniray as ray
from miniray.util import metrics
from miniray.util.queue import Empty, Full, Queue


@pytest.fixture()
def clean_metrics():
    """指标注册表是**进程全局**的,测试之间必须清干净。"""
    metrics.reset()
    yield metrics
    metrics.reset()


# ===========================================================================
# Queue
# ===========================================================================


def test_queue_put_get_basic(cluster):
    q = Queue()
    try:
        q.put("hello")
        assert q.get() == "hello"
    finally:
        q.shutdown()


def test_queue_fifo_order(cluster):
    q = Queue()
    try:
        for i in range(20):
            q.put(i)
        assert [q.get() for _ in range(20)] == list(range(20))
    finally:
        q.shutdown()


def test_queue_qsize_empty_full(cluster):
    q = Queue(maxsize=3)
    try:
        assert q.empty() is True
        assert q.qsize() == 0
        assert q.full() is False

        q.put(1)
        q.put(2)
        assert q.qsize() == 2
        assert q.empty() is False
        assert len(q) == 2

        q.put(3)
        assert q.full() is True
        assert q.qsize() == 3
    finally:
        q.shutdown()


def test_queue_unbounded_never_full(cluster):
    """``maxsize=0`` 表示无上限 —— ``full()`` 必须恒为 False。"""
    q = Queue()
    try:
        for i in range(50):
            q.put(i)
        assert q.full() is False
        assert q.qsize() == 50
    finally:
        q.shutdown()


def test_queue_get_nowait_raises_empty(cluster):
    q = Queue()
    try:
        with pytest.raises(Empty):
            q.get(block=False)
    finally:
        q.shutdown()


def test_queue_put_nowait_raises_full(cluster):
    q = Queue(maxsize=1)
    try:
        q.put(1)
        with pytest.raises(Full):
            q.put(2, block=False)
    finally:
        q.shutdown()


def test_queue_get_timeout_raises_empty(cluster):
    q = Queue()
    try:
        start = time.time()
        with pytest.raises(Empty):
            q.get(timeout=0.3)
        elapsed = time.time() - start
        # 必须真的等了(而不是立刻抛),但也不能超太多
        assert 0.2 < elapsed < 3.0
    finally:
        q.shutdown()


def test_queue_put_timeout_raises_full(cluster):
    q = Queue(maxsize=1)
    try:
        q.put(1)
        with pytest.raises(Full):
            q.put(2, timeout=0.3)
    finally:
        q.shutdown()


def test_queue_empty_and_full_are_stdlib_compatible(cluster):
    """``Empty``/``Full`` 就是标准库的那两个 —— ``except queue.Empty`` 必须能兜住。"""
    assert Empty is queue_lib.Empty
    assert Full is queue_lib.Full

    q = Queue(maxsize=1)
    try:
        with pytest.raises(queue_lib.Empty):
            q.get(block=False)
        q.put(1)
        with pytest.raises(queue_lib.Full):
            q.put(2, block=False)
    finally:
        q.shutdown()


def test_queue_blocks_until_producer_puts(cluster):
    """消费者先到、生产者后到 —— ``get()`` 必须挂住而不是返回空。"""
    q = Queue()

    @ray.remote
    def consume(q):
        return q.get()

    @ray.remote
    def produce_later(q, delay, value):
        time.sleep(delay)
        q.put(value)
        return "produced"

    try:
        consumer_ref = consume.remote(q)          # 立刻提交,此时队列是空的
        time.sleep(0.2)                            # 确保消费者真的已经在等
        produce_ref = produce_later.remote(q, 0.1, "payload")

        assert ray.get(produce_ref) == "produced"
        assert ray.get(consumer_ref) == "payload"
    finally:
        q.shutdown()


def test_queue_worked_as_argument_and_shared(cluster):
    """队列可以当参数传给任务 —— 这是它比 ObjectRef 强的地方。"""
    q = Queue()

    @ray.remote
    def push(q, value):
        q.put(value)
        return True

    @ray.remote
    def pop(q):
        return q.get()

    try:
        ray.get([push.remote(q, i) for i in range(5)])
        assert sorted(ray.get([pop.remote(q) for _ in range(5)])) == [0, 1, 2, 3, 4]
    finally:
        q.shutdown()


def test_queue_multiple_consumers_no_duplicates(cluster):
    """N 个消费者抢 M 条消息:每条**恰好**被取走一次,不重不漏。"""
    q = Queue()
    n_items = 40

    @ray.remote
    def consume_until(q, stop_value):
        out = []
        while True:
            item = q.get()
            if item == stop_value:
                return out
            out.append(item)

    try:
        for i in range(n_items):
            q.put(i)
        # 用哨兵值告诉每个消费者"结束了"(不能靠 qsize(),那是竞态的)
        for _ in range(4):
            q.put(-1)

        batches = ray.get([consume_until.remote(q, -1) for _ in range(4)])
        merged = sorted(x for batch in batches for x in batch)
        assert merged == list(range(n_items))     # 不重不漏
    finally:
        q.shutdown()


def test_queue_backpressure_bounds_memory(cluster):
    """带 ``maxsize`` 的队列会**阻塞生产者**,这正是背压的载体。

    生产者想塞 30 个、队列上限 3、消费者很慢 ——
    生产者**不可能**一次全塞进去,队列长度始终 <= maxsize。
    """
    q = Queue(maxsize=3)

    @ray.remote
    def slow_produce(q, n):
        for i in range(n):
            q.put(i)          # 第 4 个就会挂住,直到消费者取走
        return n

    @ray.remote
    def slow_consume(q, n):
        sizes = []
        for _ in range(n):
            time.sleep(0.01)
            q.get()
            sizes.append(q.qsize())
        return sizes

    try:
        producer = slow_produce.remote(q, 30)
        consumer = slow_consume.remote(q, 30)
        sizes = ray.get(consumer)
        assert ray.get(producer) == 30
        assert max(sizes) <= 3, f"队列长度超过了 maxsize: {max(sizes)}"
    finally:
        q.shutdown()


def test_queue_shutdown_kills_actor(cluster):
    """``shutdown()`` 的语义是**把内部 actor 杀掉**(与真实 Ray 一致)。

    杀掉之后再调用队列,拿到的应该是 ``RayActorError`` ——
    而不是静默成功、或者永远挂住。
    """
    q = Queue()
    q.put(1)
    assert q.get() == 1

    q.shutdown()

    with pytest.raises(ray.RayActorError):
        q.get(timeout=5)


def test_queue_repr_and_actor_handle(cluster):
    q = Queue(maxsize=7)
    try:
        assert "maxsize=7" in repr(q)
        assert q.actor is not None
    finally:
        q.shutdown()


# ===========================================================================
# metrics
# ===========================================================================


def test_counter_basic(clean_metrics):
    counter = clean_metrics.Counter("test_requests_total", description="请求数")
    assert counter.get() == 0.0
    counter.inc()
    counter.inc()
    counter.inc(5)
    assert counter.get() == 7.0


def test_counter_with_tags(clean_metrics):
    counter = clean_metrics.Counter("test_errors_total", tag_keys=("kind",))
    counter.inc(tags={"kind": "timeout"})
    counter.inc(3, tags={"kind": "timeout"})
    counter.inc(tags={"kind": "oom"})

    assert counter.get(tags={"kind": "timeout"}) == 4.0
    assert counter.get(tags={"kind": "oom"}) == 1.0
    assert counter.get(tags={"kind": "other"}) == 0.0


def test_counter_undeclared_tag_raises(clean_metrics):
    counter = clean_metrics.Counter("test_strict_total", tag_keys=("kind",))
    with pytest.raises(ray.MiniRayError, match="未声明"):
        counter.inc(tags={"kind": "a", "unexpected": "b"})


def test_counter_filters_extra_tags_when_undeclared(clean_metrics):
    """没声明 tag_keys 时,标签照收 —— 但顺序不同不该产生两条时间线。"""
    counter = clean_metrics.Counter("test_loose_total")
    counter.inc(tags={"a": 1, "b": 2})
    counter.inc(tags={"b": 2, "a": 1})          # 顺序颠倒
    assert sum(counter.snapshot().values()) == 2.0
    assert len(counter.snapshot()) == 1


def test_gauge_set_and_overwrite(clean_metrics):
    gauge = clean_metrics.Gauge("test_queue_depth")
    assert gauge.get() == 0.0
    gauge.set(42)
    assert gauge.get() == 42.0
    gauge.set(7)                                 # Gauge 是覆盖,不是累加
    assert gauge.get() == 7.0


def test_histogram_buckets(clean_metrics):
    hist = clean_metrics.Histogram("test_latency_ms", boundaries=[10, 20, 30])
    # 桶: (-inf,10) [10,20) [20,30) [30,+inf)
    for value in [5, 15, 15, 25, 100]:
        hist.observe(value)

    snap = hist.snapshot()[()]
    assert snap["counts"] == [1, 2, 1, 1]
    assert snap["total"] == 5
    assert snap["sum"] == pytest.approx(160.0)
    assert hist.count() == 5
    assert hist.mean() == pytest.approx(32.0)


def test_histogram_quantile_is_approximate(clean_metrics):
    """分位数按桶插值 —— 这是所有基于直方图的监控系统的共同行为。"""
    hist = clean_metrics.Histogram("test_q_ms", boundaries=[10, 20, 30])
    for value in [5, 15, 15, 25, 100]:
        hist.observe(value)

    # 中位数落在第 2 个桶 [10,20) 内,插值结果必在桶内
    median = hist.quantile(0.5)
    assert 10.0 <= median <= 20.0

    # q=1.0 落在 +Inf 桶 → 返回该桶下界(30),而不是无穷大
    assert hist.quantile(1.0) == 30.0

    # 空直方图
    empty = clean_metrics.Histogram("test_empty_q")
    assert empty.quantile(0.9) == 0.0


def test_histogram_timer_measures_milliseconds(clean_metrics):
    hist = clean_metrics.Histogram("test_timer_ms", boundaries=[1, 10, 100, 1000])
    with hist.timer():
        time.sleep(0.05)                          # 50ms

    assert hist.count() == 1
    assert 30.0 < hist.mean() < 500.0, hist.mean()


def test_histogram_timer_records_on_exception(clean_metrics):
    """``timer()`` 用 finally —— 抛异常的那次也要被记下来(否则监控会漏掉最慢的请求)。"""
    hist = clean_metrics.Histogram("test_timer_exc", boundaries=[1000, 2000])

    with pytest.raises(ValueError):
        with hist.timer():
            raise ValueError("boom")

    assert hist.count() == 1


def test_histogram_bad_boundaries(clean_metrics):
    with pytest.raises(ray.MiniRayError, match="升序"):
        clean_metrics.Histogram("test_bad_bounds", boundaries=[10, 5])


def test_metric_name_validation(clean_metrics):
    with pytest.raises(ray.MiniRayError, match="非法字符"):
        clean_metrics.Counter("bad name!", description="空格和感叹号都不行")
    with pytest.raises(ray.MiniRayError, match="数字开头"):
        clean_metrics.Counter("1_bad")
    with pytest.raises(ray.MiniRayError):
        clean_metrics.Counter("")


def test_duplicate_registration_raises(clean_metrics):
    """重复注册必须**显式报错** —— 静默接受会造成"打点了但看不到"的事故。"""
    clean_metrics.Counter("test_dup_total")
    with pytest.raises(ray.MiniRayError, match="已经注册过"):
        clean_metrics.Counter("test_dup_total")


def test_snapshot_exports_all_metrics(clean_metrics):
    clean_metrics.Counter("test_snap_counter").inc(3)
    clean_metrics.Gauge("test_snap_gauge").set(9)

    snap = clean_metrics.snapshot()
    assert "Counter:test_snap_counter" in snap
    assert "Gauge:test_snap_gauge" in snap
    assert snap["Counter:test_snap_counter"][()] == 3.0


def test_metrics_are_thread_safe(clean_metrics):
    """打点是热路径,而 worker 是多线程的 —— 并发 inc 不能丢计数。"""
    counter = clean_metrics.Counter("test_threaded_total")
    threads = []

    def worker():
        for _ in range(1000):
            counter.inc()

    for _ in range(8):
        thread = threading.Thread(target=worker)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()

    assert counter.get() == 8000.0


def test_metrics_inside_task_and_actor(cluster):
    """指标要能在**任务和 actor 里**用 —— 那才是打点的地方。

    注意两个坑:

    1. 指标注册表是**进程内**的,所以在 worker 里打点,driver 的 ``snapshot()``
       看不到。这里让 worker 自己把结果带回来。
    2. **不要在任务外层 import 模块再靠闭包引用它** —— 闭包会被 cloudpickle
       按值序列化,而模块对象是不可序列化的。在函数体里 import 就没有这个问题。
    """

    @ray.remote
    def task_with_metrics():
        from miniray.util import metrics as m

        m.reset()
        counter = m.Counter("task_hits_total", tag_keys=("shard",))
        counter.inc(tags={"shard": "a"})
        counter.inc(2, tags={"shard": "a"})
        return counter.get(tags={"shard": "a"})

    @ray.remote
    class MetricActor:
        def __init__(self):
            from miniray.util import metrics as m

            m.reset()
            self.hist = m.Histogram("actor_ms", boundaries=[10, 100])
            self.gauge = m.Gauge("actor_pending")

        def work(self, value):
            self.hist.observe(value)
            self.gauge.set(value)
            return self.hist.count()

    assert ray.get(task_with_metrics.remote()) == 3.0

    actor = MetricActor.remote()
    try:
        assert ray.get([actor.work.remote(v) for v in (5, 50, 500)]) == [1, 2, 3]
    finally:
        ray.kill(actor)
