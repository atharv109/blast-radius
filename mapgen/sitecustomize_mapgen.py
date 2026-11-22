import atexit
import os

# Auto-start coverage in spawned/subprocess children during map generation only.
# The pytest parent avoids this because the plugin sets COVERAGE_CHILD_GATE only
# after the parent interpreter has already run sitecustomize.
if os.environ.get("COVERAGE_CHILD_GATE"):
    try:
        from coverage import Coverage

        cov = Coverage(data_suffix="CHILD")
        cov.start()

        def _cov_save():
            try:
                cov.stop()
                cov.save()
            except Exception:
                pass

        atexit.register(_cov_save)
    except Exception:
        pass
