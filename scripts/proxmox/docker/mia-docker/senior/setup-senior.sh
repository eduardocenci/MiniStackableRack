#!/bin/sh
# Lays out /opt/senior, generates secrets once (never printed), starts senior-waha + senior-postgres,
# creates the WAHA "default" session with the NOWEB store on. Idempotent.
set -eu
umask 077
B=/opt/senior
mkdir -p $B/waha/sessions $B/waha/media $B/postgres/data $B/postgres/init
cp /root/senior-stage/docker-compose.yml $B/docker-compose.yml
cp /root/senior-stage/001_esquema.sql $B/postgres/init/001_esquema.sql
cp /root/senior-stage/900_contatos_locais.sql $B/postgres/init/900_contatos_locais.sql
chmod 755 $B/postgres/init   # postgres (uid 70) must read the init scripts; umask 077 would hide them
chmod 644 $B/docker-compose.yml $B/postgres/init/*.sql
rnd() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
if [ ! -f $B/.env ]; then
  {
    echo "SENIOR_WAHA_API_KEY=$(rnd)"
    echo "SENIOR_WAHA_DASHBOARD_PASSWORD=$(rnd | cut -c1-24)"
    echo "SENIOR_POSTGRES_PASSWORD=$(rnd)"
  } > $B/.env
  chmod 600 $B/.env
fi
cd $B
docker compose pull -q
docker compose up -d
. $B/.env
i=0; until curl -fs -o /dev/null -H "X-Api-Key: $SENIOR_WAHA_API_KEY" http://localhost:3000/api/sessions; do i=$((i+1)); [ $i -gt 60 ] && echo "waha did not come up" && exit 1; sleep 2; done
BODY='{"name":"default","start":true,"config":{"noweb":{"store":{"enabled":true,"fullSync":false}},"metadata":{"projeto":"senior-acervo-bot"}}}'
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Api-Key: $SENIOR_WAHA_API_KEY" -H 'Content-Type: application/json' -d "$BODY" http://localhost:3000/api/sessions)
echo "create session: HTTP $code"
sleep 5
curl -s -H "X-Api-Key: $SENIOR_WAHA_API_KEY" "http://localhost:3000/api/sessions?all=true" | tr ',' '\n' | grep -E '"(name|status)"'
curl -s -H "X-Api-Key: $SENIOR_WAHA_API_KEY" http://localhost:3000/api/version
echo
docker compose ps --format '{{.Name}} {{.Status}}'
docker exec senior-postgres psql -U senior -d acervo_bot -tAc "select count(*) || ' contato(s), ' || (select count(*) from parametros) || ' parametros' from contatos"
