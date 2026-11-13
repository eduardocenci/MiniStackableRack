#!/bin/sh
# Lays out /opt/senior, generates the MIA_SENIOR_* secrets once (never printed), builds the local image, starts the
# senior-* containers, configures the WAHA "default" session (NOWEB store on, webhook → senior-n8n with a shared-secret
# header), imports the n8n credentials and workflows, installs the nightly backup. Idempotent: re-running keeps .env,
# the data directories, the deploy key and the pairing.
#
# Staged in /root/senior-stage/ beforehand (see README.md → Deploy):
#   docker-compose.yml, setup-senior.sh, backup-senior.sh     (this folder)
#   001_esquema.sql, 002_fase1.sql, 003_ponte_lacunas.sql, servicos/, n8n/*.json   (from the application repo)
#   900_contatos_locais.sql                                    (written on the server only)
#   anthropic.env (optional, one line MIA_SENIOR_ANTHROPIC_API_KEY=…; deleted after use)
#   github.env (optional, one line MIA_SENIOR_GITHUB_ISSUES_TOKEN=…; deleted after use) — fine-grained token,
#     senior-sistemas only, Issues read/write only; without it workflow 7 (suggested gaps → issues) is not published
set -eu
umask 077
B=/opt/senior
S=/root/senior-stage
mkdir -p $B/waha/sessions $B/waha/media $B/postgres/data $B/postgres/init $B/n8n $B/whisper $B/acervo $B/sync/chave \
  $B/build $B/backups
chown 1000:1000 $B/n8n                      # n8n runs as node (uid 1000)
chmod 755 $B/acervo $B/build $B/whisper
cp $S/docker-compose.yml $B/docker-compose.yml
cp $S/001_esquema.sql $S/002_fase1.sql $S/003_ponte_lacunas.sql $S/900_contatos_locais.sql $B/postgres/init/
install -m 700 $S/backup-senior.sh $B/backup-senior.sh
chmod 755 $B/postgres/init   # postgres (uid 70) must read the init scripts; umask 077 would hide them
chmod 644 $B/docker-compose.yml $B/postgres/init/*.sql

rnd() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
touch $B/.env; chmod 600 $B/.env
grep -q '^MIA_SENIOR_WAHA_API_KEY=' $B/.env || echo "MIA_SENIOR_WAHA_API_KEY=$(rnd)" >> $B/.env
grep -q '^MIA_SENIOR_WAHA_DASHBOARD_PASSWORD=' $B/.env || echo "MIA_SENIOR_WAHA_DASHBOARD_PASSWORD=$(rnd | cut -c1-24)" >> $B/.env
grep -q '^MIA_SENIOR_POSTGRES_PASSWORD=' $B/.env || echo "MIA_SENIOR_POSTGRES_PASSWORD=$(rnd)" >> $B/.env
grep -q '^MIA_SENIOR_N8N_ENCRYPTION_KEY=' $B/.env || echo "MIA_SENIOR_N8N_ENCRYPTION_KEY=$(rnd)" >> $B/.env
grep -q '^MIA_SENIOR_WEBHOOK_TOKEN=' $B/.env || echo "MIA_SENIOR_WEBHOOK_TOKEN=$(rnd)" >> $B/.env
if [ -f $S/anthropic.env ]; then
  grep -q '^MIA_SENIOR_ANTHROPIC_API_KEY=' $B/.env || cat $S/anthropic.env >> $B/.env
  rm -f $S/anthropic.env
fi
grep -q '^MIA_SENIOR_ANTHROPIC_API_KEY=' $B/.env || echo "WARNING: MIA_SENIOR_ANTHROPIC_API_KEY missing — senior-agente cannot answer"
if [ -f $S/github.env ]; then
  grep -q '^MIA_SENIOR_GITHUB_ISSUES_TOKEN=' $B/.env || cat $S/github.env >> $B/.env
  rm -f $S/github.env
fi
grep -q '^MIA_SENIOR_GITHUB_ISSUES_TOKEN=' $B/.env || echo "NOTE: MIA_SENIOR_GITHUB_ISSUES_TOKEN missing — workflow 7 (suggested gaps → GitHub issues) stays unpublished"
. $B/.env

# local image (senior-mcp, senior-agente, senior-painel, senior-sync)
rm -rf $B/build/servicos && cp -r $S/servicos $B/build/servicos && chmod -R a+rX $B/build
docker build -q -t senior-acervo:local $B/build/servicos >/dev/null

# read-only deploy key for senior-sync + github.com host key pinned to GitHub's published fingerprint
if [ ! -f $B/sync/chave/id_ed25519 ]; then
  docker run --rm -v $B/sync/chave:/chave senior-acervo:local ssh-keygen -q -t ed25519 -N '' -C senior-sync@mia-docker -f /chave/id_ed25519
  echo "NEW deploy key — add it (read-only) to the repo before senior-sync can pull:"; cat $B/sync/chave/id_ed25519.pub
fi
docker run --rm senior-acervo:local ssh-keyscan -t ed25519 github.com 2>/dev/null > $B/sync/chave/known_hosts.novo
fp=$(docker run --rm -v $B/sync/chave:/chave senior-acervo:local ssh-keygen -lf /chave/known_hosts.novo | awk '{print $2}')
[ "$fp" = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" ] || { echo "github.com host key MISMATCH ($fp)"; exit 1; }
mv $B/sync/chave/known_hosts.novo $B/sync/chave/known_hosts
chmod 600 $B/sync/chave/id_ed25519; chmod 644 $B/sync/chave/id_ed25519.pub $B/sync/chave/known_hosts

cd $B
docker compose pull -q --ignore-pull-failures 2>/dev/null || true   # senior-acervo:local is built, not pulled
docker compose up -d senior-postgres
i=0; until docker exec senior-postgres pg_isready -q -U senior -d acervo_bot; do i=$((i+1)); [ $i -gt 60 ] && exit 1; sleep 2; done
psql() { docker exec -i senior-postgres psql -q -U senior -d acervo_bot -v ON_ERROR_STOP=1 "$@"; }
psql -tAc "select 1 from pg_database where datname='n8n'" | grep -q 1 || psql -c "create database n8n"
psql < $S/002_fase1.sql                                       # idempotent migrations on an existing database
psql < $S/003_ponte_lacunas.sql
psql -c "insert into parametros (chave, valor, descricao) select 'admin_chat_id', telefone || '@c.us', 'alerts recipient (server only)' from contatos where nome = 'Eduardo Cenci' on conflict (chave) do nothing"
docker compose up -d

# WAHA: session default, NOWEB store on, webhook → senior-n8n with the shared-secret header
i=0; until curl -fs -o /dev/null -H "X-Api-Key: $MIA_SENIOR_WAHA_API_KEY" http://localhost:3000/api/sessions; do i=$((i+1)); [ $i -gt 60 ] && echo "waha did not come up" && exit 1; sleep 2; done
CONFIG=$(printf '{"noweb":{"store":{"enabled":true,"fullSync":false}},"metadata":{"projeto":"senior"},"webhooks":[{"url":"http://senior-n8n:5678/webhook/senior-waha","events":["message.any"],"customHeaders":[{"name":"X-Senior-Token","value":"%s"}],"retries":{"policy":"exponential","delaySeconds":2,"attempts":5}}]}' "$MIA_SENIOR_WEBHOOK_TOKEN")
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Api-Key: $MIA_SENIOR_WAHA_API_KEY" -H 'Content-Type: application/json' -d "{\"name\":\"default\",\"start\":true,\"config\":$CONFIG}" http://localhost:3000/api/sessions)
[ "$code" = 201 ] || code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "X-Api-Key: $MIA_SENIOR_WAHA_API_KEY" -H 'Content-Type: application/json' -d "{\"name\":\"default\",\"config\":$CONFIG}" http://localhost:3000/api/sessions/default)
echo "waha session default: HTTP $code"

# n8n: credentials from .env (file removed right after), then the staged workflows, published
i=0; until docker exec senior-n8n wget -qO- http://localhost:5678/healthz >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 90 ] && exit 1; sleep 2; done
D=$B/n8n/import; mkdir -p $D/fluxos
GH=""   # the GitHub credential exists only when the issues-only token is in .env
if [ -n "${MIA_SENIOR_GITHUB_ISSUES_TOKEN:-}" ]; then
  GH=$(printf ',\n {"id": "seniorGithubIssues", "name": "GitHub issues (senior-sistemas, só issues)", "type": "httpHeaderAuth",\n  "data": {"name": "Authorization", "value": "Bearer %s"}}' "$MIA_SENIOR_GITHUB_ISSUES_TOKEN")
fi
cat > $D/cred.json <<JSON
[{"id": "seniorPostgres", "name": "senior-postgres (acervo_bot)", "type": "postgres",
  "data": {"host": "senior-postgres", "port": 5432, "database": "acervo_bot", "user": "senior",
           "password": "$MIA_SENIOR_POSTGRES_PASSWORD", "ssl": "disable", "allowUnauthorizedCerts": false}},
 {"id": "seniorWahaApi", "name": "senior-waha API (X-Api-Key)", "type": "httpHeaderAuth",
  "data": {"name": "X-Api-Key", "value": "$MIA_SENIOR_WAHA_API_KEY"}},
 {"id": "seniorWahaWebhook", "name": "senior-waha → n8n (cabeçalho secreto)", "type": "httpHeaderAuth",
  "data": {"name": "X-Senior-Token", "value": "$MIA_SENIOR_WEBHOOK_TOKEN"}}$GH]
JSON
cp $S/n8n/*.json $D/fluxos/; chown -R 1000:1000 $D
docker exec senior-n8n n8n import:credentials --input=/home/node/.n8n/import/cred.json >/dev/null 2>&1
rm -f $D/cred.json
for f in $D/fluxos/*.json; do
  docker exec senior-n8n n8n import:workflow --input=/home/node/.n8n/import/fluxos/$(basename $f) >/dev/null 2>&1
  id=$(sed -n 's/^ *"id": "\(senior[A-Za-z]*\)",$/\1/p' $f | head -1)
  if [ "$id" = seniorLacunas ] && [ -z "$GH" ]; then echo "n8n: $id imported, not published (no GitHub token)"; continue; fi
  docker exec -e N8N_RUNNERS_BROKER_PORT=5690 senior-n8n n8n publish:workflow --id=$id >/dev/null 2>&1 && echo "n8n: $id published"
done
docker restart senior-n8n >/dev/null

echo '30 3 * * * root /opt/senior/backup-senior.sh >> /var/log/senior-backup.log 2>&1' > /etc/cron.d/senior-backup
chmod 644 /etc/cron.d/senior-backup

sleep 10
docker compose ps --format '{{.Name}} {{.Status}}'
curl -s -H "X-Api-Key: $MIA_SENIOR_WAHA_API_KEY" http://localhost:3000/api/sessions/default | tr ',' '\n' | grep -E '"status"' | head -1
docker exec senior-postgres psql -U senior -d acervo_bot -tAc "select count(*) || ' contact(s), ' || (select count(*) from parametros) || ' parameters' from contatos"
