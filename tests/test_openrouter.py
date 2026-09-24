"""The stoppable OpenRouter call (parsing/openrouter.py).

A plain request could only be abandoned: OpenRouter's keep-alive lines
kept a read timeout from ever firing, and a model left running still
billed. Streamed, the call is dropped at the deadline or on Start over,
and whatever it did return still reads like a Response."""
import contextlib
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


def hung_up(stream, within=1.0):
    """The worker hangs up just after Stopped is raised, not before."""
    end = time.monotonic() + within
    while not stream.closed and time.monotonic() < end:
        time.sleep(0.01)
    return stream.closed


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
    assert time.monotonic() - t < 1.0 and hung_up(stream)


def test_start_over_drops_the_call_at_once():
    stream, stop = Stream([KEEP_ALIVE] * 1000, gap=0.05), threading.Event()
    threading.Timer(0.3, stop.set).start()
    t = time.monotonic()
    with pytest.raises(Stopped) as stopped:
        ask(poster(stream), stop=stop)
    assert stopped.value.reason == "cancelled"
    assert time.monotonic() - t < 0.8 and hung_up(stream)


def test_no_time_left_or_already_stopped_means_no_call():
    """Start over during OCR or the template fetch: the runsheet must not
    go to OpenRouter at all."""
    def post(*_a, **_k):
        raise AssertionError("must not send a call nobody will wait for")
    with pytest.raises(Stopped):
        ask(post, timeout_s=0)
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(Stopped) as why:
        ask(post, stop=stopped)
    assert why.value.reason == "cancelled"


# How the reply is framed decides who holds the socket: OpenRouter's own
# (chunked, kept alive) leaves it with the connection; the others hand it
# to the response, as a proxy that reframes the reply would.
FRAMINGS = {
    "chunked": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n",
    "chunked, close": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n",
    "no length": b"HTTP/1.1 200 OK\r\n",
    "HTTP/1.0": b"HTTP/1.0 200 OK\r\n",
}


@pytest.fixture(params=FRAMINGS)
def stalled_server(request):
    """A real HTTP server on this machine: it starts an event stream, sends
    one keep-alive, then goes quiet, as a model stuck thinking does. Gives
    its URL and an Event set once the client hangs up."""
    import socket

    listener = socket.create_server(("127.0.0.1", 0))
    hung_up = threading.Event()
    head = FRAMINGS[request.param] + b"Content-Type: text/event-stream\r\n\r\n"
    line = b": OPENROUTER PROCESSING\n\n"
    first = b"%x\r\n%s\r\n" % (len(line), line) if b"chunked" in head else line

    def serve():
        conn, _ = listener.accept()
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            conn.sendall(head + first)
            conn.settimeout(10)
            # A reset counts as a hang-up, as a clean close does.
            with contextlib.suppress(OSError):
                while conn.recv(4096):      # the request body, then silence
                    pass
            hung_up.set()

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}/api/v1/chat/completions", hung_up
    listener.close()


def test_start_over_drops_a_stalled_real_stream_at_once(stalled_server):
    """With the real HTTP stack, closing the response from the waiting
    thread blocked until the next byte: the call ran on to the read
    timeout (review before merging, Sept 2026). Now it stops at once, and
    the connection is really hung up — that is what stops the billing."""
    import requests

    url, hung_up = stalled_server
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    t = time.monotonic()
    with pytest.raises(Stopped):
        chat(requests.post, url, headers={}, body={"model": "m"},
             timeout_s=30, stop=stop)
    assert time.monotonic() - t < 1.5
    assert hung_up.wait(3), "the server never saw the connection close"


def test_hanging_up_never_raises_in_place_of_stopped():
    """Through an https:// proxy the socket is urllib3's SSLTransport, which
    has no shutdown of its own; the socket it wraps does. And a socket
    already closed raises — neither may replace the Stopped."""
    import socket
    from types import SimpleNamespace as NS
    from propresenterrunsheet.parsing.openrouter import _wake

    downs = []

    class Wrapped:
        def shutdown(self, how):
            downs.append(how)

    class Closed:
        def shutdown(self, how):
            raise OSError("already closed")

    def response(sock):
        return NS(raw=NS(connection=NS(sock=sock)))

    _wake(response(NS(socket=Wrapped())))          # SSLTransport's shape
    assert downs == [socket.SHUT_RDWR]
    for resp in (response(Closed()), response(None), NS(raw=None), None, Stream([])):
        _wake(resp)
