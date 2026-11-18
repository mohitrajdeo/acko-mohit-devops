#!/usr/bin/env bash
cd /root/acko-mohit-devops
git add -A
git commit -m "UI: dropdown selectors for deploy (all versions+env) and rollback (healthy versions)" | head -1
rsync -a --exclude launchpad.db /root/acko-mohit-devops/launchpad/ /mnt/d/MOHIT/job-plans/acko-mohit-devops/launchpad/
rsync -a /root/acko-mohit-devops/.git/ /mnt/d/MOHIT/job-plans/acko-mohit-devops/.git/ && echo git-synced
