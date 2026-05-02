"""_sync_pool coalescing and serialization."""
import threading
from unittest.mock import patch

import webserver


class TestSyncPoolCoalescing:
    def test_trigger_during_sync_causes_rerun(self):
        call_log = []
        call_started = threading.Event()
        release_first = threading.Event()

        def slow_inner():
            call_log.append("start")
            if len(call_log) == 1:
                call_started.set()
                release_first.wait(timeout=5)
            call_log.append("done")

        with patch("webserver.flask_app._sync_pool_inner", side_effect=slow_inner), \
             patch.object(webserver.flask_app, "_sync_pending", False), \
             patch.object(webserver.flask_app, "_sync_lock", threading.Lock()), \
             patch.object(webserver.flask_app, "_sync_state_lock", threading.Lock()):

            t1 = threading.Thread(target=webserver.flask_app._sync_pool)
            t1.start()

            assert call_started.wait(timeout=5)
            t2 = threading.Thread(target=webserver.flask_app._sync_pool)
            t2.start()

            release_first.set()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert call_log.count("start") == 2
        assert call_log.count("done") == 2

    def test_no_concurrent_syncs(self):
        max_concurrent = [0]
        current = [0]
        lock = threading.Lock()

        def tracking_inner():
            with lock:
                current[0] += 1
                max_concurrent[0] = max(max_concurrent[0], current[0])
            import time as _time
            _time.sleep(0.05)
            with lock:
                current[0] -= 1

        with patch("webserver.flask_app._sync_pool_inner", side_effect=tracking_inner), \
             patch.object(webserver.flask_app, "_sync_pending", False), \
             patch.object(webserver.flask_app, "_sync_lock", threading.Lock()), \
             patch.object(webserver.flask_app, "_sync_state_lock", threading.Lock()):
            threads = [threading.Thread(target=webserver.flask_app._sync_pool) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert max_concurrent[0] == 1
