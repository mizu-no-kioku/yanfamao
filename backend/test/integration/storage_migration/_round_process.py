"""轮次里跑子进程的两个共用原语：**持续落盘**的日志，与**不依赖组长存活**的整组回收。

两点都是被审查提出来的：

- 日志只在进程结束后写一次的话，超时/被杀那一轮就没有日志可看；而"失败保留日志"正是要
  在失败时才有意义。所以这里一边读一边追加到日志文件。
- `os.killpg` 只能证明"发过信号"，不能证明"整组没了"。而且组长先退出（已被 wait 收割）时，
  旧实现会因为"组长还活着吗"这个前置判断直接跳过回收，留下孤儿。所以这里**先记下 PGID**，
  回收时按 PGID 判断整组是否为空，并在 SIGKILL 之后复核。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_LOG_TAIL_LINES = 40


@dataclass
class CapturedProcess:
    """一个已经起在独立进程组里的子进程，输出持续写入 `log_path`。"""

    process: subprocess.Popen[str]
    pgid: int
    log_path: Path
    _lines: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader: threading.Thread | None = None
    _reap_lock: threading.Lock = field(default_factory=threading.Lock)
    _reaped: bool = False

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def log_text(self) -> str:
        with self._lock:
            return "".join(self._lines)

    def log_tail(self, count: int = _LOG_TAIL_LINES) -> str:
        return "\n".join(self.log_text().splitlines()[-count:])

    def wait_until(self, predicate: Callable[[list[str]], bool], *, deadline: float, poll: float = 0.2) -> bool:
        """轮询到谓词成立、进程退出或超时。返回谓词是否成立。"""

        while True:
            if predicate(self.lines()):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def reap(self, *, grace_seconds: float = 20.0) -> None:
        """整组回收：TERM → 收割组长 → 按 PGID 复核 → 仍有成员就 KILL 再复核。

        可重入：看门狗与 teardown 都可能调用它。整组消失后**还要等读线程收尾**——
        否则"回收返回了"不等于"日志写完了"，紧接着读日志或删目录就会与读线程抢。

        `_reaped` **只在全部成功之后**才置位：先置位的话，一旦信号/退出检查/日志收尾中途失败，
        后续 teardown 会直接返回，既不重试也不再报原来的错——失败就被静默吞掉了。
        """

        with self._reap_lock:
            if self._reaped:
                return
            self._signal_group(signal.SIGTERM)
            self._wait_leader(grace_seconds)
            if self.group_is_alive():
                self._signal_group(signal.SIGKILL)
                self._wait_leader(10.0)
                for _ in range(50):  # 最多 5s：SIGKILL 之后整组应该很快消失
                    if not self.group_is_alive():
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError(f"进程组 {self.pgid} 在 SIGKILL 后仍有成员存活")
            self._join_reader()
            self._reaped = True

    def _join_reader(self, *, timeout: float = 15.0) -> None:
        """等读线程把管道读完并关闭日志文件。`with open(...)` 退出即刷新。"""

        reader = self._reader
        if reader is None:
            return
        reader.join(timeout=timeout)
        if reader.is_alive():
            raise RuntimeError(f"进程已结束，读线程却没有收尾——日志可能不完整：{self.log_path}")

    def live_group_members(self) -> list[int]:
        """本组里**真正活着**的进程（Linux：读 /proc；`pgrp` 在 comm 之后的第 3 个字段）。

        不能用 `os.killpg(pgid, 0)` 判断整组是否退出：僵尸也是组成员，信号能发出去，
        于是"组长已退出、后代被 init 收养后短暂成为僵尸"会被误判成"还活着"。
        """

        proc = Path("/proc")
        if not proc.is_dir():
            return [os.getpid()] if _group_responds(self.pgid) else []
        alive: list[int] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text(encoding="utf-8").rpartition(")")[2].split()
                state, pgrp = fields[0], int(fields[2])
            except (OSError, IndexError, ValueError):
                continue
            if pgrp == self.pgid and state != "Z":
                alive.append(int(entry.name))
        return alive

    def group_is_alive(self) -> bool:
        return bool(self.live_group_members())

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.pgid, sig)
        except ProcessLookupError:
            return

    def _wait_leader(self, timeout: float) -> None:
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return


def _group_responds(pgid: int) -> bool:
    """没有 /proc 时的兜底：能不能给整组发信号（会把僵尸也算作"有人"）。"""

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def spawn_captured(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> CapturedProcess:
    """起子进程，把它自己的进程组记下来，并把输出**持续**追加到日志文件。"""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        # 独立进程组：回收时整组一起收，且不碰测试进程自己的组。
        start_new_session=True,
    )
    pgid = os.getpgid(process.pid)
    captured = CapturedProcess(process=process, pgid=pgid, log_path=log_path)
    _stream_to_log(captured)
    return captured


def _stream_to_log(captured: CapturedProcess) -> None:
    """读线程：每来一行就同时进内存与日志文件（失败那一轮才有东西可看）。"""

    def reader() -> None:
        stream = captured.process.stdout
        if stream is None:
            return
        with open(captured.log_path, "a", encoding="utf-8") as sink:
            for line in stream:
                with captured._lock:
                    captured._lines.append(line)
                sink.write(line)
                sink.flush()

    captured._reader = threading.Thread(target=reader, daemon=True)
    captured._reader.start()
