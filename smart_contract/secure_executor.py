import multiprocessing
import time
import psutil
from smart_contract.sandbox_runner import sandbox_contract_runner

TIMEOUT = 20.0
MEMORY_LIMIT_MB = 500


class SecureContractExecutor:
    def __init__(self, code: str):
        self.code = code

    def run(self, func_name: str, args, state):
        with multiprocessing.Manager() as manager:
            result = manager.dict()
            process = multiprocessing.Process(target=sandbox_contract_runner,
                args=(self.code, func_name, args, state, result))
            error = None
            try:
                process.start()
                started = time.monotonic()
                monitor = psutil.Process(process.pid)
                while process.is_alive():
                    if time.monotonic() - started > TIMEOUT:
                        error = "Execution timeout"
                        break
                    try:
                        if monitor.memory_info().rss > MEMORY_LIMIT_MB * 1024 * 1024:
                            error = "Memory limit exceeded"
                            break
                    except psutil.NoSuchProcess:
                        break
                    time.sleep(0.05)
                if error:
                    process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join()
                required = {"state", "msg", "gas_used", "error"}
                if not required.issubset(result.keys()) or process.exitcode != 0:
                    error = error or "Contract process exited without a complete result"
                error = error or result.get("error")
                return {"success": error is None, "error": error,
                        "state": result.get("state"), "msg": result.get("msg"),
                        "gas_used": result.get("gas_used", 0)}
            finally:
                # Flight cleanup also covers exceptions during launch or monitoring.
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=2)
                    if process.is_alive():
                        process.kill()
                        process.join()
                    process.close()
