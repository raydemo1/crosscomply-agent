from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock, Timer
from typing import Any

MAX_MATERIAL_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 4096
TIMEOUT_SECONDS = 8


class _Output:
    def __init__(self):
        self.data = bytearray()
        self.truncated = False

    def write(self, data: bytes) -> int:
        remaining = MAX_OUTPUT_BYTES - len(self.data)
        self.data.extend(data[:remaining])
        self.truncated |= len(data) > remaining
        return len(data)

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


class PythonSandbox:
    def __init__(self, root: Path):
        import wasmtime

        self.root = root
        self.wasm = wasmtime
        config = wasmtime.Config()
        config.epoch_interruption = True
        self.engine = wasmtime.Engine(config)
        self.module = wasmtime.Module.from_file(self.engine, root / "python.wasm")
        self.lock = Lock()

    def run(self, code: str, material: str) -> dict[str, Any]:
        if "\0" in code:
            return {"ok": False, "error": "Python 代码不能包含空字符。"}
        payload = json.dumps(material, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_MATERIAL_BYTES:
            return {"ok": False, "error": "冻结材料超过 Python 沙箱的 8 MiB 输入上限。"}
        wasm = self.wasm
        stdout, stderr = _Output(), _Output()
        program = (
            "import json as _json, sys as _sys\nmaterial = _json.loads(_sys.argv.pop(1))\n" + code
        )
        with self.lock, wasm.Store(self.engine) as store, wasm.Linker(self.engine) as linker:
            store.set_limits(memory_size=128 * 1024 * 1024)
            store.set_epoch_deadline(1)
            wasi = wasm.WasiConfig()
            wasi.argv = ["/runtime/python.wasm", "-I", "-B", "-S", "-c", program, payload]
            wasi.preopen_dir(str(self.root), "/runtime", fs_mutable=False)
            wasi.stdout_custom = stdout.write
            wasi.stderr_custom = stderr.write
            store.set_wasi(wasi)
            linker.define_wasi()
            # An epoch interrupts guest execution, but cannot interrupt a blocking WASI poll.
            linker.allow_shadowing = True
            i32 = wasm.ValType.i32()
            linker.define_func(
                "wasi_snapshot_preview1",
                "poll_oneoff",
                wasm.FuncType([i32] * 4, [i32]),
                lambda *_: 58,
            )  # WASI ENOTSUP
            timer = Timer(TIMEOUT_SECONDS, self.engine.increment_epoch)
            timer.start()
            error = None
            try:
                instance = linker.instantiate(store, self.module)
                instance.exports(store)["_start"](store)
            except wasm.ExitTrap as exc:
                if exc.code != 0:
                    error = f"Python 执行失败（退出码 {exc.code}）"
            except wasm.Trap as exc:
                error = (
                    "Python 执行超时"
                    if exc.trap_code == wasm.TrapCode.INTERRUPT
                    else "Python 执行中止"
                )
            except wasm.WasmtimeError:
                error = "Python 沙箱无法执行此次代码"
            finally:
                timer.cancel()
                timer.join()
        output = {"stderr": stderr.text(), "truncated": stdout.truncated or stderr.truncated}
        if error:
            return {"ok": False, "error": error, "diagnostic_stdout": stdout.text(), **output}
        return {"ok": True, "stdout": stdout.text(), **output}


@lru_cache(maxsize=1)
def _load_sandbox(root: str) -> PythonSandbox:
    return PythonSandbox(Path(root).resolve())


def sandbox_from_env() -> PythonSandbox | None:
    root = os.getenv("LAWAGENT_WASI_PYTHON_ROOT", "").strip()
    return _load_sandbox(root) if root else None
