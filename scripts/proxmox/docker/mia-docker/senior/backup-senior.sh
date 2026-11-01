#!/bin/sh
# Nightly backup of the senior stack (cron: /etc/cron.d/senior-backup, 03:30 BRT). Runs as root on mia-docker.
#  - pg_dump of acervo_bot and n8n (custom format), WAHA sessions tarball, n8n workflows export, the .env (it holds
#    N8N_ENCRYPTION_KEY — without it the n8n credentials cannot be restored);
#  - kept 14 days in /opt/senior/backups, copied to mia-nas-ds918plus:/volume1/NetBackup/mia-docker-senior (kept 180).
# Restore: pg_restore -U senior -d <db> --clean <file>.dump · tar xzf waha-sessions.tgz -C /opt/senior/waha.
set -eu
umask 077
B=/opt/senior
DIA=$(date +%Y-%m-%d)
D=$B/backups/$DIA
NAS=globalnet@192.168.2.15
DESTINO=/volume1/NetBackup/mia-docker-senior
SSH="ssh -i /root/.ssh/senior-backup -o IdentitiesOnly=yes -o UserKnownHostsFile=/root/.ssh/known_hosts_nas -o StrictHostKeyChecking=yes -o BatchMode=yes"

mkdir -p "$D"
docker exec senior-postgres pg_dump -U senior -Fc acervo_bot > "$D/acervo_bot.dump"
docker exec senior-postgres pg_dump -U senior -Fc n8n > "$D/n8n.dump"
tar czf "$D/waha-sessions.tgz" -C $B/waha sessions
rm -rf $B/n8n/export && mkdir -p $B/n8n/export && chown 1000:1000 $B/n8n/export
docker exec senior-n8n n8n export:workflow --all --separate --output=/home/node/.n8n/export/ >/dev/null 2>&1
tar czf "$D/n8n-fluxos.tgz" -C $B/n8n export
cp $B/.env "$D/env"
chmod 600 "$D"/*
find $B/backups -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +

# the share's inherited ACL is world-readable; the folder is closed to the owner (it holds the .env)
tar czf - -C $B/backups "$DIA" | $SSH $NAS "mkdir -p $DESTINO && chmod 700 $DESTINO && tar xzf - -C $DESTINO && find $DESTINO -mindepth 1 -maxdepth 1 -type d -mtime +180 -exec rm -rf {} +"
date -u +%Y-%m-%dT%H:%M:%SZ > $B/backups/.ultimo-ok
echo "senior-backup $DIA ok: $(du -sh "$D" | cut -f1)"
