import os

# The suite must be runnable while LITE itself is running: importing main
# executes its single-instance guard at module level, which would otherwise
# sys.exit(0) the test process the moment it detects the live instance. The
# escape hatch below is checked first inside the guard, so importing main is
# always safe here. Individual tests that exercise the real guard logic
# remove this variable for their own scope.
os.environ.setdefault("LITE_ALLOW_MULTI_INSTANCE", "1")
