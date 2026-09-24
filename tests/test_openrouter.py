"""The stoppable OpenRouter call (parsing/openrouter.py).

A plain request could only be abandoned: OpenRouter's keep-alive lines
kept a read timeout from ever firing, and a model left running still
billed. Streamed, the call is dropped at the deadline or on Start over,
and whatever it did return still reads like a Response."""
import json
import threading
import time

import pytest

from propresenterrunsheet.parsing.models import provider_failure
from propresenterrunsheet.parsing.openrouter import Stopped, chat

KEEP_ALIVE = b": OPENROUTER PROCESSING"


class Stream:
    """A streamed OpenRouter reply: `lines` arrive `gap` seconds apart."""
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, lines, gap=0.0):
        self.lines, self.gap, self.closed = lines, gap, False

    def iter_lines(self):
        for line in self.lines:
            if self.closed:
                return
            time.sleep(self.gap)
            yield line

    def close(self):
        self.closed = True


def data(chunk):
    return b"data: " + json.dumps(chunk).encode()


def poster(resp, sent=None):
    def post(url, headers=None, json=None, timeout=None, stream=False):
        if sent is not None:
            sent.append({"json": json, "stream": stream})
        return resp
    return post


def ask(post, timeout_s=5, stop=None):
    return chat(post, "https://openrouter.test", headers={}, timeout_s=timeout_s,
                body={"model": "m", "messages": []}, stop=stop)


def test_a_stream_reads_as_one_reply():
    sent = []
    reply = ask(poster(Stream([
        KEEP_ALIVE, b"",
        data({"model": "openai/gpt-4.1-mini",
              "choices": [{"delta": {"role": "assistant", "content": '{"items"'}}]}),
        data({"choices": [{"delta": {"content": ": []}"}}]}),
        data({"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
              "usage": {"cost": 0.0012}}),
        b"data: [DONE]"]), sent))
    assert reply.status_code == 200 and reply.ok
    assert reply.json() == {"model": "openai/gpt-4.1-mini", "usage": {"cost": 0.0012},
                            "choices": [{"message": {"content": '{"items": []}'}}]}
    assert sent[0]["stream"] is True and sent[0]["json"]["stream"] is True


def test_an_error_mid_stream_reads_as_the_providers_failure():
    """Seen live as HTTP 200 with an error: the parse retries elsewhere."""
    reply = ask(poster(Stream([data({"error": {
        "code": 503, "message": "Upstream error from Nvidia: overloaded"},
        "choices": [{"delta": {}, "finish_reason": "error"}]})])))
    assert "choices" not in reply.json()
    assert provider_failure(reply)["provider"] == "Nvidia"


def test_an_error_status_comes_back_as_it_was_sent():
    class Refused:
        status_code, headers = 429, {"Content-Type": "application/json"}

        def json(self):
            return {"error": {"code": 429}}

    refused = Refused()
    assert ask(poster(refused)) is refused


def test_a_model_still_thinking_at_the_deadline_is_dropped():
    """Keep-alive lines forever, as OpenRouter sends while a model works."""
    stream = Stream([KEEP_ALIVE] * 1000, gap=0.05)
    t = time.monotonic()
    with pytest.raises(Stopped) as stopped:
        ask(poster(stream), timeout_s=0.4)
    assert stopped.value.reason == "timeout"
    assert time.monotonic() - t < 1.0 and stream.closed


def test_start_over_drops_the_call_at_once():
    stream, stop = Stream([KEEP_ALIVE] * 1000, gap=0.05), threading.Event()
    threading.Timer(0.3, stop.set).start()
    t = time.monotonic()
    with pytest.raises(Stopped) as stopped:
        ask(poster(stream), stop=stop)
    assert stopped.value.reason == "cancelled"
    assert time.monotonic() - t < 0.8 and stream.closed


def test_no_time_left_means_no_call():
    def post(*_a, **_k):
        raise AssertionError("must not start a call it can't wait for")
    with pytest.raises(Stopped):
        ask(post, timeout_s=0)
