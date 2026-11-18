#!/usr/bin/env bash
cd /root/acko-mohit-devops
bash /mnt/d/MOHIT/job-plans/acko-mohit-devops/sync-to-wsl.sh > /dev/null 2>&1
git add -A
git commit -m "README complete per PDF (run-from-scratch, pages, auth, design notes, honest limits); admin overview reads pointer source-of-truth" | head -1
rsync -a --exclude launchpad.db --exclude preflight.json /root/acko-mohit-devops/launchpad/ /mnt/d/MOHIT/job-plans/acko-mohit-devops/launchpad/
rsync -a /root/acko-mohit-devops/.git/ /mnt/d/MOHIT/job-plans/acko-mohit-devops/.git/ && echo git-synced
