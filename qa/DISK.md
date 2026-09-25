# Disk preflight

Reclaim space before a run using only resources that can be rebuilt and that no
one else can be relying on. Run it at the start of every QA run.

1. **Baseline.** Save `df -h` for the artifact root and Docker's data root, and
   `docker system df`, to `evidence/disk/before.txt`.
2. **Reclaim.**
   - Unused build cache: `docker builder prune -f`.
   - Dangling images: `docker image prune -f`.
   - Stopped containers that Booley created: list them with
     `docker ps -a --filter status=exited --filter status=created --filter status=dead --format '{{.ID}} {{.Image}} {{.Names}} {{.Labels}}'`
     and remove only those whose container name begins with `booley-`, whose
     labels or image carry an `io.booley.` or `booley.` key, or that an earlier
     QA run's `resources.md` lists.
3. **Report the rest.** Under the artifact root, list earlier QA run
   directories with their sizes in `log.md`. They may hold untriaged findings;
   the maintainer decides when to delete them.
4. **Leave alone:** volumes, named or tagged images, running containers, any
   git worktree, and anything whose ownership is unclear.
5. **Verify.** Save the same commands to `evidence/disk/after.txt` and log the
   space reclaimed. If free space is still below the mission's stated need, add
   a warning to `log.md` and continue the run.
