from __future__ import annotations

import json
import random
import struct

import pytest

from mini_prometheus.labels import Labels
from mini_prometheus.remote import (
    decode_write_request,
    encode_write_request,
    parse_protobuf,
    snappy_compress,
    snappy_decompress,
)


def test_snappy_literal_roundtrip():
    for data in (b"", b"a", b"abc" * 100, bytes(range(256)) * 4):
        assert snappy_decompress(snappy_compress(data)) == data


def test_snappy_random_roundtrip():
    rng = random.Random(11)
    for _ in range(50):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 2000)))
        assert snappy_decompress(snappy_compress(data)) == data


def test_snappy_copy_type_one():
    # "abc" + copy(offset=3, length=6) => "abcabcabc"
    raw = bytes([9, 8, ord("a"), ord("b"), ord("c"), (2 << 2) | 1, 3])
    assert snappy_decompress(raw) == b"abcabcabc"


def test_snappy_copy_type_two():
    # literal "ab", then copy len 4 offset 2 (tag type 2, 2-byte offset)
    raw = bytes([6, (1 << 2), ord("a"), ord("b"), ((4 - 1) << 2) | 2, 2, 0])
    assert snappy_decompress(raw) == b"ababab"


def test_snappy_invalid_offset():
    raw = bytes([5, (0 << 2), ord("a"), ((4 - 1) << 2) | 2, 0, 9])
    with pytest.raises(ValueError):
        snappy_decompress(raw)


def test_snappy_length_mismatch():
    raw = bytes([99, (2 << 2), ord("a"), ord("b"), ord("c")])
    with pytest.raises(ValueError):
        snappy_decompress(raw)


def test_protobuf_fields():
    # field 1 varint 150, field 2 length-delimited "hi"
    data = bytes([0x08, 0x96, 0x01, 0x12, 0x02, ord("h"), ord("i")])
    fields = parse_protobuf(data)
    assert fields[0].number == 1 and fields[0].value == 150
    assert fields[1].number == 2 and fields[1].value == b"hi"


def test_encode_decode_write_request():
    series = [
        (
            Labels({"__name__": "http_requests_total", "job": "api"}),
            [(1_700_000_000_000, 10.0), (1_700_000_015_000, 12.0)],
        ),
        (Labels({"__name__": "up", "job": "api"}), [(1_700_000_000_000, 1.0)]),
    ]
    encoded = encode_write_request(series)
    decoded = decode_write_request(snappy_compress(encoded))
    assert decoded == [
        (series[0][0], 1_700_000_000_000, 10.0),
        (series[0][0], 1_700_000_015_000, 12.0),
        (series[1][0], 1_700_000_000_000, 1.0),
    ]


def test_decode_identity_encoding():
    encoded = encode_write_request(
        [(Labels({"__name__": "x"}), [(1000, 1.5)])]
    )
    assert decode_write_request(encoded, content_encoding="identity") == [
        (Labels({"__name__": "x"}), 1000, 1.5)
    ]


def test_decode_unsupported_encoding():
    with pytest.raises(ValueError):
        decode_write_request(b"", content_encoding="gzip")


def test_decode_skips_series_without_name():
    encoded = encode_write_request([(Labels({"job": "api"}), [(1000, 1.0)])])
    assert decode_write_request(snappy_compress(encoded)) == []


def test_native_histogram_rejected():
    # Handcraft a TimeSeries with field 4 (histogram) present.
    # WriteRequest { timeseries { labels, histograms } }
    from mini_prometheus.remote import _encode_varint, _ld, _tag

    label = _tag(1, 2) + _ld(b"__name__") + _tag(2, 2) + _ld(b"x")
    histogram = b""
    timeseries = (
        _tag(1, 2) + _ld(label)
        + _tag(4, 2) + _ld(histogram)
    )
    request = _tag(1, 2) + _ld(timeseries)
    with pytest.raises(ValueError):
        decode_write_request(snappy_compress(request))


def test_negative_timestamp_roundtrip():
    encoded = encode_write_request([(Labels({"__name__": "x"}), [(-1000, 1.0)])])
    decoded = decode_write_request(snappy_compress(encoded))
    assert decoded[0][1] == -1000


def test_remote_writer_json_batches():
    from mini_prometheus.config import RemoteWriteConfig
    from mini_prometheus.remote_client import RemoteWriter

    sent = []

    def sender(url, headers, body, timeout):
        sent.append((url, headers, json.loads(body)))
        return 204

    writer = RemoteWriter(
        [RemoteWriteConfig(url="http://sink/api/v1/write", batch_size=10)],
        sender=sender,
        start_worker=False,
    )
    writer.observe(
        [
            (Labels({"__name__": "x", "job": "a"}), 1000, 1.0),
            (Labels({"__name__": "x", "job": "a"}), 2000, 2.0),
        ]
    )
    writer.flush()
    assert sent[0][2]["timeseries"][0]["samples"] == [[1000, 1.0], [2000, 2.0]]


def test_remote_writer_protobuf():
    from mini_prometheus.config import RemoteWriteConfig
    from mini_prometheus.remote_client import RemoteWriter

    bodies = []

    def sender(url, headers, body, timeout):
        bodies.append((headers, body))
        return 204

    writer = RemoteWriter(
        [
            RemoteWriteConfig(
                url="http://sink/api/v1/write",
                protocol="prometheus",
                batch_size=1,
            )
        ],
        sender=sender,
        start_worker=False,
    )
    writer.observe([(Labels({"__name__": "x"}), 1000, 1.0)])
    writer.flush()
    headers, body = bodies[0]
    assert headers["Content-Encoding"] == "snappy"
    assert decode_write_request(body) == [(Labels({"__name__": "x"}), 1000, 1.0)]


def test_remote_writer_drops_4xx():
    from mini_prometheus.config import RemoteWriteConfig
    from mini_prometheus.remote_client import RemoteWriter

    writer = RemoteWriter(
        [RemoteWriteConfig(url="http://sink/api/v1/write", batch_size=1)],
        sender=lambda *args: 400,
        start_worker=False,
    )
    writer.observe([(Labels({"__name__": "x"}), 1000, 1.0)])
    writer.flush()
    assert writer.stats["dropped"] == 1


def test_tsdb_notifies_watchers(tmp_path):
    from mini_prometheus.tsdb import TSDB, TSDBOptions

    seen = []
    database = TSDB(
        TSDBOptions(path=str(tmp_path / "data"), use_lockfile=False)
    ).open()
    database.add_watcher(lambda samples: seen.extend(samples))
    database.append(Labels({"__name__": "x"}), 1000, 1.0)
    database.append_batch([(Labels({"__name__": "y"}), 2000, 2.0)])
    assert [labels.get_name() for labels, _, _ in seen] == ["x", "y"]
    database.close()
