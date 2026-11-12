"""Copy files from the NBDC source's HTTPS server into S3, rate-limiting the source only.

This replaced rclone in HTTPS ingress (2026-11-12). rclone's `--tpslimit` is one
token bucket per process, and its S3 backend draws from the same bucket, so the
limit MSI's load needed (4 requests/s per copy) also throttled our uploads: with
16 MiB parts a copy ran at ~44 MiB/s, and with 128 MiB parts at ~135 MiB/s,
against ~227 MiB/s unthrottled. Here the limiter is called before each GET to the
source and nowhere else, so S3 never spends it.

What MSI sees is fixed by two numbers and nothing else:

- `SOURCE_CONCURRENCY` worker threads, each holding at most one GET open. A file is
  one GET, never a ranged multi-stream read, so this is the cap on concurrent GETs.
- `SOURCE_REQUESTS_PER_SEC`, a token bucket with a burst of 1 (rclone's default), so
  requests are evenly spaced. Every GET attempt, retries included, takes a token.

Each GET's body streams straight into a boto3 multipart upload. Nothing is written
to disk. A body that ends short of its listed size fails the file (urllib3 enforces
Content-Length, and the byte count is checked again here), and the object is
deleted, so the caller's size check sees it as missing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from boto3.s3.transfer import TransferConfig

#: Concurrent GETs per copy. Times the 2 https-transfer slots, this is our whole
#: concurrent load on the NBDC source: the operator's ceiling is 8 (2026-11-10,
#: after MSI asked us to reduce load). tests/argo/test_nbdc_concurrency.py holds
#: the product to 8.
SOURCE_CONCURRENCY = 4

#: Requests per second per copy, on the source only. Concurrency alone does not bound
#: load when files are tiny: 4 workers on ~2 KB files could send dozens a second, and
#: MSI's httpd workers lock up under that (their reply, 2026-11-10). Times the 2
#: slots, at most 8 requests/s on the source.
SOURCE_REQUESTS_PER_SEC = 4.0

#: Attempts per file within one pass. The caller's passes retry the rest after a
#: wait, so this only absorbs the source's isolated 500s and dropped connections.
#: Every attempt takes a token.
ATTEMPTS_PER_FILE = 3
RETRY_SLEEP_SECS = 2.0

#: (connect, read) timeouts. The read timeout is per socket read, not per file: a
#: 500 MiB BOLD run streams for many seconds, each read arriving promptly. Five
#: minutes matches rclone's `--timeout`, which never fired in production.
HTTP_TIMEOUT = (30, 300)

#: S3 part size and parts held in memory per file. A non-seekable stream buffers
#: up to max_in_memory_upload_chunks parts, so 64 MiB x 4 x SOURCE_CONCURRENCY 4 =
#: 1 GiB in flight, inside the step's 3Gi limit. S3 is not rate-limited, so the part
#: count costs nothing on the source.
S3_PART_BYTES = 64 * 2**20
S3_PARTS_IN_MEMORY = 4
S3_PART_UPLOADS_PER_FILE = 4

PROGRESS_EVERY_SECS = 30.0

#: Statuses worth another attempt. Anything else (401, 403, 404) fails at once.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

#: Errors from a JSON body are logged, capped, because they carry the request ID
#: the source's operators ask for. rclone discarded the body.
ERROR_BODY_CHARS = 500


class RateLimiter:
    """At most `rate` acquisitions per second, evenly spaced (a burst of 1).

    Thread-safe. Each caller reserves the next free slot under the lock and sleeps
    outside it, so a waiting thread never blocks another's reservation.
    """

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("a rate limit must be positive; 0 would mean unlimited")
        self.interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = float("-inf")
        self.acquired = 0

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + self.interval
            self.acquired += 1
        if slot > now:
            self._sleep(slot - now)


class ShortBody(Exception):
    """The source sent fewer bytes than the listing says the file holds."""


class CountingReader:
    """A file-like view of an HTTP body that counts what it hands out."""

    def __init__(self, raw: Any, on_bytes: Callable[[int], None]) -> None:
        self._raw = raw
        self._on_bytes = on_bytes
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._raw.read(size)
        self.count += len(chunk)
        if chunk:
            self._on_bytes(len(chunk))
        return chunk


@dataclass
class Failure:
    rel: str
    reason: str


def transfer_config() -> TransferConfig:
    config = TransferConfig(
        multipart_threshold=S3_PART_BYTES,
        multipart_chunksize=S3_PART_BYTES,
        max_concurrency=S3_PART_UPLOADS_PER_FILE,
        use_threads=True,
        # The CRT client sizes its own memory and concurrency; pin the classic one
        # so the numbers above are the ones that apply.
        preferred_transfer_client="classic",
    )
    config.max_in_memory_upload_chunks = S3_PARTS_IN_MEMORY
    return config


def _error_detail(response: Any) -> str:
    detail = f"HTTP {response.status_code}"
    if "json" in response.headers.get("Content-Type", ""):
        try:
            detail += f" {response.text[:ERROR_BODY_CHARS]}"
        except Exception:  # noqa: BLE001 - a body we cannot read is not the error
            pass
    return detail


class Copier:
    """Copies listed files from `base_url` to `s3://bucket/dest_prefix/`.

    `session_factory` makes one HTTP session per worker thread (requests sessions
    are not thread-safe). The token goes in a header, never in a URL or a log.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        user_agent: str,
        s3: Any,
        bucket: str,
        dest_prefix: str,
        limiter: RateLimiter | None = None,
        workers: int = SOURCE_CONCURRENCY,
        session_factory: Callable[[], Any] = requests.Session,
        config: TransferConfig | None = None,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent,
            # The size check compares bytes on disk at the source; a transparently
            # compressed body would never match it.
            "Accept-Encoding": "identity",
            # Errors as JSON, not a browser redirect to a login page.
            "X-Requested-With": "XMLHttpRequest",
        }
        self.s3 = s3
        self.bucket = bucket
        self.dest_prefix = dest_prefix.strip("/")
        self.limiter = limiter or RateLimiter(SOURCE_REQUESTS_PER_SEC)
        self.workers = workers
        self.session_factory = session_factory
        self.config = config or transfer_config()
        self.log = log
        self.sleep = sleep
        self.clock = clock
        self._local = threading.local()
        self._lock = threading.Lock()
        self._bytes = 0
        self._in_flight = 0
        self.max_in_flight = 0

    # -- per thread ---------------------------------------------------------

    def _session(self) -> Any:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self.session_factory()
            self._local.session = session
        return session

    def _add_bytes(self, n: int) -> None:
        with self._lock:
            self._bytes += n

    def _get(self, url: str) -> Any:
        """One GET, after a token. Counts GETs in flight for the concurrency test."""
        self.limiter.acquire()
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            return self._session().get(
                url, headers=self.headers, stream=True, timeout=HTTP_TIMEOUT, allow_redirects=False
            )
        except BaseException:
            self._release()
            raise

    def _release(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def copy_one(self, rel: str, size: int) -> None:
        """Copy one file, retrying a retryable failure; raise on the last."""
        url = self.base_url + quote(rel)
        key = f"{self.dest_prefix}/{rel}"
        last = ""
        for attempt in range(1, ATTEMPTS_PER_FILE + 1):
            started = self.clock()
            retryable = True
            try:
                response = self._get(url)
                try:
                    if response.status_code != 200:
                        retryable = response.status_code in RETRYABLE_STATUS
                        raise RuntimeError(_error_detail(response))
                    reader = CountingReader(response.raw, self._add_bytes)
                    self.s3.upload_fileobj(reader, self.bucket, key, Config=self.config)
                    if reader.count != size:
                        self._delete(key)
                        raise ShortBody(f"{reader.count} of {size} bytes")
                finally:
                    response.close()
                    self._release()
            except Exception as exc:  # noqa: BLE001 - every failure is reported per file
                last = (
                    f"{type(exc).__name__}: {exc}"
                    if not isinstance(exc, RuntimeError)
                    else str(exc)
                )
                if not retryable or attempt == ATTEMPTS_PER_FILE:
                    raise RuntimeError(f"attempt {attempt}/{ATTEMPTS_PER_FILE}: {last}") from None
                self.log(f"{rel}: {last}; retrying (attempt {attempt}/{ATTEMPTS_PER_FILE}).")
                self.sleep(RETRY_SLEEP_SECS * attempt)
                continue
            secs = self.clock() - started
            self.log(f"Copied {rel} ({size} bytes, {secs:.1f} s)")
            return

    def _delete(self, key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - the size check catches what remains
            self.log(f"could not delete short object {key}: {exc}")

    # -- the pass -------------------------------------------------------------

    def copy(self, todo: Iterable[str], sizes: dict[str, int]) -> list[Failure]:
        """Copy every file in `todo`; return the ones that failed, with why."""
        todo = list(todo)
        total = sum(sizes[rel] for rel in todo)
        failures: list[Failure] = []
        done = threading.Event()
        with self._lock:
            self._bytes = 0
        requests_before = self.limiter.acquired
        started = self.clock()
        reporter = threading.Thread(target=self._report, args=(done, total, started), daemon=True)
        reporter.start()
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(self.copy_one, rel, sizes[rel]): rel for rel in todo}
                for future in as_completed(futures):
                    rel = futures[future]
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001
                        failures.append(Failure(rel, str(exc)))
                        self.log(f"FAILED {rel}: {exc}")
        finally:
            done.set()
            reporter.join()
        elapsed = max(self.clock() - started, 1e-9)
        self.log(
            f"Pass copied {self._bytes / 2**30:.2f} of {total / 2**30:.2f} GiB in {elapsed:.0f} s "
            f"({self._bytes / 2**20 / elapsed:.1f} MiB/s), {len(todo) - len(failures)} of "
            f"{len(todo)} file(s); {self.limiter.acquired - requests_before} request(s) to the "
            "source."
        )
        return failures

    def _report(self, done: threading.Event, total: int, started: float) -> None:
        while not done.wait(PROGRESS_EVERY_SECS):
            elapsed = max(self.clock() - started, 1e-9)
            self.log(
                f"  {self._bytes / 2**30:.2f} / {total / 2**30:.2f} GiB, "
                f"{self._bytes / 2**20 / elapsed:.1f} MiB/s"
            )
