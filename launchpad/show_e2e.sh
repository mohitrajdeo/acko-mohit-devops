#!/usr/bin/env bash
bash /root/acko-mohit-devops/launchpad/tests/e2e.sh 2>&1 | grep -E "PASS|FAIL|✗|✘|failed" | head -20
