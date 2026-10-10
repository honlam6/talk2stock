# jshERP docker-compose SQL mount path bug

## Symptom
`docker compose up -d --build` builds all images fine, creates containers, but
`erp-mysql` fails its healthcheck and the stack errors with:
```
dependency failed to start: container erp-mysql is unhealthy
```
MySQL logs show, during init SQL execution:
```
ERROR: Can't initialize batch_readline - may be the input source is a directory or a block device.
```

## Root cause
In `deploy/docker-compose.yml` the mysql service mounts:
```
- ./jshERP-boot/docs/jsh_erp.sql:/docker-entrypoint-initdb.d/init.sql:ro
```
Docker Compose resolves the bind source `./jshERP-boot/...` **relative to the
directory containing the compose file** (`deploy/`), not the build context (`..`).
The SQL actually lives at repo-root `jshERP-boot/docs/jsh_erp.sql`. So `deploy/jshERP-boot`
does not exist → Compose creates it as an empty **directory** → the initdb mount
target `/docker-entrypoint-initdb.d/init.sql` becomes a directory → MySQL refuses it.

Confirm with `docker inspect erp-mysql` — the Mounts Source will show
`.../deploy/jshERP-boot/docs/jsh_erp.sql` (wrong) instead of `.../jshERP-boot/docs/...`.

## Fix (one-line)
Change the mount in `deploy/docker-compose.yml`:
```diff
-      - ./jshERP-boot/docs/jsh_erp.sql:/docker-entrypoint-initdb.d/init.sql:ro
+      - ../jshERP-boot/docs/jsh_erp.sql:/docker-entrypoint-initdb.d/init.sql:ro
```

## Required after the fix
The init SQL only runs when the mysql **data volume is empty**. Because the
previous (broken) run already created the volume (empty, no tables), you must
drop it and start fresh:
```
docker compose down
docker volume rm <compose_project>_mysql-data
docker compose up -d --build
```
Then `erp-mysql` imports `jsh_erp.sql` (jsh_erp DB + tables appear), goes healthy,
and backend/web start.
