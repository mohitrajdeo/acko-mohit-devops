#!/usr/bin/env bash
cd /root/acko-mohit-devops
bash /mnt/d/MOHIT/job-plans/acko-mohit-devops/sync-to-wsl.sh > /dev/null 2>&1
git add -A
git commit -m "PROGRESS/DECISIONS: config snapshots, pointer rollback, failure-stage telemetry logged" | head -1
rsync -a /root/acko-mohit-devops/.git/ /mnt/d/MOHIT/job-plans/acko-mohit-devops/.git/ && echo git-synced
