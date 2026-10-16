#!/usr/bin/env bash
# Ensures a local PostgreSQL + pgvector server is installed, initialised on persistent
# storage, running on 127.0.0.1, and has the app's role, database and extensions.
#
#   scripts/ensure_local_db.sh          # check / install / init / start / provision
#   scripts/ensure_local_db.sh stop     # fast shutdown
#
# Idempotent: safe to run on every boot. Prints "[DB-Started] 1" when this run started
# the server (so the caller knows to stop it on shutdown).
set -euo pipefail

if [[ -z "${DATA_DIR:-}" ]]; then
    if mkdir -p /data 2>/dev/null && [[ -w /data ]]; then DATA_DIR=/data; else DATA_DIR="$(pwd)/data"; fi
fi
PG_MAJOR="${PG_MAJOR:-16}"
PG_DATA_DIR="${PG_DATA_DIR:-$DATA_DIR/pgdata}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-pst_user}"
PG_PASS="${PG_PASS:-pst_local_pass}"
PG_DB="${PG_DB:-pst_search}"
PG_SOCKET_DIR="${PG_SOCKET_DIR:-/tmp}"
PG_LOG="${PG_LOG:-$PG_DATA_DIR/server.log}"
PGVECTOR_VERSION="${PGVECTOR_VERSION:-v0.8.0}"

log() { echo "[$1] ${*:2}"; }

# --- privilege helpers -------------------------------------------------------------
IS_ROOT=0; [[ "$(id -u)" == "0" ]] && IS_ROOT=1
AS_PG_USER=0
if [[ $IS_ROOT == 1 ]] && id postgres >/dev/null 2>&1; then AS_PG_USER=1; fi

as_pg() {
    # Run a command as the postgres OS user when we are root (Docker/Railway), else as ourselves.
    if [[ $AS_PG_USER == 1 ]]; then
        if command -v runuser >/dev/null 2>&1; then runuser -u postgres -- "$@"
        else su postgres -s /bin/bash -c "$(printf '%q ' "$@")"; fi
    else
        "$@"
    fi
}

sudo_if_needed() { if [[ $IS_ROOT == 1 ]]; then "$@"; elif command -v sudo >/dev/null; then sudo "$@"; else "$@"; fi; }

# --- locate binaries -----------------------------------------------------------------
find_bin_dir() {
    local d
    for d in "/usr/lib/postgresql/$PG_MAJOR/bin" \
             $(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V -r) \
             "/opt/homebrew/opt/postgresql@$PG_MAJOR/bin" "/usr/local/opt/postgresql@$PG_MAJOR/bin" \
             $(ls -d /opt/homebrew/opt/postgresql@*/bin /usr/local/opt/postgresql@*/bin 2>/dev/null | sort -V -r); do
        if [[ -x "$d/initdb" && -x "$d/pg_ctl" ]]; then echo "$d"; return 0; fi
    done
    if command -v pg_config >/dev/null 2>&1; then
        d="$(pg_config --bindir)"
        if [[ -x "$d/initdb" ]]; then echo "$d"; return 0; fi
    fi
    return 1
}

install_postgres() {
    if [[ "$OSTYPE" == linux-gnu* ]] && command -v apt-get >/dev/null; then
        log DB-Install "Installing PostgreSQL $PG_MAJOR + pgvector with apt..."
        sudo_if_needed apt-get update -y
        sudo_if_needed apt-get install -y --no-install-recommends ca-certificates curl gnupg postgresql-common
        if ! apt-cache show "postgresql-$PG_MAJOR-pgvector" >/dev/null 2>&1; then
            sudo_if_needed /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y || true
        fi
        sudo_if_needed apt-get install -y --no-install-recommends "postgresql-$PG_MAJOR" "postgresql-contrib-$PG_MAJOR" 2>/dev/null \
            || sudo_if_needed apt-get install -y --no-install-recommends "postgresql-$PG_MAJOR"
    elif [[ "$OSTYPE" == darwin* ]] && command -v brew >/dev/null; then
        log DB-Install "Installing PostgreSQL $PG_MAJOR + pgvector with Homebrew..."
        brew install "postgresql@$PG_MAJOR" pgvector
    else
        log DB-Install "Unsupported platform ($OSTYPE). Install PostgreSQL $PG_MAJOR and pgvector manually."
        exit 1
    fi
}

install_pgvector() {
    if [[ "$OSTYPE" == linux-gnu* ]] && command -v apt-get >/dev/null; then
        log DB-Install "Installing pgvector for PostgreSQL $PG_MAJOR..."
        if sudo_if_needed apt-get install -y --no-install-recommends "postgresql-$PG_MAJOR-pgvector"; then return 0; fi
        log DB-Install "No package available; compiling pgvector $PGVECTOR_VERSION from source..."
        sudo_if_needed apt-get install -y --no-install-recommends build-essential git "postgresql-server-dev-$PG_MAJOR"
        rm -rf /tmp/pgvector
        git clone --depth 1 --branch "$PGVECTOR_VERSION" https://github.com/pgvector/pgvector.git /tmp/pgvector
        (cd /tmp/pgvector && make PG_CONFIG="$PG_BIN_DIR/pg_config" && sudo_if_needed make install PG_CONFIG="$PG_BIN_DIR/pg_config")
        rm -rf /tmp/pgvector
    elif [[ "$OSTYPE" == darwin* ]]; then
        brew install pgvector
    else
        log DB-Install "Cannot install pgvector on $OSTYPE automatically."; exit 1
    fi
}

stop_server() {
    PG_BIN_DIR="$(find_bin_dir)" || { log DB-Stop "PostgreSQL not installed"; exit 0; }
    if [[ -f "$PG_DATA_DIR/postmaster.pid" ]]; then
        as_pg "$PG_BIN_DIR/pg_ctl" -D "$PG_DATA_DIR" -m fast -w stop || true
    fi
    log DB-Stop "Stopped."
}

if [[ "${1:-}" == "stop" ]]; then stop_server; exit 0; fi

# --- 1. binaries -----------------------------------------------------------------------
log DB-Check "Checking for a local PostgreSQL installation..."
if ! PG_BIN_DIR="$(find_bin_dir)"; then
    install_postgres
    PG_BIN_DIR="$(find_bin_dir)" || { log DB-Check "initdb still not found after install"; exit 1; }
fi
export PATH="$PG_BIN_DIR:$PATH"
BIN_MAJOR="$("$PG_BIN_DIR/postgres" --version | sed -E 's/[^0-9]*([0-9]+).*/\1/')"
log DB-Check "Using $("$PG_BIN_DIR/postgres" --version) from $PG_BIN_DIR"

# --- 2. pgvector -------------------------------------------------------------------------
PG_SHAREDIR="$("$PG_BIN_DIR/pg_config" --sharedir 2>/dev/null || echo "/usr/share/postgresql/$BIN_MAJOR")"
if [[ ! -f "$PG_SHAREDIR/extension/vector.control" ]]; then
    install_pgvector
    [[ -f "$PG_SHAREDIR/extension/vector.control" ]] || { log DB-Check "pgvector still missing in $PG_SHAREDIR"; exit 1; }
fi
log DB-Check "pgvector available ($PG_SHAREDIR/extension/vector.control)"

# --- 3. data directory -------------------------------------------------------------------
mkdir -p "$PG_DATA_DIR" "$PG_SOCKET_DIR"
if [[ $AS_PG_USER == 1 ]]; then
    # Only the cluster directory belongs to postgres; uploads/attachments stay with the app.
    chown postgres:postgres "$PG_DATA_DIR"
    [[ -f "$PG_DATA_DIR/PG_VERSION" ]] && chown -R postgres:postgres "$PG_DATA_DIR"
fi
chmod 700 "$PG_DATA_DIR"
if ! as_pg test -w "$PG_DATA_DIR"; then
    log DB-Check "The postgres user cannot reach $PG_DATA_DIR (a parent directory is not traversable). Choose another DATA_DIR/PG_DATA_DIR."
    exit 1
fi

if [[ -f "$PG_DATA_DIR/PG_VERSION" ]]; then
    DATA_MAJOR="$(cat "$PG_DATA_DIR/PG_VERSION")"
    if [[ "$DATA_MAJOR" != "$BIN_MAJOR" ]]; then
        log DB-Check "Data directory $PG_DATA_DIR was created by PostgreSQL $DATA_MAJOR but binaries are $BIN_MAJOR."
        log DB-Check "Run pg_upgrade or set PG_MAJOR=$DATA_MAJOR. Refusing to start."
        exit 1
    fi
else
    if [[ -n "$(ls -A "$PG_DATA_DIR" 2>/dev/null)" ]]; then
        log DB-Init "$PG_DATA_DIR is not empty and has no PG_VERSION; refusing to initdb over it."; exit 1
    fi
    log DB-Init "Initialising a new cluster in $PG_DATA_DIR..."
    as_pg "$PG_BIN_DIR/initdb" -D "$PG_DATA_DIR" -U postgres --encoding=UTF8 --locale=C \
        --auth-local=trust --auth-host=scram-sha-256 >/dev/null
    cat >> "$PG_DATA_DIR/postgresql.conf" <<CONF

# --- added by ensure_local_db.sh ---
listen_addresses = '127.0.0.1'
max_connections = 50
shared_buffers = '256MB'
maintenance_work_mem = '256MB'
work_mem = '16MB'
CONF
fi

# --- 4. start ----------------------------------------------------------------------------
export PGOPTIONS="-c client_min_messages=warning"
PSQL_SU=("$PG_BIN_DIR/psql" -X -q -v ON_ERROR_STOP=1 -h "$PG_SOCKET_DIR" -p "$PG_PORT" -U postgres)
STARTED=0
if as_pg "$PG_BIN_DIR/pg_isready" -q -h "$PG_SOCKET_DIR" -p "$PG_PORT"; then
    log DB-Start "PostgreSQL is already running on port $PG_PORT."
else
    PIDFILE="$PG_DATA_DIR/postmaster.pid"
    if [[ -f "$PIDFILE" ]]; then
        OLDPID="$(head -1 "$PIDFILE")"
        if ! kill -0 "$OLDPID" 2>/dev/null || [[ "$(ps -p "$OLDPID" -o comm= 2>/dev/null)" != postgres* ]]; then
            log DB-Start "Removing stale postmaster.pid (pid $OLDPID is not a running postgres)."
            rm -f "$PIDFILE"
        fi
    fi
    log DB-Start "Starting PostgreSQL on 127.0.0.1:$PG_PORT..."
    as_pg "$PG_BIN_DIR/pg_ctl" -D "$PG_DATA_DIR" -l "$PG_LOG" -w -t 120 \
        -o "-p $PG_PORT -k $PG_SOCKET_DIR -c listen_addresses=127.0.0.1" start >/dev/null
    STARTED=1
fi

# --- 5. role, database, extensions -------------------------------------------------------
log DB-Setup "Ensuring role '$PG_USER', database '$PG_DB' and extensions..."
as_pg "${PSQL_SU[@]}" -d postgres -v u="$PG_USER" -v p="$PG_PASS" -v d="$PG_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'u') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'u')\gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'u', :'p')\gexec
SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8''', :'d', :'u') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'d')\gexec
SQL
as_pg "${PSQL_SU[@]}" -d "$PG_DB" -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"

log DB-Ready "postgresql://$PG_USER:****@127.0.0.1:$PG_PORT/$PG_DB"
log DB-Started "$STARTED"
