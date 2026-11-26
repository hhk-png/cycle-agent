from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from mini_rabbitmq import Broker
from mini_rabbitmq.client import AMQPClient
from mini_rabbitmq.server import AMQPServer


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = Path(__file__).resolve().parent / "_tmp"


@pytest.fixture(scope="session", autouse=True)
def clean_test_tmp():
    if TEST_TMP.exists():
        shutil.rmtree(TEST_TMP)
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    yield
    if TEST_TMP.exists():
        shutil.rmtree(TEST_TMP)


@pytest.fixture
def local_tmp():
    path = TEST_TMP / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def broker():
    instance = Broker()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def server(broker):
    instance = AMQPServer(broker, port=0)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def client(server):
    instance = AMQPClient(port=server.port)
    instance.connect()
    try:
        yield instance
    finally:
        instance.close()
