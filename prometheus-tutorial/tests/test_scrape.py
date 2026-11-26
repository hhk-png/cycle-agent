from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mini_prometheus.config import parse_config
from mini_prometheus.labels import Labels, Matcher
from mini_prometheus.scrape.manager import HttpResponse, ScrapeManager
from mini_prometheus.util.timeutil import is_stale


def config(targets=None, static_labels=None, **job):
    scrape_job = {
        "job_name": "demo",
        "static_configs": [
            {
                "targets": targets or ["127.0.0.1:9100"],
                "labels": static_labels or {},
            }
        ],
    }
    scrape_job.update(job)
    return parse_config(
        {
            "global": {"scrape_interval": "15s", "scrape_timeout": "5s"},
            "scrape_configs": [scrape_job],
        }
    )


def manager(tsdb, cfg, fetcher, base_dir=".", start_threads=False):
    return ScrapeManager(
        tsdb,
        cfg,
        base_dir=base_dir,
        fetcher=fetcher,
        start_threads=start_threads,
    )


def body(text):
    return HttpResponse(200, {"content-type": "text/plain"}, text.encode())


def scrape(manager_, timestamp=1_700_000_000_000, text=None, target_index=0):
    targets = manager_.targets()
    assert targets, "no discovered targets"
    if text is not None:
        manager_.fetcher = lambda url, headers, timeout: body(text)
    report = manager_.scrape_once(targets[target_index], timestamp=timestamp)
    return report, targets[target_index]


def sample_values(tsdb, name, **labels):
    matchers = [Matcher("__name__", "=", name)] + [
        Matcher(key, "=", value) for key, value in labels.items()
    ]
    return tsdb.select(matchers, 0, 2**62)


def test_static_discovery(tsdb):
    m = manager(tsdb, config(static_labels={"env": "test"}), lambda *a: body(""))
    added, removed = m.sync(now=1_700_000_000_000)
    assert len(added) == 1 and removed == []
    target = added[0]
    assert target.labels["job"] == "demo"
    assert target.labels["instance"] == "127.0.0.1:9100"
    assert target.labels["env"] == "test"
    assert target.url == "http://127.0.0.1:9100/metrics"
    assert target.target_labels().to_dict() == {
        "job": "demo",
        "instance": "127.0.0.1:9100",
        "env": "test",
    }


def test_scrape_success(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    report, target = scrape(m, text="x{a=\"1\"} 5\ny 7\n")
    assert report.status == "success"
    assert report.samples == 2
    assert target.health == "up"
    series = sample_values(tsdb, "x")
    assert len(series) == 1
    labels, samples = series[0]
    assert samples[0].value == 5
    assert labels.get("job") == "demo"
    assert labels.get("instance") == "127.0.0.1:9100"
    assert labels.get("a") == "1"
    up = sample_values(tsdb, "up")[0][1]
    assert up[0].value == 1


def test_scrape_metrics_are_appended(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    scrape(m, text="x 1\ny 2\n")
    for name in (
        "up",
        "scrape_duration_seconds",
        "scrape_samples_scraped",
        "scrape_samples_post_metric_relabeling",
        "scrape_series_added",
        "scrape_body_size_bytes",
        "scrape_timeout_seconds",
    ):
        assert sample_values(tsdb, name), name
    assert sample_values(tsdb, "scrape_samples_scraped")[0][1][0].value == 2


def test_metric_relabel_drop_and_rename(tsdb):
    cfg = config(
        metric_relabel_configs=[
            {"source_labels": ["__name__"], "regex": "drop_me", "action": "drop"},
            {
                "source_labels": ["__name__"],
                "regex": "old_name",
                "target_label": "__name__",
                "replacement": "new_name",
            },
        ]
    )
    m = manager(tsdb, cfg, lambda *a: body(""))
    m.sync(now=0)
    scrape(m, text="drop_me 1\nold_name 2\nkeep 3\n")
    assert sample_values(tsdb, "drop_me") == []
    assert sample_values(tsdb, "new_name")[0][1][0].value == 2
    assert sample_values(tsdb, "keep")[0][1][0].value == 3


def test_honor_labels_false(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    scrape(m, text='x{instance="scraped",job="scraped"} 1\n')
    labels = sample_values(tsdb, "x")[0][0]
    assert labels.get("instance") == "127.0.0.1:9100"
    assert labels.get("exported_instance") == "scraped"
    assert labels.get("job") == "demo"
    assert labels.get("exported_job") == "scraped"


def test_honor_labels_true(tsdb):
    m = manager(tsdb, config(honor_labels=True), lambda *a: body(""))
    m.sync(now=0)
    scrape(m, text='x{instance="scraped"} 1\n')
    labels = sample_values(tsdb, "x")[0][0]
    assert labels.get("instance") == "scraped"
    assert labels.get("job") == "demo"


def test_honor_timestamps_false(tsdb):
    m = manager(tsdb, config(honor_timestamps=False), lambda *a: body(""))
    m.sync(now=0)
    scrape(m, timestamp=1_700_000_000_000, text="x 1 1600000000000\n")
    sample = sample_values(tsdb, "x")[0][1][0]
    assert sample.timestamp == 1_700_000_000_000


def test_explicit_timestamp_is_used(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    scrape(m, timestamp=1_700_000_000_000, text="x 1 1600000000000\n")
    assert sample_values(tsdb, "x")[0][1][0].timestamp == 1_600_000_000_000


def test_stale_marker_when_series_disappears(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    target = m.targets()[0]
    m.fetcher = lambda *a: body("a 1\nb 2\n")
    m.scrape_once(target, timestamp=1000)
    m.fetcher = lambda *a: body("a 3\n")
    m.scrape_once(target, timestamp=2000)
    samples = sample_values(tsdb, "b")[0][1]
    assert len(samples) == 2
    assert samples[-1].timestamp == 2000
    assert is_stale(samples[-1].value)


def test_target_removal_writes_stale(tsdb, tmp_path):
    sd_file = tmp_path / "targets.yml"
    sd_file.write_text("- targets: ['127.0.0.1:9100']\n")
    cfg = parse_config(
        {
            "global": {"scrape_interval": "15s", "scrape_timeout": "5s"},
            "scrape_configs": [
                {
                    "job_name": "demo",
                    "file_sd_configs": [{"files": [str(sd_file)]}],
                }
            ],
        }
    )
    m = manager(tsdb, cfg, lambda *a: body(""))
    m.sync(now=0)
    m.fetcher = lambda *a: body("x 1\n")
    m.scrape_once(m.targets()[0], timestamp=1000)
    sd_file.write_text("- targets: []\n")
    added, removed = m.sync(now=2000)
    assert len(removed) == 1
    samples = sample_values(tsdb, "x")[0][1]
    assert is_stale(samples[-1].value)
    assert samples[-1].timestamp == 2000


def test_failed_scrape_writes_stale_for_previous_series(tsdb):
    m = manager(tsdb, config(), lambda *a: body("x 1\n"))
    m.sync(now=0)
    target = m.targets()[0]
    m.scrape_once(target, timestamp=1000)
    m.fetcher = lambda *a: HttpResponse(500, {}, b"boom")
    m.scrape_once(target, timestamp=2000)
    samples = sample_values(tsdb, "x")[0][1]
    assert is_stale(samples[-1].value)
    assert samples[-1].timestamp == 2000
    assert target.series == set()


def test_http_failure_sets_up_zero(tsdb):
    m = manager(tsdb, config(), lambda *a: HttpResponse(500, {}, b"nope"))
    m.sync(now=0)
    report, target = scrape(m)
    assert report.status == "error"
    assert "500" in report.error
    assert target.health == "down"
    up = sample_values(tsdb, "up")[0][1][0]
    assert up.value == 0


def test_parse_error_sets_up_zero(tsdb):
    m = manager(tsdb, config(), lambda *a: body("not a metric line\n"))
    m.sync(now=0)
    report, target = scrape(m)
    assert report.status == "error"
    assert "parse error" in report.error
    assert sample_values(tsdb, "up")[0][1][0].value == 0


def test_connection_error(tsdb):
    def fail(url, headers, timeout):
        raise TimeoutError("deadline")

    m = manager(tsdb, config(), fail)
    m.sync(now=0)
    report, target = scrape(m)
    assert report.status == "error"
    assert "deadline" in report.error or "timeout" in target.last_error


def test_sample_limit(tsdb):
    cfg = config(sample_limit=1)
    m = manager(tsdb, cfg, lambda *a: body("a 1\nb 2\n"))
    m.sync(now=0)
    report, target = scrape(m)
    assert report.status == "error"
    assert "sample limit" in report.error
    assert sample_values(tsdb, "up")[0][1][0].value == 0


def test_body_size_limit(tsdb):
    cfg = config(body_size_limit=3)
    m = manager(tsdb, cfg, lambda *a: body("x 12345\n"))
    m.sync(now=0)
    report, target = scrape(m)
    assert report.status == "error"
    assert "body size" in report.error


def test_basic_auth_header(tsdb):
    seen = {}
    def fetcher(url, headers, timeout):
        seen.update(headers)
        return body("x 1\n")
    cfg = config(basic_auth={"username": "u", "password": "p"})
    m = manager(tsdb, cfg, fetcher)
    m.sync(now=0)
    scrape(m)
    assert seen["Authorization"].startswith("Basic ")
    import base64
    assert base64.b64decode(seen["Authorization"][6:]).decode() == "u:p"


def test_bearer_token(tsdb):
    seen = {}
    def fetcher(url, headers, timeout):
        seen.update(headers)
        return body("x 1\n")
    m = manager(tsdb, config(bearer_token="secret"), fetcher)
    m.sync(now=0)
    scrape(m)
    assert seen["Authorization"] == "Bearer secret"


def test_params_in_url(tsdb):
    cfg = config(params={"foo": ["bar", "baz"]})
    m = manager(tsdb, cfg, lambda *a: body(""))
    m.sync(now=0)
    assert "foo=bar" in m.targets()[0].url
    assert "foo=baz" in m.targets()[0].url


def test_file_sd(tsdb, tmp_path):
    sd = tmp_path / "sd.yml"
    sd.write_text(
        "- targets: ['10.0.0.1:9100', '10.0.0.2:9100']\n"
        "  labels: {rack: r1}\n"
    )
    cfg = parse_config(
        {
            "global": {"scrape_interval": "15s", "scrape_timeout": "5s"},
            "scrape_configs": [
                {"job_name": "file", "file_sd_configs": [{"files": [str(sd)]}]}
            ],
        }
    )
    m = manager(tsdb, cfg, lambda *a: body(""))
    added, _ = m.sync(now=0)
    assert len(added) == 2
    assert added[0].labels["rack"] == "r1"
    assert added[0].labels["__meta_filepath"] == str(sd)
    sd.write_text("- targets: ['10.0.0.3:9100']\n")
    added, removed = m.sync(now=1)
    assert len(added) == 1 and len(removed) == 2


def test_http_sd(tsdb):
    payload = json.dumps(
        [{"targets": ["10.0.0.1:9100"], "labels": {"zone": "a"}}]
    ).encode()
    cfg = parse_config(
        {
            "global": {"scrape_interval": "15s", "scrape_timeout": "5s"},
            "scrape_configs": [
                {
                    "job_name": "http",
                    "http_sd_configs": [{"url": "http://sd.example/targets"}],
                }
            ],
        }
    )
    m = manager(tsdb, cfg, lambda *a: HttpResponse(200, {}, payload))
    added, _ = m.sync(now=0)
    assert len(added) == 1
    assert added[0].labels["zone"] == "a"
    assert added[0].labels["__meta_url"] == "http://sd.example/targets"


def test_relabel_keep_and_drop(tsdb):
    cfg = config(
        relabel_configs=[
            {"source_labels": ["__address__"], "regex": "127.0.0.1:9100", "action": "keep"},
        ]
    )
    m = manager(tsdb, cfg, lambda *a: body(""))
    assert len(m.sync(now=0)[0]) == 1
    cfg = config(
        relabel_configs=[
            {"source_labels": ["__address__"], "regex": "127.0.0.1:9100", "action": "drop"},
        ]
    )
    m = manager(tsdb, cfg, lambda *a: body(""))
    assert len(m.sync(now=0)[0]) == 0


def test_relabel_replace_instance(tsdb):
    cfg = config(
        relabel_configs=[
            {
                "source_labels": ["__address__"],
                "regex": "([^:]+):.*",
                "target_label": "instance",
                "replacement": "$1",
            }
        ]
    )
    m = manager(tsdb, cfg, lambda *a: body(""))
    target = m.sync(now=0)[0][0]
    assert target.labels["instance"] == "127.0.0.1"


def test_duplicate_timestamp_across_scrapes(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    target = m.targets()[0]
    m.fetcher = lambda *a: body("x 1 1700000000000\n")
    m.scrape_once(target, timestamp=1000)
    m.fetcher = lambda *a: body("x 2 1700000000000\n")
    report = m.scrape_once(target, timestamp=2000)
    # The scrape succeeded; only the duplicate sample was rejected.
    assert report.status == "success"
    assert target.health == "up"
    assert sample_values(tsdb, "up")[0][1][-1].value == 1


def test_openmetrics_exemplar(tsdb):
    text = (
        "# TYPE x_bucket histogram\n"
        'x_bucket{le="1"} 5 # {trace_id="abc"} 4.2 1600000000000\n'
        'x_bucket{le="+Inf"} 5\n# EOF\n'
    )
    m = manager(tsdb, config(), lambda *a: body(text))
    m.sync(now=0)
    scrape(m)
    target = m.targets()[0]
    assert len(target.exemplars) == 1
    labels, exemplar = target.exemplars[0]
    assert labels.get("__name__") == "x_bucket"
    assert exemplar.labels.get("trace_id") == "abc"


def test_empty_scrape_is_up(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    report, target = scrape(m)
    assert report.status == "success"
    assert target.health == "up"
    assert sample_values(tsdb, "up")[0][1][0].value == 1


def test_metrics_endpoint_text(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    scrape(m)
    text = m.metrics()
    assert "prometheus_target_scrapes_total" in text
    assert "up{" in text


def test_unknown_target(tsdb):
    m = manager(tsdb, config(), lambda *a: body(""))
    m.sync(now=0)
    with pytest.raises(KeyError):
        m.scrape_once("missing")
