"""Shared process CPU/RSS measurements; imports no speech/model framework."""

import threading
import time

import numpy as np


class Measure:
    """CPU seconds are authoritative; CPU% uses 100% per logical core."""

    def __enter__(self):
        import psutil

        self.process = psutil.Process()
        self.cores = psutil.cpu_count()
        self.cpu = sum(self.process.cpu_times()[:2])
        self.swap = psutil.swap_memory()
        self.memory = psutil.virtual_memory()
        self.start = time.perf_counter()
        self.rss = self.process.memory_info().rss
        self.host = []
        self.event = threading.Event()

        def sample():
            psutil.cpu_percent()
            while not self.event.wait(0.1):
                self.rss = max(self.rss, self.process.memory_info().rss)
                self.host.append(psutil.cpu_percent())

        self.thread = threading.Thread(target=sample, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *error):
        import psutil

        wall = time.perf_counter() - self.start
        cpu = sum(self.process.cpu_times()[:2]) - self.cpu
        info = self.process.memory_info()
        swap = psutil.swap_memory()
        self.event.set()
        self.thread.join()
        self.result = {
            "wall_s": wall,
            "cpu_s": cpu,
            "process_cpu_percent": cpu / wall * 100,
            "machine_capacity_percent": cpu / wall * 100 / self.cores,
            "rss_end_mib": info.rss / 1024**2,
            "rss_peak_mib": max(self.rss, info.rss) / 1024**2,
            "host_cpu_mean_percent": float(np.mean(self.host)) if self.host else None,
            "host_swap_in_delta_bytes": max(0, swap.sin - self.swap.sin),
            "host_swap_out_delta_bytes": max(0, swap.sout - self.swap.sout),
            "host_available_start_mib": self.memory.available / 1024**2,
        }
