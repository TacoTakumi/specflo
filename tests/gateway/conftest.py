"""What the gateway's tests share: the leak guard on each of them.

A gateway run starts member work and makes a scratch dir of its own, and the
test that starts one must end it, also when it fails. The guard fails a test
that leaves a member scope, a child process, a unix socket or a run scratch
dir behind (see ``tests/leak_guard.py``).
"""

import leak_guard

leak_guarded = leak_guard.autouse()
