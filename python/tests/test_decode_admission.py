"""Decode admission: the semaphore bounds concurrent decodes; native codecs serialize.

open_image_for_render bounds decoding two ways (see image_renderer):
  * _DECODE_SEMAPHORE (size from the memory budget) caps *how many* decodes run
    at once, so the classic formats (JPEG/PNG/...) decode in parallel up to what
    RAM allows instead of queuing behind one decode;
  * _NATIVE_DECODE_LOCK additionally serializes the non-thread-safe native codecs
    (HEIF/AVIF/JXL) to at most one at a time.

These use barriers rather than sleeps so they are deterministic: a barrier that
is satisfied proves the threads were genuinely concurrent; one that times out
(``broken``) proves they could not overlap.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hokku.webserver import image_renderer


@pytest.fixture(autouse=True)
def _restore_decode_semaphore():
    """Isolate the process-global semaphore each test mutates."""
    saved = image_renderer._DECODE_SEMAPHORE
    yield
    image_renderer._DECODE_SEMAPHORE = saved


def _decode_in_threads(monkeypatch, suffixes, *, concurrency, barrier):
    """Run one open_image_for_render per suffix, each rendezvousing at ``barrier``
    inside the (faked) decode. Returns the list of exceptions raised per thread."""

    def fake_open(_path):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return object()  # stand-in decoded image; the caller just discards it

    monkeypatch.setattr(image_renderer, "_open_image_for_render", fake_open)
    image_renderer.set_decode_concurrency(concurrency)

    errors: list[Exception] = []

    def run(path):
        try:
            image_renderer.open_image_for_render(path)
        except Exception as e:  # surface any thread failure to the test
            errors.append(e)

    threads = [
        threading.Thread(target=run, args=(Path(f"img{i}{suffix}"),))
        for i, suffix in enumerate(suffixes)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_common_formats_decode_concurrently(monkeypatch):
    # concurrency 3, three PNGs → all three rendezvous at once (barrier satisfied).
    barrier = threading.Barrier(3, timeout=5.0)
    errors = _decode_in_threads(monkeypatch, [".png"] * 3, concurrency=3, barrier=barrier)
    assert not errors
    assert not barrier.broken  # all three decoded simultaneously


def test_concurrency_one_serializes_common_formats(monkeypatch):
    # concurrency 1 → two PNGs cannot overlap; the 2-party barrier never fills.
    barrier = threading.Barrier(2, timeout=1.0)
    _decode_in_threads(monkeypatch, [".png"] * 2, concurrency=1, barrier=barrier)
    assert barrier.broken


def test_native_codecs_cannot_decode_concurrently(monkeypatch):
    # Memory permits 4 concurrent decodes, but the native lock still caps HEIF at
    # one at a time, so a 2-party barrier between two HEIF decodes never fills.
    barrier = threading.Barrier(2, timeout=1.0)
    _decode_in_threads(monkeypatch, [".heic"] * 2, concurrency=4, barrier=barrier)
    assert barrier.broken


def test_native_and_common_can_overlap(monkeypatch):
    # A HEIF and a PNG use different decoders, so only the native lock is
    # exclusive — they may decode at the same time.
    barrier = threading.Barrier(2, timeout=5.0)
    errors = _decode_in_threads(monkeypatch, [".heic", ".png"], concurrency=2, barrier=barrier)
    assert not errors
    assert not barrier.broken


def test_set_decode_concurrency_floors_at_one(monkeypatch):
    # A non-positive value must not crash or unbound the pool — it means 1.
    barrier = threading.Barrier(2, timeout=1.0)
    _decode_in_threads(monkeypatch, [".png"] * 2, concurrency=0, barrier=barrier)
    assert barrier.broken
