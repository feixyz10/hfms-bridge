import io

import pytest
from tqdm import tqdm

from hfms_bridge import Transfer
from hfms_bridge.cli import build_parser
from hfms_bridge.http import HTTPError
from hfms_bridge.progress import Progress
from test_transfer import BLOB, options, server


@pytest.fixture
def bars(monkeypatch):
    records = []

    class Recorded(tqdm):
        def __init__(self, **kwargs):
            self.phases = []
            self.before_reset = []
            self.requested_leave = kwargs["leave"]
            self.output = io.StringIO()
            kwargs.update(file=self.output, mininterval=0, dynamic_ncols=False, ncols=100)
            super().__init__(**kwargs)
            records.append(self)

        def reset(self, *args, **kwargs):
            self.before_reset.append(self.n)
            return super().reset(*args, **kwargs)

        def set_description_str(self, desc, refresh=True):
            self.phases.append(desc)
            return super().set_description_str(desc, refresh=refresh)

    monkeypatch.setattr("hfms_bridge.progress.tqdm", Recorded)
    return records


def test_two_rows_count_skips_and_clear_file_rows(tmp_path, bars):
    with server() as (base, data):
        opt = options(tmp_path, base, progress="on")
        Transfer(opt, "ms-secret").run()
        assert [bar.total for bar in bars] == [2, len(BLOB), 5]
        assert [bar.n for bar in bars] == [2, len(BLOB), 5]
        assert [bar.requested_leave for bar in bars] == [True, False, False]
        assert all(bar.disable for bar in bars)  # All closed.
        assert any("Committing" in p for p in bars[1].phases)
        assert "100%" in bars[0].output.getvalue()
        bars.clear()
        Transfer(opt, "ms-secret").run()
        assert bars[0].n == 2
        assert all(bar.n == 0 for bar in bars[1:])
        assert all(any("Skipped" in p for p in bar.phases) for bar in bars[1:])


def test_commit_failure_does_not_advance_total(tmp_path, bars):
    with server() as (base, data):
        data["fail_commit"] = True
        with pytest.raises(HTTPError):
            Transfer(options(tmp_path, base, progress="on"), "ms-secret").run()
        assert bars[0].n == 0
        assert bars[1].n == len(BLOB)
        assert bars[1].disable and bars[0].disable


def test_retry_resets_bytes_without_double_counting(tmp_path, bars, monkeypatch):
    with server() as (base, data):
        data["fail_commit"] = True
        monkeypatch.setattr(
            "hfms_bridge.transfer.Transfer._backoff", lambda self, _: data.update(fail_commit=False)
        )
        opt = options(tmp_path, base, progress="on")
        opt.attempts = 2
        Transfer(opt, "ms-secret").run()
        assert bars[0].n == 2
        assert len(BLOB) in bars[1].before_reset
        assert bars[1].n == 0  # Retry reused the blob, no invented transferred bytes.
        assert any("Reused" in p for p in bars[1].phases)
        assert data["puts"] == 1


def test_hash_pass_is_separate_from_upload(tmp_path, bars):
    with server() as (base, data):
        data["omit_hash"] = True
        Transfer(options(tmp_path, base, progress="on"), "ms-secret").run()
        assert bars[1].n == len(BLOB)
        assert len(BLOB) in bars[1].before_reset
        assert any("Hashing" in p for p in bars[1].phases)
        assert any("HF -> MS" in p for p in bars[1].phases)


def test_filtered_total_and_dry_run(tmp_path, bars):
    with server() as (base, data):
        opt = options(tmp_path, base, progress="on", include=("README.md",))
        Transfer(opt, "ms-secret").run()
        assert bars[0].total == bars[0].n == 1
        bars.clear()
        opt.dry_run = True
        Transfer(opt, "ms-secret").run()
        assert bars == []


def test_auto_off_for_redirected_stderr(monkeypatch, capsys):
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    progress = Progress()
    assert not progress.enabled
    progress.start(1)
    with progress.file("file", 3):
        progress.update(3)
    progress.advance()
    progress.close()
    assert capsys.readouterr().err == ""
    assert build_parser().parse_args(["hf2ms", "a/b", "c/d", "--ms-token", "x"]).progress == "auto"


def test_progress_choice_does_not_change_checkpoint_identity(tmp_path):
    opt = options(tmp_path, "http://localhost", progress="off")
    first = Transfer(opt, "secret")
    other = Transfer(options(tmp_path, "http://localhost", progress="on"), "secret")
    try:
        assert first._identity() == other._identity()
    finally:
        first.source.close()
        first.target.close()
        other.source.close()
        other.target.close()


def test_concurrent_rows_are_independent_and_reused(bars):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    progress = Progress("on")
    progress.start(4)
    barrier = threading.Barrier(3, timeout=10)

    def show(index):
        with progress.file(f"file-{index}", 100 + index):
            progress.phase("HF -> MS", reset=True)
            barrier.wait()
            progress.update(100 + index)
        progress.advance()

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(show, range(3)))
        assert sorted(abs(bar.pos) for bar in bars[1:]) == [1, 2, 3]
        assert all(bar.n == bar.total for bar in bars[1:])
        assert all(bar.disable and not bar.requested_leave for bar in bars[1:])
        assert progress._rows == {}
        with progress.file("next-file", 20):
            progress.update(20)
        progress.advance()
        assert abs(bars[-1].pos) == 1
        assert bars[0].n == bars[0].total == 4
    finally:
        progress.close()
    assert all(bar.disable for bar in bars)


def test_parallel_transfer_progress_and_cleanup(tmp_path, bars):
    import threading
    from test_parallel import parallel_server

    with parallel_server(count=5) as (base, data):
        barrier = threading.Barrier(3, timeout=10)
        data["put_hook"] = lambda name: barrier.wait() if int(name[5:-4]) < 3 else None
        opt = options(tmp_path, base, workers=3, progress="on")
        Transfer(opt, "ms-secret").run()
        assert bars[0].n == bars[0].total == 5
        assert len(bars) == 6
        assert sorted(abs(bar.pos) for bar in bars[1:4]) == [1, 2, 3]
        assert all(1 <= abs(bar.pos) <= 3 for bar in bars[1:])
        assert all(bar.n == bar.total for bar in bars[1:])
        assert all(bar.disable for bar in bars)
        assert all(any("Committing" in p for p in bar.phases) for bar in bars[1:])
        assert all(not bar.requested_leave for bar in bars[1:])


def test_parallel_retry_resets_only_its_own_bar(tmp_path, bars, monkeypatch):
    import threading
    from test_parallel import parallel_server

    with parallel_server(count=3) as (base, data):
        barrier = threading.Barrier(3, timeout=10)
        data["put_hook"] = lambda name: barrier.wait() if data["puts"][name] == 1 else None
        data["fail_put_once"].add("part-0.bin")
        monkeypatch.setattr("hfms_bridge.transfer.Transfer._backoff", lambda self, _: None)
        opt = options(tmp_path, base, workers=3, progress="on")
        opt.attempts = 2
        assert Transfer(opt, "ms-secret").run() == {"committed": 3, "skipped": 0}
        assert data["puts"] == {"part-0.bin": 2, "part-1.bin": 1, "part-2.bin": 1}
        assert bars[0].n == 3
        for bar in bars[1:]:
            assert bar.n == bar.total
            if bar.desc.startswith("part-0.bin"):
                assert bar.total in bar.before_reset
                assert any("Retrying 2/2" in p for p in bar.phases)
            else:
                assert all(n == 0 for n in bar.before_reset)
        assert all(bar.disable for bar in bars)
